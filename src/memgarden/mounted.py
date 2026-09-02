"""挂载好的花园 —— 判断 + 存储都接上，接入方拿到的是能用的东西。

## 和 GardenComponent 的分工

    GardenComponent   只判断，不碰存储。内核可被独立测试、可被替换的前提。
    MountedGarden     把 StoragePort 接上，负责 load → 判断 → 原子写回 → 回执。

为什么要有这一层（sevenfloor 2026-09-02 §3.1）：只有 ``GardenComponent`` 的话，
**每个接入方都得自己编排** tenant、actor、allowed mounts、load、生命周期过滤、
mutation 执行、CAS、幂等键、整理账本、工具搜索、失败后重读重算。那不叫插件，
叫零件——而且这些语义写错了不会报错，只会悄悄丢记忆。

## 🔴 作用域来自 Runtime，不来自模型

``Scope`` 里的 tenant / actor / allowed_mounts **必须由宿主的可信上下文注入**。
模型的工具参数**永远**覆盖不了它们 —— 否则模型只要在参数里写别人的 tenant，
就能读到别人的记忆。这一条是 :meth:`invoke_tool` 里唯一不能商量的地方。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .component import GardenComponent
from .contracts import (
    Actor,
    CaptureRequest,
    ContextRequest,
    ContextResult,
    MaintenanceRequest,
    ToolCall,
    ToolResult,
)
from .records import UnknownMutation, required_capabilities, validate_mutations
from .storage import RevisionConflict

DEFAULT_MOUNT = "agent-private"


class MountPermissionError(PermissionError):
    """请求碰了它无权访问的 mount。**默认拒绝** —— 不在允许列表里就是不允许。"""


@dataclass(frozen=True)
class Scope:
    """一次调用的可信作用域。**由 Runtime 提供，不由模型提供。**

    ``allowed_mounts`` 为空时退化成只有默认的 ``agent-private`` —— 空列表
    绝不能被理解成「都可以」，那是权限系统最经典的翻车方式。
    """

    tenant_id: str
    actor: Actor = field(default_factory=Actor)
    allowed_mounts: tuple[str, ...] = (DEFAULT_MOUNT,)

    def mounts(self) -> tuple[str, ...]:
        return tuple(self.allowed_mounts) or (DEFAULT_MOUNT,)

    def check(self, mount: str) -> str:
        """确认这个 mount 可用，返回规范化之后的名字。不可用就抛。"""
        target = str(mount or "").strip() or self.mounts()[0]
        if target not in self.mounts():
            raise MountPermissionError(
                f"mount {target!r} not in allowed mounts {self.mounts()!r}"
            )
        return target


@dataclass
class OperationReceipt:
    """一次「判断 + 写入」的回执。

    ``written`` 是**真的落库了**，不是「建议这么写」—— 这个区别对调用方是刚需：
    前者可以告诉用户「记住了」，后者不行。
    """

    written: bool = False
    record_ids: tuple[str, ...] = ()
    revision: str = ""
    #: 判断产出但**没有**写入时的原因（模型觉得没什么可记、整理不需要跑…）。
    #: 它不是错误 —— 空结果是合法结果。
    reason: str = ""
    error: str | None = None
    trace: dict = field(default_factory=dict)
    schema_version: int = 1


@dataclass
class MaintenanceCheck:
    """要不要整理。**先问这一句，别为了问一句就烧一次模型调用。**"""

    needed: bool = False
    reason: str = ""
    trace: dict = field(default_factory=dict)
    schema_version: int = 1


class MountedGarden:
    """把判断和存储接在一起。

    ``store`` 要满足 :mod:`memgarden.storage` 的必需契约。参考实现见
    :mod:`memgarden.stores`。
    """

    def __init__(self, *, model: Any, store: Any, **component_kwargs: Any) -> None:
        self._store = store
        self.component = GardenComponent(model=model, **component_kwargs)

    # -- 记 -------------------------------------------------------------- #

    def capture_and_store(
        self, scope: Scope, request: CaptureRequest
    ) -> OperationReceipt:
        """这段对话里有什么值得记 —— **并且真的写进去**。

        空结果和失败必须分开：模型觉得没什么可记是**正常**的（游标该推进），
        解析彻底失败是**异常**（游标不能动，否则这批对话永远不会再被看一眼）。
        """
        mount = scope.check(request.mount or DEFAULT_MOUNT)
        result = self.component.capture(request)
        if result.error:
            return OperationReceipt(error=result.error,
                                    trace=dict(result.trace or {}))
        if not result.mutations:
            return OperationReceipt(reason="nothing_worth_keeping",
                                    trace=dict(result.trace or {}))
        return self._apply(
            scope, mount, result.mutations,
            idempotency_key=request.idempotency_key,
            trace=dict(result.trace or {}),
        )

    # -- 想起 ------------------------------------------------------------ #

    def context_for_turn(
        self,
        scope: Scope,
        query: str,
        *,
        limit: int = 8,
    ) -> ContextResult:
        """这一轮该想起哪几张 —— **候选自己从库里取**。

        以前要调用方先把候选准备好，于是每个接入方都要重写一遍：查库、
        生命周期过滤、mount 过滤、权限过滤、投影、回填。现在归这里。
        """
        candidates = self._readable_cards(scope)
        return self.component.build_context(ContextRequest(
            query=query,
            actor=scope.actor,
            mounts=scope.mounts(),
            candidates=candidates,
            limit=limit,
        ))

    # -- 整理 ------------------------------------------------------------ #

    def check_maintenance(self, scope: Scope) -> MaintenanceCheck:
        """要不要整理。不调模型。"""
        cards = self._readable_cards(scope)
        result = self.component.run_maintenance(MaintenanceRequest(
            cards=cards, all_cards=cards, dry_run=True,
            known_ids=tuple(str(c.get("id") or "") for c in cards),
        ))
        return MaintenanceCheck(
            needed=result.needed,
            reason=str((result.trace or {}).get("reason") or ""),
            trace=dict(result.trace or {}),
        )

    def run_and_store_maintenance(
        self, scope: Scope, request: MaintenanceRequest
    ) -> OperationReceipt:
        """整理并写回。

        ``request.locale`` **必须给** —— 这个花园用什么语言写卡，只有宿主知道。
        内核在这里不猜：猜错的表现是整理完之后整个花园换了语言，而且没有报错
        （2026-08-24 线上事故就是这个形状）。

        账本（signature / seed_card_count）跟着 trace 一起回来 —— 宿主要存回去，
        否则下次判断不出增量、会反复整理同一批。
        """
        if not str(getattr(request, "locale", "") or "").strip():
            raise ValueError(
                "run_and_store_maintenance 需要 request.locale —— "
                "这个花园用什么语言写卡由宿主决定，内核不猜"
            )
        mount = scope.check(request.mount or DEFAULT_MOUNT)
        cards = self._readable_cards(scope)
        base = request
        result = self.component.run_maintenance(MaintenanceRequest(
            cards=cards,
            all_cards=cards,
            known_ids=tuple(str(c.get("id") or "") for c in cards),
            mount=mount,
            locale=base.locale,
            ai_name=base.ai_name,
            user_name=base.user_name,
            recent_conversations=base.recent_conversations,
            last_seed_card_count=base.last_seed_card_count,
            last_signature=base.last_signature,
        ))
        if result.error:
            return OperationReceipt(error=result.error,
                                    trace=dict(result.trace or {}))
        if not result.needed:
            return OperationReceipt(reason="not_needed",
                                    trace=dict(result.trace or {}))
        if not result.mutations:
            return OperationReceipt(reason="nothing_to_consolidate",
                                    trace=dict(result.trace or {}))
        return self._apply(scope, mount, result.mutations,
                           idempotency_key="", trace=dict(result.trace or {}))

    # -- 给模型的工具 ----------------------------------------------------- #

    def tools(self):
        return self.component.tools()

    def invoke_tool(self, scope: Scope, call: ToolCall) -> ToolResult:
        """执行工具 —— **真查库、真写库**。

        🔴 作用域用 ``scope``，**不用 ``call`` 里带的**。工具参数由模型生成，
        模型只要在参数里写别人的 tenant/mount 就能越权 —— 所以这里从头到尾
        不读 ``call.actor`` / ``call.mounts``。
        """
        if call.name == "memory_search":
            query = str((call.arguments or {}).get("query") or "").strip()
            if not query:
                return ToolResult(ok=False, error="query_required")
            found = self.context_for_turn(scope, query, limit=8)
            by_id = {str(c.get("id") or ""): c for c in self._readable_cards(scope)}
            lines = []
            for rid in found.record_ids:
                card = by_id.get(rid) or {}
                text = str(card.get("summary") or "").strip()
                if text:
                    lines.append(f"- {text}")
            return ToolResult(ok=True, content="\n".join(lines))

        if call.name == "memory_write":
            args = call.arguments or {}
            summary = str(args.get("summary") or "").strip()
            content = str(args.get("content") or "").strip()
            if not summary or not content:
                return ToolResult(ok=False, error="summary_and_content_required")
            mount = scope.check(DEFAULT_MOUNT)
            receipt = self._apply(scope, mount, [{
                "op": "add",
                "mount": mount,
                "card": {"summary": summary, "content": content,
                         "bucket": str(args.get("bucket") or "")},
            }], idempotency_key="", trace={})
            if receipt.error:
                return ToolResult(ok=False, error=receipt.error)
            # 「已经写进去了」和「建议这么写」对调用方是两回事,回执必须说清楚。
            return ToolResult(ok=True, content="ok",
                              mutations=[{"record_id": r} for r in receipt.record_ids])

        return ToolResult(ok=False, error=f"unknown_tool:{call.name}")

    # -- 看和导出 --------------------------------------------------------- #

    def browse(self, scope: Scope, *, include_archived: bool = False):
        cards = self._readable_cards(scope, include_archived=include_archived)
        return self.component.browse(cards)

    def export(self, scope: Scope, *, include_archived: bool = True):
        from .contracts import ExportRequest

        cards = self._readable_cards(scope, include_archived=include_archived)
        return self.component.export(ExportRequest(
            actor=scope.actor, mounts=scope.mounts(),
            include_archived=include_archived,
        ), cards)

    # -- 内部 ------------------------------------------------------------- #

    def _missing_capabilities(self, needed: set[str]) -> set[str]:
        """存储声明支持不了的那些能力。

        存储没有 ``capabilities()`` 时**当作全部支持** —— 这个方法是可选契约，
        缺它不代表能力弱，只代表适配器没实现声明。真做不到的话,写入那一步
        自己会失败,不会静默错。
        """
        declare = getattr(self._store, "capabilities", None)
        if declare is None:
            return set()
        try:
            caps = declare()
        except Exception:  # noqa: BLE001 —— 声明取不到不该让写入失败
            return set()
        out = set()
        for name in needed:
            flag = getattr(caps, f"supports_{name}", None)
            if flag is None:
                flag = getattr(caps, name, None)
            if flag is False:
                out.add(name)
        return out

    def _readable_cards(
        self, scope: Scope, *, include_archived: bool = False
    ) -> list[dict]:
        """这个作用域能看见的卡。

        **过滤在这里做，不在调用方**。放给调用方做的话，25 个接入点就有 25 种
        理解，而漏掉一处的表现是「读到了别人的记忆」——不会报错。
        """
        snapshot = self._store.load(scope.tenant_id,
                                    include_archived=include_archived)
        allowed = set(scope.mounts())
        out = []
        for card in snapshot.cards:
            # 没写 mount 的卡按默认 mount 处理 —— 老数据没有这个字段。
            mount = str(card.get("mount") or DEFAULT_MOUNT)
            if mount in allowed:
                out.append(card)
        return out

    def _apply(
        self, scope: Scope, mount: str, mutations: list[dict], *,
        idempotency_key: str, trace: dict,
    ) -> OperationReceipt:
        stamped = []
        for m in mutations:
            row = dict(m)
            # mount 一律以可信作用域为准 —— 判断层给的只是建议值。
            row["mount"] = scope.check(str(row.get("mount") or mount))
            stamped.append(row)

        # 🔴 进 Store 之前的唯一关口：结构不合法 / 存储支持不了,就根本不写。
        #
        # 以前是送到 Store、Store 不认识就抛。三个问题:每个 Store 各判一遍
        # 松紧不一(有的默默跳过那条,记忆就悄悄少了);错误从存储层冒出来,
        # 调用方看到的是和自己代码对不上号的话;批次里第 3 条不合法时前 2 条
        # 可能已经写进去了 —— 那取决于 Store 有没有事务,不该由合法性决定。
        try:
            typed = validate_mutations(stamped)
        except (UnknownMutation, ValueError) as exc:
            return OperationReceipt(error=f"invalid_mutation:{exc}", trace=trace)

        needed = required_capabilities(typed)
        missing = self._missing_capabilities(needed)
        if missing:
            # 提前说「做不到」,好过写一半再失败 —— 后者留下的半成品状态
            # 最难查:库里既有新卡又有没归档的旧卡,而且没有报错。
            return OperationReceipt(
                error=f"storage_lacks_capabilities:{','.join(sorted(missing))}",
                trace=trace)

        try:
            applied = self._store.apply(
                scope.tenant_id, stamped,
                idempotency_key=idempotency_key or _digest_key(scope, stamped),
                expected_revision=None,
            )
        except RevisionConflict as exc:
            # 让调用方重读重算,而不是覆盖别人刚写的东西。
            return OperationReceipt(error="revision_conflict", trace={
                **trace, "detail": str(exc)})
        ids = tuple(str(r.get("id") or "") for r in applied.results
                    if r.get("id"))
        return OperationReceipt(written=True, record_ids=ids,
                                revision=str(applied.revision), trace=trace)


def _digest_key(scope: Scope, mutations: list[dict]) -> str:
    """调用方没给幂等键时，从内容算一个。

    比「每次都生成新键」强：同一批内容重放不会写第二遍。但**调用方自己给的
    键更好** —— 它知道「同一个 turn」这种业务边界，内容摘要不知道。
    """
    import hashlib
    import json

    payload = json.dumps(
        [scope.tenant_id, mutations], sort_keys=True, ensure_ascii=False,
        default=str,
    )
    return "auto-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
