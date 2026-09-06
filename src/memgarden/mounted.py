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

from dataclasses import dataclass, field, replace
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
from .rendering import render_buckets, render_card_index, render_threads
from .storage import (IdempotencyConflict, MutationRejected, PartialFailure,
                      RevisionConflict)

DEFAULT_MOUNT = "agent-private"


class MissingMemoryOwner(ValueError):
    """作用域里没有稳定的记忆归属人。

    单独一个类型，是为了让宿主能把它和别的参数错误分开处理：这一条的正确
    处置是**关闭记忆功能并告诉用户**，不是重试，也不是塞一个默认值进去。
    """


class MountPermissionError(PermissionError):
    """请求碰了它无权访问的 mount。**默认拒绝** —— 不在允许列表里就是不允许。"""


@dataclass(frozen=True)
class Scope:
    """一次调用的可信作用域。**由 Runtime 提供，不由模型提供。**

    ``allowed_mounts`` 为空时退化成只有默认的 ``agent-private`` —— 空列表
    绝不能被理解成「都可以」，那是权限系统最经典的翻车方式。

    ## tenant / owner / actor 是三样东西，别混

        tenant_id         账户、组织或部署的**安全边界**
        memory_owner_id   一座长期花园的**稳定所有者**
        actor             此刻在执行的是谁（agent / session / turn 来源身份）

    最容易错的是把 session 当成 owner：那样用户换个设备、重开一轮，
    拿到的就是一座空花园 —— 而这在测试里根本看不出来，测试都是新建的。

    反过来，把 owner 省掉、只用 tenant 也不行：同一个账户下两个 agent
    各自的 agent-private 就互相读得到了。sevenfloor 2026-09-06 复现的正是
    这一条 —— 当时的「多 agent 隔离测试」用的是两个不同 tenant，
    验的是另一件事。
    """

    tenant_id: str
    #: 🔴 **必填。** 空值直接拒绝，不回退成默认花园 —— 回退的后果是
    #: 所有没显式给 owner 的调用共用同一座花园，隔离在最常见的路径上失效。
    memory_owner_id: str = ""
    actor: Actor = field(default_factory=Actor)
    allowed_mounts: tuple[str, ...] = (DEFAULT_MOUNT,)

    def owner(self) -> str:
        """稳定的记忆归属人。缺了就抛 —— fail closed。"""
        value = str(self.memory_owner_id or "").strip()
        if not value:
            raise MissingMemoryOwner(
                "Scope.memory_owner_id 必填：缺稳定 owner 时必须 fail closed。"
                "宿主拿不到稳定 owner 时应当明确关闭记忆功能，"
                "而不是回退成一个全局默认值（那会让同租户的用户互相读到）。"
            )
        return value

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
class Page:
    """一页结果 + 下一页的游标。

    ``next_cursor`` 为空表示没有下一页 —— 调用方据此停止，不用自己数总数。

    ## 为什么它可迭代、还转发属性

    分页是**加法**：以前 ``browse()`` 直接返回一个列表、``export()`` 返回一个
    带 ``counts`` 的结果对象。套一层新类型会让所有现有调用方一起断，而它们
    并没有做错什么 —— 这个包对外发过版，别人的代码在跑。

    所以 ``for x in page`` / ``len(page)`` / ``page[0]`` 和以前一样，
    ``page.counts`` 这类原结果上的字段也照常取得到；要分页的人才去读
    ``page.next_cursor``。
    """

    items: Any = None
    next_cursor: str = ""
    total: int = 0
    schema_version: int = 1

    def __iter__(self):
        return iter(self.items if self.items is not None else ())

    def __len__(self) -> int:
        try:
            return len(self.items)
        except TypeError:
            return 0

    def __getitem__(self, index):
        return self.items[index]

    def __eq__(self, other) -> bool:
        # 和裸列表比较时按内容比 —— 老测试写的是 `assert browse(...) == []`。
        if isinstance(other, Page):
            return (self.items, self.next_cursor, self.total) == (
                other.items, other.next_cursor, other.total)
        return self.items == other

    def __getattr__(self, name: str):
        # dataclass 自己的字段走不到这里（只有找不到时才调），所以不会打架。
        try:
            return getattr(object.__getattribute__(self, "items"), name)
        except AttributeError:
            raise AttributeError(name) from None


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

        ## 这一步会先读库，不只是判断

        调用方只需要给可信 Scope 和这一轮的对话文本。已有的卡、桶、线索
        由这里从库里取出来渲染进判断请求 —— 否则模型看不见任何旧卡，
        于是**永远只会 add**：同一件事说两遍就是两张 active 卡，
        而且每一步都「成功」，没人会发现。

        空结果和失败必须分开：模型觉得没什么可记是**正常**的（游标该推进），
        解析彻底失败是**异常**（游标不能动，否则这批对话永远不会再被看一眼）。
        """
        mount = scope.check(request.mount or DEFAULT_MOUNT)

        def judge(prepared: CaptureRequest):
            return self.component.capture(prepared)

        return self._capture_with_cas(scope, mount, request, judge)

    def prepare_capture(
        self, scope: Scope, request: CaptureRequest
    ) -> tuple[CaptureRequest, Any]:
        """把库里的现状填进 Capture 请求，并把快照版本一起返回。

        host-driven（``capture.begin``/``feed``）和普通 ``capture`` 走的是
        **同一个**准备函数 —— 分两份写的话，两条路看到的旧记忆会不一样，
        表现是「DSH 上记的东西和别处不一样」，而且不报错。
        """
        snapshot = self._snapshot(scope)
        cards = self._visible(scope, snapshot.cards)
        prepared = replace(
            request,
            mount=scope.check(request.mount or DEFAULT_MOUNT),
            # 调用方已经渲染过就尊重它（宿主可能有更好的身份信息）；
            # 没给才由我们从库里填 —— 但**不能两边都空着**。
            cards=request.cards or render_card_index(cards),
            buckets=request.buckets or render_buckets(cards),
            threads=request.threads or render_threads(cards),
        )
        return prepared, snapshot.revision

    def store_capture_result(
        self, scope: Scope, request: CaptureRequest, result: Any,
        *, expected_revision: Any = None,
    ) -> OperationReceipt:
        """把**已经判断完**的落卡结果写库。

        给 host-driven 用：模型由宿主调（它持有 key、路由、用量、超时、取消），
        判断由内核做，写库这一步仍然归这里 —— 否则每个宿主又要自己实现一遍
        mount 校验、typed mutation 关口、能力检查、幂等和 CAS。

        ``expected_revision`` 来自 :meth:`prepare_capture`。给了就走 CAS：
        判断期间别人写过，这次提交会被拒，调用方需要重新走一遍准备+判断。
        """
        if getattr(result, "error", None):
            return OperationReceipt(error=result.error,
                                    trace=dict(getattr(result, "trace", {}) or {}))
        mutations = list(getattr(result, "mutations", []) or [])
        if not mutations:
            return OperationReceipt(reason="nothing_worth_keeping",
                                    trace=dict(getattr(result, "trace", {}) or {}))
        return self._apply(
            scope, scope.check(request.mount or DEFAULT_MOUNT), mutations,
            idempotency_key=request.idempotency_key,
            trace=dict(getattr(result, "trace", {}) or {}),
            expected_revision=expected_revision,
        )

    # -- 想起 ------------------------------------------------------------ #

    def context_for_turn(
        self,
        scope: Scope,
        query: str,
        *,
        limit: int = 8,
        mount: str | None = None,
    ) -> ContextResult:
        """这一轮该想起哪几张 —— **候选自己从库里取**。

        以前要调用方先把候选准备好，于是每个接入方都要重写一遍：查库、
        生命周期过滤、mount 过滤、权限过滤、投影、回填。现在归这里。
        """
        # 宿主可以把这一轮**收窄**到某一个挂载点。收窄同样要过权限检查 ——
        # 悄悄忽略一个不认识的 mount，宿主会以为自己限制住了，实际读的是全部。
        if mount is not None:
            scope.check(mount)
        mounts = (mount,) if mount is not None else scope.mounts()
        candidates = [c for c in self._readable_cards(scope)
                      if mount is None
                      or str(c.get("mount") or DEFAULT_MOUNT) == mount]
        return self.component.build_context(ContextRequest(
            query=query,
            actor=scope.actor,
            mounts=tuple(mounts),
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
        """整理并写回。**账本由这里读、由这里写，和卡改动同一次提交。**

        ``request.locale`` **必须给** —— 这个花园用什么语言写卡，只有宿主知道。
        内核在这里不猜：猜错的表现是整理完之后整个花园换了语言，而且没有报错
        （2026-08-24 线上事故就是这个形状）。

        ## 账本为什么不能丢给宿主存

        以前这里把 signature / seed_card_count 放进 trace，让宿主存回去。
        宿主重启一次就忘了，表现是**同一批卡被反复合并**。而且宿主分两步存
        （先存账本再写卡，或反过来）时，两种坏法都很隐蔽：

            账本先走 → 这批整理再也不会跑，改动丢了没人知道
            卡先走   → 下次照样整理同一批，重复合并

        所以账本落到 Store，并且和卡改动**在同一个事务里**成或败。
        """
        if not str(getattr(request, "locale", "") or "").strip():
            raise ValueError(
                "run_and_store_maintenance 需要 request.locale —— "
                "这个花园用什么语言写卡由宿主决定，内核不猜"
            )
        mount = scope.check(request.mount or DEFAULT_MOUNT)
        ledger = self.maintenance_ledger(scope, mount=mount)
        base = request

        last: OperationReceipt | None = None
        for attempt in range(self.MAX_RECOMPUTE):
            snapshot = self._snapshot(scope)
            cards = self._visible(scope, snapshot.cards)
            result = self.component.run_maintenance(MaintenanceRequest(
                cards=cards,
                all_cards=cards,
                known_ids=tuple(str(c.get("id") or "") for c in cards),
                mount=mount,
                locale=base.locale,
                ai_name=base.ai_name,
                user_name=base.user_name,
                recent_conversations=base.recent_conversations,
                # 账本优先用库里的；调用方显式给了才用它的（便于测试和迁移）。
                last_seed_card_count=(
                    base.last_seed_card_count
                    if base.last_seed_card_count
                    else int(ledger.get("seed_card_count") or 0)),
                last_signature=(base.last_signature
                                or str(ledger.get("signature") or "")),
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
            trace = dict(result.trace or {})
            receipt = self._apply(
                scope, mount, result.mutations,
                idempotency_key="", trace=trace,
                expected_revision=snapshot.revision,
                maintenance_state={
                    "mount": mount,
                    "signature": str(trace.get("signature") or ""),
                    "seed_card_count": int(trace.get("seed_card_count")
                                           or len(cards)),
                    "schema_version": 1,
                },
            )
            if receipt.error != "revision_conflict":
                return receipt
            last = receipt
            last.trace = {**last.trace, "recompute_attempt": attempt + 1}
        return last or OperationReceipt(error="revision_conflict")

    def maintenance_ledger(self, scope: Scope, *, mount: str | None = None) -> dict:
        """上一次整理留下的账本。存储没实现就返回空 —— 那种情况下整理会
        保守地重跑，重复但不会丢东西。"""
        target = scope.check(mount or DEFAULT_MOUNT)
        read = getattr(self._store, "maintenance_state", None)
        if read is None:
            return {}
        try:
            return dict(read(scope.tenant_id, owner=scope.owner(),
                             mount=target) or {})
        except Exception:  # noqa: BLE001 —— 读不到账本不该让整理彻底失败
            return {}

    # -- 历史导入 ---------------------------------------------------------- #

    #: 一批多少字。够模型一次读完，也够小到中断时不心疼。
    IMPORT_BATCH_CHARS = 6000

    def import_history(self, scope: Scope, request: Any, *,
                       progress: Any = None, max_batches: int | None = None):
        """把一大批过去的材料**分批**蒸成卡，可断点续跑。

        ``progress`` 传上一次返回的那个对象就从断点继续；不传就从头开始。
        ``max_batches`` 限制这一次最多跑几批 —— 宿主可以跑一小段就把进度
        交还给用户（显示百分比），下次接着来。

        ## 为什么必须串行

        第 N 批做判断时，前 N-1 批写进去的卡就在它的「已有记忆索引」里，
        模型于是会选 merge 而不是 add —— **跨批去重靠的是这个**，不靠额外
        状态。并行跑的话每批看到的都是导入前的旧状态，同一件事在不同批里
        各写一张，谁也不知道。
        """
        from .contracts import CaptureRequest
        from .importing import ImportProgress, batch_key, split_material

        material = str(getattr(request, "material", "") or "")
        prog = progress or ImportProgress(total=len(material))
        prog.total = len(material)
        mount = scope.check(getattr(request, "mount", None) or DEFAULT_MOUNT)
        policy = getattr(request, "policy", None) or "history_import"
        base_key = str(getattr(request, "idempotency_key", "") or "")
        if not base_key:
            # 没给稳定键就从内容算一个 —— 至少同一份材料重跑不会写两遍。
            import hashlib
            base_key = "import-" + hashlib.sha256(
                material.encode("utf-8")).hexdigest()[:16]

        batches = [(off, chunk)
                   for off, chunk in split_material(
                       material, batch_chars=self.IMPORT_BATCH_CHARS)
                   if off >= prog.cursor]
        if max_batches:
            batches = batches[:max_batches]

        for offset, chunk in batches:
            receipt = self.capture_and_store(scope, CaptureRequest(
                window=chunk,
                mount=mount,
                locale=getattr(request, "locale", "") or "",
                ai_name=getattr(request, "ai_name", "") or "",
                user_name=getattr(request, "user_name", "") or "",
                policy=policy,
                idempotency_key=batch_key(base_key, offset=offset, chunk=chunk),
            ))
            if receipt.error:
                # 🔴 失败就**停在这里**，游标不动。继续往下跑的话，后面几批
                # 看不到这一批本该写进去的卡，会把同一件事再记一遍；
                # 而游标推过去了，这一批永远不会被重试。
                prog.failed.append({"offset": offset, "error": receipt.error})
                break
            prog.cursor = offset + len(chunk)
            prog.batches_done += 1
            if receipt.written:
                prog.cards_written += len(receipt.record_ids)
            else:
                # 空结果**不是失败** —— 某一批确实没什么可记是正常的，
                # 游标照常推进。但要记下来，否则「导入完什么都没有」时
                # 分不清是材料没内容还是我们漏读了。
                prog.skipped.append({"offset": offset,
                                     "reason": receipt.reason or "empty"})
        return prog

    # -- 用户明说要记 ------------------------------------------------------- #

    def write_one(self, scope: Scope, request: Any) -> OperationReceipt:
        """用户明说要记的一件事 —— 不做「值不值得」的判断，直接落库。"""
        result = self.component.write_one(request)
        if result.error:
            return OperationReceipt(error=result.error,
                                    trace=dict(result.trace or {}))
        if not result.mutations:
            return OperationReceipt(reason="nothing_worth_keeping",
                                    trace=dict(result.trace or {}))
        return self._apply(
            scope, scope.check(getattr(request, "mount", None) or DEFAULT_MOUNT),
            result.mutations,
            idempotency_key=str(getattr(request, "idempotency_key", "") or ""),
            trace=dict(result.trace or {}))

    # -- 换挂载点 / 升级老卡 ------------------------------------------------ #

    def promote(self, scope: Scope, request: Any) -> OperationReceipt:
        """把一张卡换到另一个挂载点（比如私密 → 家庭共享）。

        🔴 ``to_mount`` 也要过 ``scope.check`` —— 调用方无权访问的挂载点，
        不能通过「把卡提升过去」绕进去。而且 ``authorized`` 必须为真：
        这是用户的授权，不是模型能决定的事。
        """
        target = scope.check(str(getattr(request, "to_mount", "") or ""))
        if not getattr(request, "authorized", False):
            return OperationReceipt(error="not_authorized")
        record_id = str(getattr(request, "record_id", "") or "").strip()
        if not record_id:
            return OperationReceipt(error="record_id_required")
        visible = {str(c.get("id") or "")
                   for c in self._readable_cards(scope, include_archived=True)}
        if record_id not in visible:
            return OperationReceipt(error="record_not_found")
        # 换挂载点走专门的 op，不走 update —— update 明确禁止改 mount，
        # 因为那条路绕过了这里的授权检查。
        return self._apply(scope, target, [{
            "op": "promote", "record_id": record_id, "to_mount": target,
            "reason": str(getattr(request, "reason", "") or ""),
        }], idempotency_key=f"promote:{record_id}:{target}", trace={
            "promoted_to": target, "reason": str(getattr(request, "reason", ""))})

    def migrate_and_store(self, scope: Scope, request: Any) -> OperationReceipt:
        """把一批老格式的卡升级成当前形状并写回。"""
        result = self.component.migrate(request)
        if getattr(result, "error", None):
            return OperationReceipt(error=result.error,
                                    trace=dict(getattr(result, "trace", {}) or {}))
        mutations = list(getattr(result, "mutations", []) or [])
        if not mutations:
            return OperationReceipt(reason="nothing_to_migrate",
                                    trace=dict(getattr(result, "trace", {}) or {}))
        return self._apply(
            scope, scope.check(getattr(request, "mount", None) or DEFAULT_MOUNT),
            mutations, idempotency_key="", trace=dict(getattr(result, "trace", {}) or {}))

    # -- 用户主动删除 ------------------------------------------------------ #

    def delete_record(
        self, scope: Scope, record_id: str, *,
        requested_by: str, reason: str = "",
    ) -> OperationReceipt:
        """**用户要求删除 → 真删。**

        和整理时的 archive/supersede 是两回事：那两个是编辑，内容还在、
        可追溯；这个是删除，正文不再可读。混在一起的后果是界面说「已删除」
        而库里原封不动 —— 当着用户的面撒谎。

        ``requested_by`` 必填：删除必须能追溯到是谁要求的。
        """
        target = str(record_id or "").strip()
        if not target:
            return OperationReceipt(error="record_id_required")
        if not str(requested_by or "").strip():
            return OperationReceipt(error="requested_by_required")
        mount = scope.check(DEFAULT_MOUNT)
        # 只能删自己作用域里看得见的那张 —— 否则给个别人的 id 就能删别人的卡。
        visible = {str(c.get("id") or "")
                   for c in self._readable_cards(scope, include_archived=True)}
        if target not in visible:
            return OperationReceipt(error="record_not_found")
        return self._apply(scope, mount, [{
            "op": "delete", "record_id": target,
            "requested_by": str(requested_by), "reason": str(reason or ""),
        }], idempotency_key=f"delete:{target}", trace={})

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

    #: 一页最多多少条。**浏览和导出必须有界** —— 不分页的 export 在几万张卡的
    #: 花园上会把整座花园塞进一条响应，宿主那边直接 OOM。
    DEFAULT_PAGE = 100
    MAX_PAGE = 1000

    def browse(self, scope: Scope, *, include_archived: bool = False,
               limit: int | None = None, cursor: str = ""):
        cards = self._readable_cards(scope, include_archived=include_archived)
        page, next_cursor = _paginate(cards, limit, cursor,
                                      self.DEFAULT_PAGE, self.MAX_PAGE)
        return Page(items=self.component.browse(page), next_cursor=next_cursor,
                    total=len(cards))

    def export(self, scope: Scope, *, include_archived: bool = True,
               limit: int | None = None, cursor: str = ""):
        from .contracts import ExportRequest

        cards = self._readable_cards(scope, include_archived=include_archived)
        page, next_cursor = _paginate(cards, limit, cursor,
                                      self.DEFAULT_PAGE, self.MAX_PAGE)
        return Page(
            items=self.component.export(ExportRequest(
                actor=scope.actor, mounts=scope.mounts(),
                include_archived=include_archived,
            ), page),
            next_cursor=next_cursor, total=len(cards))

    # -- 内部 ------------------------------------------------------------- #

    #: 冲突后最多重算几次。**必须有界** —— 无界重算在高并发下会把额度烧光，
    #: 而且每一次重算都要调一次模型。到顶了就如实返回 conflict，
    #: 绝不能对用户说「记住了」。
    MAX_RECOMPUTE = 3

    def _capture_with_cas(self, scope: Scope, mount: str,
                          request: CaptureRequest, judge) -> OperationReceipt:
        """load → 判断 → CAS 提交；冲突就**重读重算**，不是重放旧结果。

        重放旧 mutation 是最诱人也最错的做法：那批 mutation 是基于旧快照算的，
        里面的 target_id 可能已经被别人 supersede 掉了，去重结论也可能失效。
        重放的结果是「凭空多出一张重复卡」或「supersede 一张不存在的卡」。
        """
        last: OperationReceipt | None = None
        for attempt in range(self.MAX_RECOMPUTE):
            prepared, revision = self.prepare_capture(scope, request)
            result = judge(prepared)
            if getattr(result, "error", None):
                return OperationReceipt(error=result.error,
                                        trace=dict(result.trace or {}))
            if not getattr(result, "mutations", None):
                return OperationReceipt(reason="nothing_worth_keeping",
                                        trace=dict(result.trace or {}))
            receipt = self._apply(
                scope, mount, result.mutations,
                idempotency_key=request.idempotency_key,
                trace=dict(result.trace or {}),
                expected_revision=revision,
            )
            if receipt.error != "revision_conflict":
                return receipt
            last = receipt
            last.trace = {**last.trace, "recompute_attempt": attempt + 1}
        # 重算到顶还在冲突 —— 如实报出去。调用方可以退避后再来，
        # 但**不能**把这当成成功。
        return last or OperationReceipt(error="revision_conflict")

    def _snapshot(self, scope: Scope, *, include_archived: bool = False):
        snapshot = self._store.load(scope.tenant_id, owner=scope.owner(),
                                    include_archived=include_archived)
        # 存储把 owner 过滤漏掉时，这里是唯一能当场发现的地方 ——
        # 漏过滤的表现是读到别人的卡，不会有任何异常。
        got = str(getattr(snapshot, "owner", "") or "")
        if got and got != scope.owner():
            raise MountPermissionError(
                f"store returned a snapshot for owner {got!r}, "
                f"expected {scope.owner()!r}")
        return snapshot

    def _visible(self, scope: Scope, cards: list[dict]) -> list[dict]:
        allowed = set(scope.mounts())
        # 没写 mount 的卡按默认 mount 处理 —— 老数据没有这个字段。
        return [c for c in cards
                if str(c.get("mount") or DEFAULT_MOUNT) in allowed]

    def _missing_capabilities(self, needed: set[str]) -> set[str]:
        """存储支持不了的那些能力。

        ## 🔴 取不到声明 = 全部当作不支持（fail closed）

        以前这里反过来：没有 ``capabilities()``、或者调用抛异常，就当成
        「全部支持」。那正好错在最危险的方向 —— 一个不声明能力的适配器会被
        当成什么都能做，于是 supersede 被下发给一个只会覆盖的后端，
        「永远不硬删」这条红线在没有任何报错的情况下破掉。

        而 :mod:`memgarden.storage` 从一开始就写着「``Capabilities`` 没有默认值，
        外部适配器要逐项写清楚」。这里必须和那条约束一致，否则声明的强制性
        被这一个 fallback 全部抵消。
        """
        declare = getattr(self._store, "capabilities", None)
        if declare is None:
            return set(needed)
        try:
            caps = declare()
        except Exception:  # noqa: BLE001
            return set(needed)
        out = set()
        for name in needed:
            flag = getattr(caps, f"supports_{name}", None)
            if flag is None:
                flag = getattr(caps, name, None)
            # None（没有这个字段）也算不支持 —— 「没声明」不是「支持」。
            if not flag:
                out.add(name)
        return out

    def _readable_cards(
        self, scope: Scope, *, include_archived: bool = False
    ) -> list[dict]:
        """这个作用域能看见的卡。

        **过滤在这里做，不在调用方**。放给调用方做的话，25 个接入点就有 25 种
        理解，而漏掉一处的表现是「读到了别人的记忆」——不会报错。
        """
        snapshot = self._snapshot(scope, include_archived=include_archived)
        return self._visible(scope, snapshot.cards)

    def _apply(
        self, scope: Scope, mount: str, mutations: list[dict], *,
        idempotency_key: str, trace: dict,
        expected_revision: Any = None,
        maintenance_state: dict | None = None,
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
                owner=scope.owner(),
                idempotency_key=idempotency_key or _digest_key(scope, stamped),
                # 🔴 基于旧状态做的判断**必须**带着读到的 revision 回来。
                # 以前这里写死 None —— Store 支持 CAS，主链路却从不使用，
                # 于是并发下后写的那次会盖掉先写的判断，且不报错。
                expected_revision=expected_revision,
                maintenance_state=maintenance_state,
            )
        except RevisionConflict as exc:
            # 让调用方重读重算,而不是覆盖别人刚写的东西。
            return OperationReceipt(error="revision_conflict", trace={
                **trace, "detail": str(exc)})
        except IdempotencyConflict as exc:
            # 同键不同内容 —— 调用方的键生成有 bug。单独一个码，
            # 因为处置和版本冲突完全不同：这个重试多少次都一样。
            return OperationReceipt(error="idempotency_conflict", trace={
                **trace, "detail": str(exc)})
        except PartialFailure as exc:
            # 🔴 **绝不能报成写入成功**，也不能报成什么都没写。
            # 回执里带上分界线，调用方才能只重放剩下的那部分 ——
            # 当成全失败去重试会把已落库的那几条写第二遍。
            return OperationReceipt(
                error="partial_failure",
                record_ids=tuple(str(r.get("id") or "") for r in exc.applied
                                 if r.get("id")),
                trace={**trace, "applied": len(exc.applied),
                       "failed_at": exc.failed_at, "detail": str(exc)[:200]})
        except MutationRejected as exc:
            # 目标不在了、改了不该改的字段 —— 格式对但做不到。
            return OperationReceipt(error=f"mutation_rejected:{exc}", trace=trace)
        except Exception as exc:  # noqa: BLE001
            # 🔴 存储自己炸了（磁盘满、连接断、适配器有 bug）也必须变成回执。
            # 让它原样冒出去的话，一次写库失败会把整轮对话带走 —— 而记忆
            # 写不进去**不该影响用户能不能继续聊**。
            #
            # 更要紧的是账本：整理路径上如果异常直接穿出去，调用方分不清
            # 「没整理」和「整理了但没写成」，而这两件事的后续处置相反。
            return OperationReceipt(
                error=f"storage_failed:{type(exc).__name__}",
                trace={**trace, "detail": str(exc)[:200]})
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
        [scope.tenant_id, scope.owner(), mutations], sort_keys=True,
        ensure_ascii=False, default=str,
    )
    return "auto-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _paginate(cards: list[dict], limit: int | None, cursor: str,
              default: int, maximum: int) -> tuple[list[dict], str]:
    """按稳定顺序切一页出来。

    游标用的是**卡的 id**，不是下标：下标游标在翻页途中有卡被删掉时会跳过
    一条，而那一条从此不会出现在任何一页里 —— 导出「成功」了，内容少一张。

    id 找不到时（那张卡在翻页途中被删了）从头开始，宁可重复一页也不跳过。
    """
    size = int(limit) if limit else default
    size = max(1, min(size, maximum))
    ordered = sorted(cards, key=lambda c: str(c.get("id") or ""))
    start = 0
    if cursor:
        ids = [str(c.get("id") or "") for c in ordered]
        if cursor in ids:
            start = ids.index(cursor) + 1
    page = ordered[start:start + size]
    nxt = ""
    if start + size < len(ordered) and page:
        nxt = str(page[-1].get("id") or "")
    return page, nxt
