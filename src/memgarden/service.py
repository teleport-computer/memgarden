"""长驻服务 —— 一行一个 JSON 请求，走 stdio（sevenfloor 2026-09-02 §3.10）。

    memgarden serve --storage sqlite:///path/to/memory.db

## 为什么光有 MCP 工具不够

只注册 ``memory_search`` / ``memory_write`` 的话，能做到的只有「模型主动想起来
要查一下」。做不到的是：

    每轮自动带上相关记忆    模型不调工具就没有
    对话结束自动落卡        同上
    后台整理                根本没有触发点

这三件恰恰是「记忆」这个产品的主体。所以除了模型工具面，还要一个**生命周期
面**给 Runtime 调。

## 协议

一行一个 JSON 对象，回一行 JSON 对象。选行分隔而不是 Content-Length 分帧：
调试时可以直接 `echo '{...}' | memgarden serve`，出问题时人眼能读。

    → {"id": "1", "method": "manifest.get"}
    ← {"id": "1", "ok": true, "result": {...}}

    → {"id": "2", "method": "capture.run", "params": {"scope": {...}, ...}}
    ← {"id": "2", "ok": false, "error": {"code": "invalid_mutation", "message": "…"}}

**错误一律是结构化 code**（见 :data:`memgarden.schema.ERROR_CODES`），调用方
按 code 分支，不要去解析 message 那句人话 —— 那句会改，code 不会。

## 🔴 scope 由调用方在每个请求里给

服务本身不持有「当前用户是谁」。多个 Runtime、多个 agent 可能共用一个进程，
把身份记在进程里迟早串号。谁调用谁负责证明自己是谁 —— 而那个证明来自
Runtime 的可信上下文，不来自模型。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, TextIO

from .contracts import Actor, CaptureRequest, MaintenanceRequest, ToolCall
from .mounted import MountPermissionError, MountedGarden, Scope
from .schema import manifest, schemas


def _scope_from(params: dict) -> Scope:
    raw = params.get("scope") or {}
    if not str(raw.get("tenant_id") or "").strip():
        raise ValueError("scope.tenant_id 必填 —— 服务不猜你是谁")
    actor = raw.get("actor") or {}
    mounts = raw.get("allowed_mounts") or ()
    return Scope(
        tenant_id=str(raw["tenant_id"]),
        actor=Actor(user_id=str(actor.get("user_id") or ""),
                    agent_id=str(actor.get("agent_id") or ""),
                    session_id=str(actor.get("session_id") or "")),
        allowed_mounts=tuple(str(m) for m in mounts) or ("agent-private",),
    )


def _as_dict(obj: Any) -> Any:
    """把 dataclass / 元组变成能 JSON 化的东西。"""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_as_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _as_dict(v) for k, v in obj.items()}
    return obj


class Service:
    """把一个 :class:`~memgarden.mounted.MountedGarden` 包成可远程调用的方法表。"""

    def __init__(self, garden: MountedGarden) -> None:
        self.garden = garden
        self._methods: dict[str, Callable[[dict], Any]] = {
            # ---- Runtime 生命周期面 ----
            "manifest.get": lambda p: manifest(),
            "schema.get": lambda p: schemas(),
            "health.get": lambda p: {"ok": True},
            "capture.run": self._capture,
            "context.get": self._context,
            "maintenance.check": self._maintenance_check,
            "maintenance.run": self._maintenance_run,
            "records.browse": self._browse,
            "records.export": self._export,
            # ---- 模型工具面 ----
            "tool.list": lambda p: [_as_dict(t) for t in self.garden.tools()],
            "tool.invoke": self._invoke,
        }

    # -- 方法 ------------------------------------------------------------- #

    def _capture(self, p: dict) -> Any:
        scope = _scope_from(p)
        return self.garden.capture_and_store(scope, CaptureRequest(
            window=str(p.get("window") or ""),
            locale=str(p.get("locale") or ""),
            ai_name=str(p.get("ai_name") or ""),
            user_name=str(p.get("user_name") or ""),
            idempotency_key=str(p.get("idempotency_key") or ""),
        ))

    def _context(self, p: dict) -> Any:
        return self.garden.context_for_turn(
            _scope_from(p), str(p.get("query") or ""),
            limit=int(p.get("limit") or 8))

    def _maintenance_check(self, p: dict) -> Any:
        return self.garden.check_maintenance(_scope_from(p))

    def _maintenance_run(self, p: dict) -> Any:
        return self.garden.run_and_store_maintenance(
            _scope_from(p),
            MaintenanceRequest(locale=str(p.get("locale") or ""),
                               ai_name=str(p.get("ai_name") or ""),
                               user_name=str(p.get("user_name") or "")))

    def _browse(self, p: dict) -> Any:
        return self.garden.browse(
            _scope_from(p), include_archived=bool(p.get("include_archived")))

    def _export(self, p: dict) -> Any:
        return self.garden.export(_scope_from(p))

    def _invoke(self, p: dict) -> Any:
        # 🔴 scope 用请求里的可信作用域，工具参数里的 actor/mounts 一概不读。
        return self.garden.invoke_tool(_scope_from(p), ToolCall(
            name=str(p.get("name") or ""),
            arguments=dict(p.get("arguments") or {})))

    # -- 派发 ------------------------------------------------------------- #

    def handle(self, request: dict) -> dict:
        """处理一个请求。**永远返回一个 dict，不抛。**

        抛出去的话，长驻进程会因为一次坏请求整个死掉，把其它调用方一起带走。
        """
        rid = request.get("id")
        method = str(request.get("method") or "")
        fn = self._methods.get(method)
        if fn is None:
            return {"id": rid, "ok": False,
                    "error": {"code": "unknown_method", "message": method}}
        try:
            result = fn(dict(request.get("params") or {}))
        except MountPermissionError as exc:
            return {"id": rid, "ok": False,
                    "error": {"code": "mount_not_allowed", "message": str(exc)}}
        except ValueError as exc:
            return {"id": rid, "ok": False,
                    "error": {"code": "invalid_request", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            return {"id": rid, "ok": False,
                    "error": {"code": "internal_error",
                              "message": f"{type(exc).__name__}: {exc}"}}
        return {"id": rid, "ok": True, "result": _as_dict(result)}

    def serve(self, stdin: TextIO | None = None,
              stdout: TextIO | None = None) -> None:
        """读一行、答一行，直到对方关掉管道。"""
        source = stdin if stdin is not None else sys.stdin
        sink = stdout if stdout is not None else sys.stdout
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {"id": None, "ok": False,
                            "error": {"code": "invalid_json",
                                      "message": str(exc)}}
            else:
                response = self.handle(request)
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()   # 不 flush 的话对方会一直等，看起来像挂死
