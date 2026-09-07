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

自带 provider 的 Runtime 使用 host-driven 会话，不把 key 交给服务：

    capture.begin / feed / cancel
    maintenance.begin / feed / cancel

两条 lane 都由服务生成 prompt、解析和写库，Runtime 只负责调用模型并把原始
reply 与截断状态喂回来。

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
import time
from typing import Any, Callable, TextIO

from .contracts import Actor, CaptureRequest, MaintenanceRequest, ToolCall
from .mounted import (
    DEFAULT_MOUNT,
    MaintenanceStorageError,
    MountPermissionError,
    MountedGarden,
    OperationReceipt,
    Scope,
    StorageCapabilityError,
)
from .schema import WIRE_OPERATIONS, manifest, method_schemas, schemas
from .validate import SchemaViolation, validate


#: 冲突后最多重来几轮判断。**必须有界** —— 每一轮都要烧一次模型调用。
#: 到顶了就如实回 revision_conflict，绝不对用户说「记住了」。
_MAX_CAPTURE_RETRIES = 3

#: Host-driven 会话只保存提示词编排状态，不是持久产品数据；宿主断连后若不
#: cancel，必须自动回收。两个 lane 共用一个上限，避免换 method 绕过背压。
_SESSION_TTL_SECONDS = 15 * 60
_MAX_ACTIVE_SESSIONS = 1024


