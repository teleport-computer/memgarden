"""长驻服务 + JSON Schema（sevenfloor 2026-09-02 §3.8 / §3.10）。

只有 MCP 的 tool definitions 和 dispatch，接入方跑不起来任何东西 ——
没有 transport、没有生命周期、没有 health、没有版本协商。
这个文件盯的就是「真的能跑」。
"""
from __future__ import annotations

import io
import json
import pathlib
import tempfile

import pytest

from memgarden import MountedGarden, SqliteStore
from memgarden.schema import ERROR_CODES, manifest, schemas
from memgarden.selection import Chain, RecentStage, RelevanceStage
from memgarden.service import Service


class _Model:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return json.dumps({"cards": [{
            "action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
            "summary": "不吃辣，一吃就胃疼", "content": "对方不吃辣，点菜要避开。",
        }]}, ensure_ascii=False)


@pytest.fixture()
def service() -> Service:
    return Service(MountedGarden(
        model=_Model(),
        store=SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db")),
        selection_policy=Chain(stages=(RelevanceStage(limit=8),
                                       RecentStage(limit=4))),
    ))


T1 = {"scope": {"tenant_id": "t1"}}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def test_the_mutation_union_carries_a_discriminator():
    """union 必须能靠一个字段判分支，不能靠「挨个试哪个解析得动」。

    试着解析的做法在错误信息上是灾难：对方只会看到「不匹配任何分支」，
    而不是「supersede 缺 card」。
    """
    mutation = schemas()["Mutation"]
    assert mutation["discriminator"]["propertyName"] == "op"
    titles = {branch["title"] for branch in mutation["oneOf"]}
    assert {"add", "update", "supersede", "archive", "delete"} <= titles


def test_every_branch_lists_its_own_required_fields():
    """每个分支都要列全必填字段，对方才能给出「缺哪个」这种具体错误。"""
    for branch in schemas()["Mutation"]["oneOf"]:
        assert branch.get("required"), f"{branch['title']} 没有 required"
        assert "op" in branch["required"]


def test_unknown_fields_are_allowed_so_new_versions_do_not_break_old_callers():
    """加字段不该让旧调用方崩。"""
    assert schemas()["Card"]["additionalProperties"] is True


def test_the_manifest_states_versions_a_caller_can_check_at_startup():
    """版本不兼容要在**启动时**拒绝，而不是跑到第一条用户消息才失败。"""
    m = manifest()
    for key in ("component_id", "protocol_version",
                "record_schema_version", "mutation_schema_version"):
        assert m.get(key) is not None, f"manifest 缺 {key}"


def test_errors_are_codes_not_prose():
    """错误是**结构化 code** —— 调用方按 code 分支，不解析人话。

    人话会改（改措辞、翻译、加上下文），code 不会。
    """
    assert "invalid_mutation" in ERROR_CODES
    assert "mount_not_allowed" in ERROR_CODES
    assert schemas()["ErrorCode"]["enum"] == list(ERROR_CODES)


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #

def test_a_bad_request_does_not_kill_the_process(service):
    """一次坏请求不能让长驻进程死掉 —— 那会把其它调用方一起带走。"""
    bad_json = io.StringIO('这不是 JSON\n{"id":"2","method":"health.get"}\n')
    out = io.StringIO()
    service.serve(bad_json, out)

    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["code"] == "invalid_json"
    # 进程活着，后面那条照常处理
    assert lines[1]["ok"] is True


def test_an_unknown_method_is_a_structured_error(service):
    out = service.handle({"id": "1", "method": "nope"})
    assert out["ok"] is False
    assert out["error"]["code"] == "unknown_method"


def test_a_missing_tenant_is_refused_rather_than_guessed(service):
    """没给 tenant 就报错。**服务不猜你是谁** —— 猜错就是读到别人的记忆。"""
    out = service.handle({"id": "1", "method": "context.get",
                          "params": {"query": "辣"}})
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"
    assert "tenant_id" in out["error"]["message"]


def test_capture_then_search_over_the_wire(service):
    """走协议跑一遍：落卡 → 搜得到。"""
    wrote = service.handle({"id": "1", "method": "capture.run", "params": {
        **T1, "window": "用户：我不吃辣", "locale": "zh-Hans"}})
    assert wrote["ok"] and wrote["result"]["written"], wrote

    found = service.handle({"id": "2", "method": "tool.invoke", "params": {
        **T1, "name": "memory_search", "arguments": {"query": "辣"}}})
    assert found["ok"] and "不吃辣" in found["result"]["content"], found


def test_one_tenant_cannot_reach_another_over_the_wire(service):
    """协议这一层同样守住租户隔离。"""
    service.handle({"id": "1", "method": "capture.run", "params": {
        **T1, "window": "用户：我不吃辣", "locale": "zh-Hans"}})

    other = service.handle({"id": "2", "method": "tool.invoke", "params": {
        "scope": {"tenant_id": "别人"},
        "name": "memory_search", "arguments": {"query": "辣"}}})
    assert other["ok"] and other["result"]["content"] == "", other


def test_a_service_without_a_model_still_serves_what_needs_no_model():
    """没配模型时，不需要模型的方法照常可用；需要的那些给出说得清的错。

    直接 AttributeError 的话，接入方只会看到一句和自己无关的 Python 报错。
    """
    from memgarden.cli import _NoModel

    svc = Service(MountedGarden(
        model=_NoModel(),
        store=SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db")),
        selection_policy=Chain(stages=(RecentStage(limit=4),)),
    ))
    assert svc.handle({"id": "1", "method": "health.get"})["ok"]
    assert svc.handle({"id": "2", "method": "context.get",
                       "params": {**T1, "query": "辣"}})["ok"]

    needs_model = svc.handle({"id": "3", "method": "capture.run", "params": {
        **T1, "window": "用户：我不吃辣", "locale": "zh-Hans"}})
    assert needs_model["ok"] is False
    assert "没有配模型" in needs_model["error"]["message"]
