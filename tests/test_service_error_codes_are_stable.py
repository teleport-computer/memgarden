"""服务边界上的每种坏情况都要有**稳定的错误码**。

为什么这件事值得单独一个文件：宿主拿到错误之后要分支处理，而分支只能靠
`code`——消息是人话、会改、还可能被翻译。曾经「capture 会话不存在」抛的是
裸 `KeyError`，掉进兜底分支变成 `internal_error` + Python 异常名，于是
「会话过期，重开一个就行」和「服务出 bug，该停手报警」在宿主眼里长得一样。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from memgarden import MountedGarden, SqliteStore
from memgarden.schema import ERROR_CODES
from memgarden.selection import Chain, RecentStage, RelevanceStage
from memgarden.selection import Chain, RecentStage, RelevanceStage
from memgarden.service import Service


class _Model:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return json.dumps({"cards": [{
            "action": "add", "bucket": "偏好与边界",
            "summary": "喜欢喝美式", "content": "下午常喝美式。",
        }]}, ensure_ascii=False)


class _NoModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        from memgarden.service import ServiceError

        raise ServiceError("model_not_configured", "没配模型")


def _service(model=None) -> Service:
    return Service(MountedGarden(
        model=model or _Model(),
        store=SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db")),
        selection_policy=Chain(stages=(RelevanceStage(limit=8),
                                       RecentStage(limit=4))),
    ))


SCOPE = {"tenant_id": "t1", "actor": {"user_id": "u1", "agent_id": "a1"},
         "allowed_mounts": ["agent-private"]}


def _err(service: Service, method: str, params: dict) -> str:
    out = service.handle({"id": "1", "method": method, "params": params})
    assert out["ok"] is False, f"{method} 本该失败，却成功了：{out}"
    return out["error"]["code"]


def test_unknown_method_has_its_own_code() -> None:
    assert _err(_service(), "没有这个方法", {}) == "unknown_method"


def test_unknown_capture_session_is_not_internal_error() -> None:
    """最关键的一条：会话不存在 ≠ 服务出 bug。"""
    code = _err(_service(), "capture.feed",
                {"session_id": "根本不存在", "reply": "{}"})
    assert code == "unknown_session"


def test_cancelled_session_reports_unknown_session() -> None:
    s = _service()
    begun = s.handle({"id": "1", "method": "capture.begin", "params": {
        "scope": SCOPE, "window": "用户：我爱喝美式", "locale": "zh-Hans",
    }})["result"]
    s.handle({"id": "2", "method": "capture.cancel",
              "params": {"session_id": begun["session_id"]}})
    code = _err(s, "capture.feed",
                {"session_id": begun["session_id"], "reply": "{}"})
    assert code == "unknown_session"


def test_missing_model_reports_model_not_configured() -> None:
    """没配模型时要说得清 —— 宿主据此改走 capture.begin/feed 那条路。"""
    code = _err(_service(_NoModel()), "capture.run",
                {"scope": SCOPE, "window": "x", "locale": "zh-Hans"})
    assert code == "model_not_configured"


def test_reads_can_be_narrowed_to_one_mount() -> None:
    """宿主可以把这一轮收窄到某个挂载点。"""
    s = _service()
    s.handle({"id": "1", "method": "capture.run", "params": {
        "scope": SCOPE, "window": "用户：我爱喝美式", "locale": "zh-Hans",
    }})
    out = s.handle({"id": "2", "method": "context.get", "params": {
        "scope": SCOPE, "query": "喝什么", "mount": "agent-private",
    }})
    assert out["ok"] is True
    assert out["result"]["blocks"], "收窄到卡自己所在的挂载点应该读得到"


def test_narrowing_to_an_unauthorized_mount_is_refused() -> None:
    """🔴 越权收窄必须被**拒绝**，不能被悄悄忽略。

    悄悄忽略的后果很具体：宿主以为这一轮只读 shared，实际读了这个租户的
    全部记忆，包括 agent-private —— 而且没有任何报错。
    """
    code = _err(_service(), "context.get",
                {"scope": SCOPE, "query": "x", "mount": "shared"})
    assert code == "mount_not_allowed"


@pytest.mark.parametrize("code", [
    "unknown_method", "invalid_request", "unknown_session",
    "model_not_configured", "mount_not_allowed", "internal_error",
])
def test_every_code_the_service_emits_is_declared(code: str) -> None:
    """服务能吐出来的码，必须在 schema 里声明过。

    没声明的码等于没有契约：宿主写 switch 时看不到它，只能落进 default。
    """
    assert code in ERROR_CODES