class ServiceError(Exception):
    """带稳定错误码的协议级错误。

    宿主要能靠 `code` 分支处理,而不是去 match 人话消息 —— 消息会改,
    错误码不会。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _scope_from(params: dict) -> Scope:
    raw = params.get("scope") or {}
    if not str(raw.get("tenant_id") or "").strip():
        raise ServiceError("scope_required", "scope.tenant_id 必填 —— 服务不猜你是谁")
    owner = str(raw.get("memory_owner_id") or "").strip()
    if not owner:
        # 🔴 不回退成 tenant，也不回退成 actor.agent_id。
        # 回退的后果是同一个租户下所有 agent 共用一座花园 —— 隔离在最常见的
        # 路径上直接失效，而且一切「正常」，没有任何报错。
        raise ServiceError(
            "memory_owner_required",
            "scope.memory_owner_id 必填：它是这座花园的稳定所有者。"
            "宿主拿不到稳定 owner 时应当关闭记忆功能并告知用户，"
            "而不是让服务替它猜一个",
        )
    actor = raw.get("actor") or {}
    mounts = raw.get("allowed_mounts") or ()
    return Scope(
        tenant_id=str(raw["tenant_id"]),
        memory_owner_id=owner,
        actor=Actor(user_id=str(actor.get("user_id") or ""),
                    agent_id=str(actor.get("agent_id") or ""),
                    session_id=str(actor.get("session_id") or "")),
        allowed_mounts=tuple(str(m) for m in mounts) or ("agent-private",),
    )


def _as_dict(obj: Any) -> Any:
    """把 dataclass / 元组变成能 JSON 化的东西。"""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj):
        # asdict 只展开 dataclass，本身不会保证 tuple 等嵌套值变成 JSON array。
        # handle() 的直接调用结果也必须符合公开 schema，不能只靠最后 json.dumps
        # 恰好替调用方做一次隐式转换。
        return _as_dict(asdict(obj))
    if isinstance(obj, (list, tuple)):
        return [_as_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _as_dict(v) for k, v in obj.items()}
    return obj


class Service:
    """把一个 :class:`~memgarden.mounted.MountedGarden` 包成可远程调用的方法表。"""

    def __init__(
        self, garden: MountedGarden, *, model_available: bool | None = None,
        session_ttl_seconds: float = _SESSION_TTL_SECONDS,
        max_active_sessions: int = _MAX_ACTIVE_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.garden = garden
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds 必须大于 0")
        if max_active_sessions < 1:
            raise ValueError("max_active_sessions 必须至少为 1")
        self._session_ttl_seconds = float(session_ttl_seconds)
        self._max_active_sessions = int(max_active_sessions)
        self._clock = clock
        component = getattr(garden, "component", None)
        inferred_model = getattr(component, "_model", None) is not None
        self._model_available = (inferred_model if model_available is None
                                 else bool(model_available))
        #: 进行中的 host-driven 落卡会话。见 _capture_begin。
        self._sessions: dict[str, dict] = {}
        #: 进行中的 host-driven 整理会话。和 capture 分开，避免类型串用。
        self._maintenance_sessions: dict[str, dict] = {}
        self._methods: dict[str, Callable[[dict], Any]] = {
            # ---- Runtime 生命周期面 ----
            "manifest.get": lambda p: self._manifest(),
            "schema.get": lambda p: {"schemas": schemas(),
                                     "methods": method_schemas()},
            "health.get": lambda p: {"ok": True},
            "capture.run": self._capture,
            # host-driven:模型调用归宿主。见 _capture_begin 的说明。
            "capture.begin": self._capture_begin,
            "capture.feed": self._capture_feed,
            "capture.cancel": self._capture_cancel,
            "context.get": self._context,
            "maintenance.check": self._maintenance_check,
            "maintenance.run": self._maintenance_run,
            "maintenance.begin": self._maintenance_begin,
            "maintenance.feed": self._maintenance_feed,
            "maintenance.cancel": self._maintenance_cancel,
            "records.browse": self._browse,
            "records.export": self._export,
            "records.delete": self._delete,
            "records.write": self._write_one,
            "records.promote": self._promote,
            "records.migrate": self._migrate,
            "history.import": self._import,
            # ---- 模型工具面 ----
            "tool.list": lambda p: [_as_dict(t) for t in self.garden.tools()],
            "tool.invoke": self._invoke,
        }
        self._METHOD_NAMES = tuple(self._methods)
        self._method_schemas = method_schemas()
        self._schemas = schemas()
        # 🔴 方法表、manifest 的 operations、方法级 schema —— 三者必须一致。
        # 不对账的话，加了方法忘了登记（陌生 Runtime 从 manifest 上看不到它，
        # 等于没加）或者登记了没实现（对方照着 manifest 调，得到
        # unknown_method）都不会有任何提示，直到接入方踩上去。
        declared, actual = set(WIRE_OPERATIONS), set(self._methods)
        if declared != actual:
            raise RuntimeError(
                "服务方法表和 manifest 对不上 —— "
                f"manifest 多出 {sorted(declared - actual)}，"
                f"方法表多出 {sorted(actual - declared)}")
        missing_schema = actual - set(self._method_schemas)
        if missing_schema:
            raise RuntimeError(
                f"这些方法没有 request/response schema: {sorted(missing_schema)}")

    def _manifest(self) -> dict[str, Any]:
        """返回当前装配的真实能力，而不是只有方法名的理想清单。"""
        from dataclasses import asdict

        from .storage import describe_for_user, plan_degradations

        disabled: set[str] = set()
        storage_info: dict[str, Any] = {"available": False}
        if not self._model_available:
            # 这两条没有 begin/feed host-driven lane，只能由服务自己的模型完成。
            disabled.update({"history_import", "migrate"})
        storage_capabilities = {
            "capture", "turn_context", "maintenance", "model_tools", "tools",
            "browse", "export", "delete", "curated_write", "promote", "migrate",
            "history_import",
        }
        try:
            store_caps = self.garden._store.capabilities()
        except Exception:  # noqa: BLE001 - 能力声明坏了必须 fail closed
            disabled.update(storage_capabilities)
        else:
            storage_info = {
                "available": True,
                "capabilities": asdict(store_caps),
                "degradations": [asdict(item)
                                 for item in plan_degradations(store_caps)],
                "user_notices": describe_for_user(store_caps),
            }
            if not getattr(store_caps, "supports_owner_scoping", False):
                disabled.update(storage_capabilities)
            if not getattr(store_caps, "supports_hard_delete", False):
                disabled.add("delete")
            if not getattr(store_caps, "supports_atomic_batch", False):
                disabled.update({"capture", "maintenance", "migrate",
                                 "history_import"})
            if not getattr(store_caps, "supports_supersede", False):
                disabled.update({"capture", "maintenance", "history_import"})
            if (not getattr(store_caps, "supports_maintenance_state", False)
                    or not getattr(
                        store_caps, "supports_monotonic_seed_generation", False)):
                disabled.add("maintenance")
        return {
            **manifest(tuple(self._METHOD_NAMES),
                       disabled_capabilities=disabled),
            "storage": storage_info,
        }

    def _purge_expired_sessions(self) -> None:
        cutoff = self._clock() - self._session_ttl_seconds
        for sessions in (self._sessions, self._maintenance_sessions):
            expired = [sid for sid, entry in sessions.items()
                       if float(entry.get("_touched_at", 0)) <= cutoff]
            for sid in expired:
                sessions.pop(sid, None)

    def _put_session(self, sessions: dict[str, dict], sid: str,
                     entry: dict) -> None:
        self._purge_expired_sessions()
        active = len(self._sessions) + len(self._maintenance_sessions)
        if sid not in sessions and active >= self._max_active_sessions:
            raise ServiceError(
                "session_capacity",
                "host-driven 在途会话已达到容量上限；请取消旧会话或稍后重试",
            )
        sessions[sid] = {**entry, "_touched_at": self._clock()}

    def _get_session(self, sessions: dict[str, dict], sid: str) -> dict | None:
        self._purge_expired_sessions()
        entry = sessions.get(sid)
        if entry is not None:
            entry["_touched_at"] = self._clock()
        return entry

    def _pop_session(self, sessions: dict[str, dict], sid: str) -> dict | None:
        self._purge_expired_sessions()
        return sessions.pop(sid, None)

    # -- 方法 ------------------------------------------------------------- #

    def _capture(self, p: dict) -> Any:
        self._require_model()
        scope = _scope_from(p)
        return self.garden.capture_and_store(scope, CaptureRequest(
            window=str(p.get("window") or ""),
            locale=str(p.get("locale") or ""),
            ai_name=str(p.get("ai_name") or ""),
            user_name=str(p.get("user_name") or ""),
            idempotency_key=str(p.get("idempotency_key") or ""),
        ))

    # ---- host-driven 落卡：模型调用归宿主 ---------------------------- #
    #
    # ## 为什么要有这条线
    #
    # `capture.run` 会让**这个服务**去调模型 —— 那意味着它得自己拿到 key、
    # 自己处理超时和重试。而宿主(DSH、io、任何 Runtime)本来就持有这些:
    # provider 配置、key、模型路由、用量统计、超时、取消、重试、遥测。
    #
    # 让服务另开一条宿主管不着的通道,后果很具体:用户在宿主界面上按「停止」,
    # 这次落卡的模型调用停不下来;这次调用也不计入宿主的用量。
    #
    # 所以分工是:**宿主调模型,Garden 决定问什么、怎么解析、要不要重问。**
    #
    #     begin  → {session_id, next_prompt}
    #     宿主用自己的 provider 调模型
    #     feed   → {status: "needs_model", next_prompt}   还要再问一轮
    #              {status: "completed", result}          完事了
    #
    # ## 会话是进程内的,但写入的幂等不依赖它
    #
    # 进程重启会丢掉所有在途会话 —— 这是可接受的:宿主重新 begin 一次即可。
    # **但最终写入的幂等键由宿主给**,所以「崩溃后重放同一轮」不会写第二遍,
    # 这一点不依赖会话还在。

    def _capture_begin(self, p: dict) -> Any:
        import uuid

        scope = _scope_from(p)
        request = CaptureRequest(
            window=str(p.get("window") or ""),
            locale=str(p.get("locale") or ""),
            ai_name=str(p.get("ai_name") or ""),
            user_name=str(p.get("user_name") or ""),
            idempotency_key=str(p.get("idempotency_key") or ""),
        )
        # 🔴 和 capture.run 走**同一条** store-aware 准备路径。
        # 分两份写的话，DSH 这条路看不到已有记忆，于是永远只 add ——
        # 表现是「DSH 上记的东西和别处不一样」，而且不报错。
        prepared, revision = self.garden.prepare_capture(scope, request)
        session = self.garden.component.capture_session(prepared)
        prompt = session.next_prompt()
        if prompt is None:
            # 请求本身就被判定为不用问模型(比如窗口是空的)。直接给结果,
            # 不让宿主白跑一趟。
            return {"status": "completed",
                    "result": self.garden.store_capture_result(
                        scope, prepared, session.result(),
                        expected_revision=revision)}
        sid = uuid.uuid4().hex
        self._put_session(self._sessions, sid, {
            "session": session, "scope": scope,
            "request": prepared, "base_request": request,
            "revision": revision,
        })
        return {"session_id": sid, "status": "needs_model", "next_prompt": prompt}

    def _capture_feed(self, p: dict) -> Any:
        sid = str(p.get("session_id") or "")
        entry = self._get_session(self._sessions, sid)
        if entry is None:
            # 说清楚是「不认识这个会话」,而不是含糊的 internal_error ——
            # 宿主据此决定重新 begin,而不是盲目重试同一个 id。
            # 🔴 用带稳定错误码的类型,别抛裸 KeyError:抛裸的会掉进兜底分支,
            # 宿主拿到的是 internal_error + Python 异常名,分不出「会话过期」
            # 和「服务出 bug」—— 这两件事的处置完全相反(重开 vs 停手报警)。
            raise ServiceError("unknown_session",
                               f"没有这个 capture 会话: {sid!r}"
                               "(可能是服务重启过,重新 capture.begin)")
        session = entry["session"]
        session.feed(str(p.get("reply") or ""),
                     truncated=bool(p.get("truncated")))
        prompt = session.next_prompt()
        if prompt is not None:
            return {"session_id": sid, "status": "needs_model",
                    "next_prompt": prompt}
        result = session.result()
        receipt = self.garden.store_capture_result(
            entry["scope"], entry["request"], result,
            expected_revision=entry.get("revision"))

        # 🔴 冲突了就**重来一轮判断**，而不是把这一轮丢掉。
        #
        # host-driven 这条路的模型由宿主调，服务自己没法重问 —— 所以它把
        # 「再问一次」交回给宿主：重新读库、重新构造 prompt、发回
        # needs_model。宿主那边的循环本来就在处理这个状态，无需改动。
        #
        # 不这么做的后果实测过：并发写一冲突，那一轮的记忆直接没了，
        # 而 capture.feed 回的是一个「完成」的回执，只是 error=revision_conflict
        # —— 宿主多半只看 written，于是这条记忆悄悄消失。
        attempts = int(entry.get("attempts") or 0)
        if receipt.error == "revision_conflict" and attempts < _MAX_CAPTURE_RETRIES:
            prepared, revision = self.garden.prepare_capture(
                entry["scope"], entry["base_request"])
            fresh = self.garden.component.capture_session(prepared)
            prompt = fresh.next_prompt()
            if prompt is not None:
                self._put_session(self._sessions, sid, {
                    "session": fresh, "scope": entry["scope"],
                    "request": prepared, "revision": revision,
                    "base_request": entry["base_request"],
                    "attempts": attempts + 1,
                })
                return {"session_id": sid, "status": "needs_model",
                        "next_prompt": prompt, "retrying_after": "conflict"}

        self._sessions.pop(sid, None)
        return {"status": "completed", "result": receipt}

    def _capture_cancel(self, p: dict) -> Any:
        """宿主主动放弃这次落卡(用户按了停止、turn 被取消)。

        丢掉会话即可 —— 还没写库,没有需要回滚的东西。
        """
        sid = str(p.get("session_id") or "")
        return {"cancelled": self._pop_session(
            self._sessions, sid) is not None}

    def _delete(self, p: dict) -> Any:
        """用户主动删除 —— **真删**。

        作用域一律用可信 scope，不看参数里的其它身份字段：否则调用方
        （或它背后的模型）只要换个 id 就能删别人的卡。
        """
        return self.garden.delete_record(
            _scope_from(p),
            str(p.get("record_id") or ""),
            requested_by=str(p.get("requested_by") or ""),
            reason=str(p.get("reason") or ""),
        )

    def _write_one(self, p: dict) -> Any:
        """用户明说要记的一件事 —— 不做「值不值得」的判断。"""
        from .contracts import CuratedWriteRequest

        return self.garden.write_one(_scope_from(p), CuratedWriteRequest(
            text=str(p.get("text") or ""),
            bucket=str(p.get("bucket") or ""),
            mount=str(p.get("mount") or DEFAULT_MOUNT),
            locale=str(p.get("locale") or ""),
            idempotency_key=str(p.get("idempotency_key") or ""),
        ))

    def _promote(self, p: dict) -> Any:
        """换挂载点。``authorized`` 必须由**宿主**给 —— 这是用户的授权。"""
        from .contracts import PromoteRequest

        return self.garden.promote(_scope_from(p), PromoteRequest(
            record_id=str(p.get("record_id") or ""),
            to_mount=str(p.get("to_mount") or ""),
            authorized=bool(p.get("authorized")),
            reason=str(p.get("reason") or ""),
        ))

    def _migrate(self, p: dict) -> Any:
        self._require_model()
        from .contracts import MigrateRequest

        return self.garden.migrate_and_store(_scope_from(p), MigrateRequest(
            old_cards=str(p.get("old_cards") or ""),
            allowed_ids=tuple(str(x) for x in (p.get("allowed_ids") or ())),
            vocab=str(p.get("vocab") or ""),
            mount=str(p.get("mount") or DEFAULT_MOUNT),
            locale=str(p.get("locale") or ""),
            ai_name=str(p.get("ai_name") or ""),
            user_name=str(p.get("user_name") or ""),
            idempotency_key=str(p.get("idempotency_key") or ""),
        ))

    def _import(self, p: dict) -> Any:
        """历史导入，**分批 + 可断点续跑**。

        宿主把上次返回的 progress 原样传回来就从断点继续。
        """
        self._require_model()
        from dataclasses import asdict

        from .contracts import ImportRequest
        from .importing import ImportProgress

        prior = p.get("progress") or None
        progress = None
        if isinstance(prior, dict):
            known = set(ImportProgress.__dataclass_fields__)
            progress = ImportProgress(
                **{k: v for k, v in prior.items() if k in known})
        out = self.garden.import_history(
            _scope_from(p),
            ImportRequest(
                material=str(p.get("material") or ""),
                mount=str(p.get("mount") or DEFAULT_MOUNT),
                locale=str(p.get("locale") or ""),
                material_kind=str(p.get("material_kind") or ""),
                max_cards=int(p.get("max_cards") or 50),
                policy=p.get("policy"),
                ai_name=str(p.get("ai_name") or ""),
                user_name=str(p.get("user_name") or ""),
                idempotency_key=str(p.get("idempotency_key") or ""),
            ),
            progress=progress,
            max_batches=p.get("max_batches"),
        )
        return {**asdict(out), "done": out.done, "percent": out.percent}

    def _context(self, p: dict) -> Any:
        mount = p.get("mount")
        return self.garden.context_for_turn(
            _scope_from(p), str(p.get("query") or ""),
            limit=int(p.get("limit") or 8),
            mount=str(mount) if mount is not None else None)

    def _maintenance_check(self, p: dict) -> Any:
        mount = p.get("mount")
        return self.garden.check_maintenance(
            _scope_from(p), mount=str(mount) if mount is not None else None)

    def _maintenance_run(self, p: dict) -> Any:
        self._require_model()
        return self.garden.run_and_store_maintenance(
            _scope_from(p),
            MaintenanceRequest(locale=str(p.get("locale") or ""),
                               mount=str(p.get("mount") or DEFAULT_MOUNT),
                               ai_name=str(p.get("ai_name") or ""),
                               user_name=str(p.get("user_name") or ""),
                               recent_conversations=str(
                                   p.get("recent_conversations") or ""),
                               idempotency_key=str(
                                   p.get("idempotency_key") or "")))

    # ---- host-driven 整理：服务决定语义，宿主调用自己的模型 ------------ #

    def _maintenance_begin(self, p: dict) -> Any:
        import uuid

        scope = _scope_from(p)
        request = MaintenanceRequest(
            locale=str(p.get("locale") or ""),
            mount=str(p.get("mount") or DEFAULT_MOUNT),
            ai_name=str(p.get("ai_name") or ""),
            user_name=str(p.get("user_name") or ""),
            recent_conversations=str(p.get("recent_conversations") or ""),
            idempotency_key=str(p.get("idempotency_key") or ""),
        )
        try:
            prepared, revision = self.garden.prepare_maintenance(scope, request)
        except MaintenanceStorageError as exc:
            return {"status": "completed", "result": OperationReceipt(
                error="storage_failed:maintenance_state",
                trace={"detail": str(exc)})}
        session = self.garden.component.maintenance_session(prepared)
        prompt = session.next_prompt()
        if prompt is None:
            return {"status": "completed",
                    "result": self.garden.store_maintenance_result(
                        scope, prepared, session.result(),
                        expected_revision=revision)}
        sid = uuid.uuid4().hex
        self._put_session(self._maintenance_sessions, sid, {
            "session": session, "scope": scope, "request": prepared,
            "base_request": request, "revision": revision, "attempts": 0,
        })
        return {"session_id": sid, "status": "needs_model",
                "next_prompt": prompt}

    def _maintenance_feed(self, p: dict) -> Any:
        sid = str(p.get("session_id") or "")
        entry = self._get_session(self._maintenance_sessions, sid)
        if entry is None:
            raise ServiceError(
                "unknown_session",
                f"没有这个 maintenance 会话: {sid!r}"
                "(可能是服务重启过，重新 maintenance.begin)",
            )
        session = entry["session"]
        session.feed(str(p.get("reply") or ""),
                     truncated=bool(p.get("truncated")))
        prompt = session.next_prompt()
        if prompt is not None:
            return {"session_id": sid, "status": "needs_model",
                    "next_prompt": prompt}

        receipt = self.garden.store_maintenance_result(
            entry["scope"], entry["request"], session.result(),
            expected_revision=entry.get("revision"))
        attempts = int(entry.get("attempts") or 0)
        if receipt.error == "revision_conflict" and attempts < _MAX_CAPTURE_RETRIES:
            prepared, revision = self.garden.prepare_maintenance(
                entry["scope"], entry["base_request"])
            fresh = self.garden.component.maintenance_session(prepared)
            prompt = fresh.next_prompt()
            if prompt is not None:
                self._put_session(self._maintenance_sessions, sid, {
                    "session": fresh, "scope": entry["scope"],
                    "request": prepared,
                    "base_request": entry["base_request"], "revision": revision,
                    "attempts": attempts + 1,
                })
                return {"session_id": sid, "status": "needs_model",
                        "next_prompt": prompt,
                        "retrying_after": "conflict"}
            receipt = self.garden.store_maintenance_result(
                entry["scope"], prepared, fresh.result(),
                expected_revision=revision)

        self._maintenance_sessions.pop(sid, None)
        return {"status": "completed", "result": receipt}

    def _maintenance_cancel(self, p: dict) -> Any:
        sid = str(p.get("session_id") or "")
        return {"cancelled":
                self._pop_session(self._maintenance_sessions, sid) is not None}

    def _browse(self, p: dict) -> Any:
        return self.garden.browse(
            _scope_from(p), include_archived=bool(p.get("include_archived")),
            limit=p.get("limit"), cursor=str(p.get("cursor") or ""))

    def _export(self, p: dict) -> Any:
        # include_archived 以前在这里被丢掉了 —— schema 声明了它、调用方传了，
        # 而实现从不读。导出「归档的也要」的请求会静默返回不含归档的结果。
        return self.garden.export(
            _scope_from(p),
            include_archived=bool(p.get("include_archived", True)),
            limit=p.get("limit"), cursor=str(p.get("cursor") or ""))

    def _invoke(self, p: dict) -> Any:
        # 🔴 scope 用请求里的可信作用域，工具参数里的 actor/mounts 一概不读。
        return self.garden.invoke_tool(_scope_from(p), ToolCall(
            name=str(p.get("name") or ""),
            arguments=dict(p.get("arguments") or {})))

    def _require_model(self) -> None:
        if not self._model_available:
            raise ServiceError(
                "model_not_configured",
                "服务没有配置模型；请改用 begin/feed host-driven lane，"
                "或启动时提供 model",
            )

    # -- 派发 ------------------------------------------------------------- #

    def handle(self, request: Any) -> dict:
        """处理一个请求。**永远返回一个 dict，不抛。**

        抛出去的话，长驻进程会因为一次坏请求整个死掉，把其它调用方一起带走。
        """
        if not isinstance(request, dict):
            return {"id": None, "ok": False,
                    "error": {"code": "invalid_request",
                              "message": "request 必须是 JSON 对象，"
                                         f"收到 {type(request).__name__}"}}
        rid = request.get("id")
        if (rid is not None
                and (not isinstance(rid, (str, int)) or isinstance(rid, bool))):
            return {"id": None, "ok": False,
                    "error": {"code": "invalid_request",
                              "message": "id 必须是 string、integer 或 null"}}
        method = str(request.get("method") or "")
        fn = self._methods.get(method)
        if fn is None:
            return {"id": rid, "ok": False,
                    "error": {"code": "unknown_method", "message": method}}
        # `handle` 承诺**永远返回 dict、不抛**。所以连「把 params 变成 dict」
        # 这一步都要在保护内：params 传成字符串/数组时 dict() 会直接抛，
        # 而一次坏请求把长驻进程带走，会连带影响所有别的调用方。
        raw_params = request.get("params")
        if raw_params is None:
            raw_params = {}
        if not isinstance(raw_params, dict):
            return {"id": rid, "ok": False,
                    "error": {"code": "invalid_request",
                              "message": "params 必须是对象，"
                                         f"收到 {type(raw_params).__name__}"}}
        params = dict(raw_params)
        # 🔴 **执行前**按方法的 request schema 校验。
        # 不校验的话坏请求会走到业务代码里才炸，那时的错误消息是
        # 「NoneType 没有 strip」—— 调用方既不知道哪个字段错了，
        # 也分不出是自己传错还是服务有 bug。
        spec = (self._method_schemas.get(method) or {}).get("request")
        if spec:
            try:
                validate(params, spec, schemas=self._schemas)
            except SchemaViolation as exc:
                return {"id": rid, "ok": False,
                        "error": {"code": "invalid_request",
                                  "message": str(exc), "field": exc.path}}
        try:
            result = fn(params)
        except ServiceError as exc:
            return {"id": rid, "ok": False,
                    "error": {"code": exc.code, "message": str(exc)}}
        except MountPermissionError as exc:
            return {"id": rid, "ok": False,
                    "error": {"code": "mount_not_allowed", "message": str(exc)}}
        except StorageCapabilityError as exc:
            return {"id": rid, "ok": False,
                    "error": {"code": "storage_lacks_capabilities",
                              "message": str(exc)}}
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
