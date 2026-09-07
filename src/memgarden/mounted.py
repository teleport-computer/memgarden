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


class MaintenanceStorageError(RuntimeError):
    """Store 没有可靠提供整理账本；此时必须停止整理，不能假装空账本。"""


class StorageCapabilityError(RuntimeError):
    """Store 缺少当前读写路径不可降级的正确性能力。"""


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
    error: str | None = None
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
        self, scope: Scope, request: CaptureRequest,
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
        target_mount = scope.check(request.mount or DEFAULT_MOUNT)
        snapshot = self._snapshot(scope)
        cards = [
            card for card in self._visible(scope, snapshot.cards)
            if str(card.get("mount") or DEFAULT_MOUNT) == target_mount
        ]
        prepared = replace(
            request,
            # actor 只能来自 Runtime 的可信 Scope，不能接受模型/调用参数覆盖。
            actor=scope.actor,
            mount=target_mount,
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

    def prepare_maintenance(
        self, scope: Scope, request: MaintenanceRequest
    ) -> tuple[MaintenanceRequest, Any]:
        """装配一次 store-aware 整理请求，并返回对应快照版本。

        ``cards`` 只含当前 active 卡；``all_cards`` 必须同时含 archived 与
        superseded，水位线才会只增不减。账本也在每次准备时重读，CAS 冲突后
        重新判断不会继续沿用旧账本。
        """
        mount = scope.check(request.mount or DEFAULT_MOUNT)
        self._require_maintenance_storage()
        snapshot = self._snapshot(
            scope, include_archived=True, include_superseded=True)
        all_visible = [
            c for c in self._visible(scope, snapshot.cards)
            if str(c.get("mount") or DEFAULT_MOUNT) == mount
        ]
        active = [
            c for c in all_visible
            if not c.get("archived") and not c.get("superseded_by")
            and str(c.get("lifecycle") or "active") == "active"
        ]
        ledger = self.maintenance_ledger(scope, mount=mount)
        generations = getattr(snapshot, "seed_generations", {}) or {}
        seed_rows = sum(
            str(card.get("source") or "") != "memory_dream"
            for card in all_visible)
        if seed_rows and mount not in generations:
            raise MaintenanceStorageError(
                "Storage Snapshot 缺少该 mount 的只增 seed_generation；"
                "hard delete 后无法可靠计算新增量")
        if int(generations.get(mount, 0)) < seed_rows:
            raise MaintenanceStorageError(
                "Storage Snapshot 的 seed_generation 小于现存 seed 数，"
                "水位不满足只增契约")
        return replace(
            request,
            actor=scope.actor,
            mount=mount,
            cards=active,
            all_cards=all_visible,
            known_ids=tuple(str(c.get("id") or "") for c in active),
            current_seed_generation=int(generations.get(mount, 0)),
            last_seed_card_count=(
                request.last_seed_card_count
                if request.last_seed_card_count
                else int(ledger.get("seed_card_count") or 0)
            ),
            last_signature=(request.last_signature
                            or str(ledger.get("signature") or "")),
        ), snapshot.revision

    def check_maintenance(
        self, scope: Scope, *, mount: str | None = None
    ) -> MaintenanceCheck:
        """要不要整理。不调模型；账本不可用时明确失败，不重复整理。"""
        try:
            prepared, _ = self.prepare_maintenance(
                scope, MaintenanceRequest(mount=mount or DEFAULT_MOUNT, dry_run=True))
        except MaintenanceStorageError as exc:
            return MaintenanceCheck(reason="maintenance_state_unavailable",
                                    error="storage_failed:maintenance_state",
                                    trace={"detail": str(exc)})
        result = self.component.run_maintenance(prepared)
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

        last: OperationReceipt | None = None
        for attempt in range(self.MAX_RECOMPUTE):
            try:
                prepared, revision = self.prepare_maintenance(scope, request)
            except MaintenanceStorageError as exc:
                return OperationReceipt(error="storage_failed:maintenance_state",
                                        trace={"detail": str(exc)})
            result = self.component.run_maintenance(prepared)
            receipt = self.store_maintenance_result(
                scope, prepared, result, expected_revision=revision)
            if receipt.error != "revision_conflict":
                return receipt
            last = receipt
            last.trace = {**last.trace, "recompute_attempt": attempt + 1}
        return last or OperationReceipt(error="revision_conflict")

    def store_maintenance_result(
        self, scope: Scope, request: MaintenanceRequest, result: Any, *,
        expected_revision: Any = None,
    ) -> OperationReceipt:
        """提交一次已经完成的整理判断，供内置循环与 host-driven 共用。"""
        try:
            self._require_maintenance_storage()
        except MaintenanceStorageError as exc:
            return OperationReceipt(error="storage_failed:maintenance_state",
                                    trace={"detail": str(exc)})
        trace = dict(getattr(result, "trace", {}) or {})
        if getattr(result, "error", None):
            return OperationReceipt(error=result.error, trace=trace)
        if not getattr(result, "needed", False):
            return OperationReceipt(reason="not_needed", trace=trace)

        mutations = list(getattr(result, "mutations", []) or [])
        # 模型判断“该整理”但没有可合并项时，也要原子推进账本；否则每次 check
        # 都会再次触发同一批昂贵模型调用。no_op 不改卡，只和账本一起提交。
        if not mutations:
            mutations = [{"op": "no_op", "reason": "nothing_to_consolidate"}]
        signature = str(trace.get("signature") or "")
        seed_count = int(trace.get("seed_card_count") or 0)
        mount = scope.check(request.mount or DEFAULT_MOUNT)
        # 调用方给的是一次整理流程的稳定前缀，不是跨所有花园状态复用的
        # 最终 Store key。同一个前缀遇到新快照时必须形成新键；否则两次都产出
        # no_op 时 Store 会把第二次误判成第一次的重放，账本永远不再推进。
        key_prefix = request.idempotency_key or "maintenance"
        store_key = f"{key_prefix}:{mount}:{signature}"
        return self._apply(
            scope, mount, mutations,
            idempotency_key=store_key,
            trace=trace,
            expected_revision=expected_revision,
            maintenance_state={
                "mount": mount,
                "signature": signature,
                "seed_card_count": seed_count,
                "schema_version": 1,
            },
        )

    def maintenance_ledger(self, scope: Scope, *, mount: str | None = None) -> dict:
        """上一次整理留下的账本。缺失或读取失败时 fail closed。"""
        target = scope.check(mount or DEFAULT_MOUNT)
        read = getattr(self._store, "maintenance_state", None)
        if read is None:
            raise MaintenanceStorageError(
                "StoragePort 缺少 maintenance_state；无法安全运行 Maintenance")
        try:
            return dict(read(scope.tenant_id, owner=scope.owner(),
                             mount=target) or {})
        except Exception as exc:  # noqa: BLE001
            raise MaintenanceStorageError(
                f"读取 maintenance_state 失败: {type(exc).__name__}: {exc}") from exc

    def _require_maintenance_storage(self) -> None:
        try:
            store_caps = self._store.capabilities()
        except Exception as exc:  # noqa: BLE001
            raise MaintenanceStorageError(
                f"读取 Storage capabilities 失败: {type(exc).__name__}: {exc}"
            ) from exc
        missing = [name for name in (
            "supports_owner_scoping",
            "supports_supersede",
            "supports_atomic_batch",
            "supports_maintenance_state",
            "supports_monotonic_seed_generation",
        ) if not getattr(store_caps, name, False)]
        if missing:
            raise MaintenanceStorageError(
                "Storage 未声明 Maintenance 正确性能力: " + ", ".join(missing))

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

        import hashlib
        import json

        material = str(getattr(request, "material", "") or "")
        mount = scope.check(getattr(request, "mount", None) or DEFAULT_MOUNT)
        policy = getattr(request, "policy", None) or "history_import"
        batch_card_limit = max(
            1, int(getattr(request, "max_cards", 50) or 50))
        source_digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        fingerprint_payload = [
            "history-import-v1", source_digest, scope.tenant_id, scope.owner(),
            mount, str(getattr(request, "locale", "") or ""), policy,
            str(getattr(request, "material_kind", "") or ""),
            str(getattr(request, "ai_name", "") or ""),
            str(getattr(request, "user_name", "") or ""),
            str(getattr(request, "idempotency_key", "") or ""),
            batch_card_limit, self.IMPORT_BATCH_CHARS,
        ]
        import_fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        prog = progress or ImportProgress(total=len(material),
                                          source_digest=source_digest,
                                          import_fingerprint=import_fingerprint)
        if prog.cursor < 0 or prog.cursor > len(material):
            raise ValueError("history import progress.cursor 超出材料范围")
        if prog.source_digest and prog.source_digest != source_digest:
            raise ValueError(
                "history import progress 属于另一份材料；请从新进度开始导入")
        if not prog.source_digest and prog.cursor:
            raise ValueError(
                "旧 history import progress 没有 source_digest，无法证明它属于"
                "当前材料；请从头重启（稳定 batch 幂等键会防止重复落卡）")
        if prog.import_fingerprint and prog.import_fingerprint != import_fingerprint:
            raise ValueError(
                "history import progress 的 scope/mount/locale/policy/来源/幂等/批次规则"
                "与当前请求不同；不能安全续传")
        if not prog.import_fingerprint and prog.cursor:
            raise ValueError(
                "旧 history import progress 没有 import_fingerprint，无法验证"
                "当前导入语义；请从头重启")
        prog.source_digest = source_digest
        prog.import_fingerprint = import_fingerprint
        prog.total = len(material)
        # 同一正文换 mount/policy 也是另一条导入，幂等键必须绑定完整语义。
        key_prefix = str(getattr(request, "idempotency_key", "") or "import")
        base_key = f"{key_prefix}:{import_fingerprint[:16]}"

        all_batches = split_material(
            material, batch_chars=self.IMPORT_BATCH_CHARS)
        # cursor 是不可信的持久输入，只接受本实现曾经返回过的位置。任意落在
        # 某批中间的 cursor 会静默跳过该批前半段；落在空白洞里则可能永远卡住。
        valid_cursors = {0, len(material)}
        valid_cursors.update(off + len(chunk) for off, chunk in all_batches)
        if prog.cursor not in valid_cursors:
            raise ValueError(
                "history import progress.cursor 不是合法批次边界；请使用服务"
                "上次原样返回的 progress")
        batches = [(off, chunk) for off, chunk in all_batches
                   if off >= prog.cursor]
        if max_batches is not None and int(max_batches) < 1:
            raise ValueError("max_batches 必须至少为 1")
        if max_batches:
            batches = batches[:max_batches]

        # 全空白输入不会生成 batch，但它已经被完整消费；cursor 必须走到末尾，
        # 否则 progress.done 永远为 False，宿主会无限重试。
        if not material.strip():
            if material and not prog.skipped:
                prog.skipped.append({"offset": 0, "reason": "blank_material"})
            prog.failed.clear()
            prog.cursor = len(material)
            return prog

        for offset, chunk in batches:
            # 正在重试这一批时先移除它的旧失败记录。成功后 failed 应为空；
            # 旧实现只 append，导致一次临时失败后即使续传成功也永远 done=False。
            prog.failed[:] = [f for f in prog.failed
                              if int(f.get("offset", -1)) != offset]
            receipt = self.capture_and_store(scope, CaptureRequest(
                window=chunk,
                mount=mount,
                locale=getattr(request, "locale", "") or "",
                ai_name=getattr(request, "ai_name", "") or "",
                user_name=getattr(request, "user_name", "") or "",
                policy=policy,
                material_kind=str(getattr(request, "material_kind", "") or ""),
                source="history_import",
                max_cards=batch_card_limit,
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
        # split_material 故意不把纯空白批次送给模型。最后一批有效内容之后若
        # 还有尾随空白，需要把它也标成已消费；否则下一次没有 batch 可跑，
        # cursor 却永远小于 total。
        if not prog.failed and not any(
            off >= prog.cursor for off, _chunk in all_batches
        ):
            prog.cursor = len(material)
        return prog

    # -- 用户明说要记 ------------------------------------------------------- #

    def write_one(self, scope: Scope, request: Any) -> OperationReceipt:
        """用户明说要记的一件事 —— 不做「值不值得」的判断，直接落库。"""
        # actor 只能来自可信 Scope，不能信 wire/request 自报。
        prepared = replace(request, actor=scope.actor)
        result = self.component.write_one(prepared)
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
        mount = scope.check(getattr(request, "mount", None) or DEFAULT_MOUNT)
        snapshot = self._snapshot(
            scope, include_archived=True, include_superseded=True)
        in_mount = {
            str(card.get("id") or "") for card in self._visible(scope, snapshot.cards)
            if str(card.get("mount") or DEFAULT_MOUNT) == mount
        }
        allowed = tuple(str(x) for x in getattr(request, "allowed_ids", ()) or ())
        missing = [record_id for record_id in allowed if record_id not in in_mount]
        if missing:
            return OperationReceipt(
                error="record_not_found",
                trace={"missing_ids": missing, "mount": mount})

        prepared = replace(request, actor=scope.actor, mount=mount)
        result = self.component.migrate(prepared)
        if getattr(result, "error", None):
            return OperationReceipt(error=result.error,
                                    trace=dict(getattr(result, "trace", {}) or {}))
        # MigrateResult 的正式输出叫 upgrades，不是 mutations。每项是已有卡的
        # 新字段，落到 Store 上必须明确转换成 update；此前读错属性导致所有
        # 成功迁移都回 nothing_to_migrate，manifest 却仍宣称支持。
        mutations = []
        for upgrade in list(getattr(result, "upgrades", []) or []):
            row = dict(upgrade)
            record_id = str(row.pop("id", "") or "").strip()
            if record_id:
                mutations.append({"op": "update", "record_id": record_id,
                                  "changes": row, "mount": mount})
        trace = {
            **dict(getattr(result, "trace", {}) or {}),
            "unmigrated_ids": list(
                getattr(result, "unmigrated_ids", []) or []),
        }
        if not mutations:
            return OperationReceipt(reason="nothing_to_migrate",
                                    trace=trace)
        return self._apply(
            scope, mount, mutations,
            idempotency_key=str(getattr(request, "idempotency_key", "") or ""),
            trace=trace, expected_revision=snapshot.revision)

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
        # 不在这里先做 existence preflight：第一次删除成功、回包丢失后，重试
        # 必须能命中 Store 的幂等回执。Store 自身按 owner 分区，所以直接提交
        # 不会碰到别人的卡；不存在的目标统一映射成 record_not_found。
        receipt = self._apply(scope, mount, [{
            "op": "delete", "record_id": target,
            "requested_by": str(requested_by), "reason": str(reason or ""),
        }], idempotency_key=f"delete:{target}", trace={})
        if (receipt.error or "").startswith("mutation_rejected:delete target not found"):
            receipt.error = "record_not_found"
        return receipt

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
                         "bucket": str(args.get("bucket") or ""),
                         "source": "model_tool"},
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

    def _snapshot(
        self, scope: Scope, *, include_archived: bool = False,
        include_superseded: bool = False,
    ):
        declare = getattr(self._store, "capabilities", None)
        try:
            caps = declare() if declare is not None else None
        except Exception as exc:  # noqa: BLE001
            raise StorageCapabilityError(
                f"读取 Storage capabilities 失败: {type(exc).__name__}: {exc}"
            ) from exc
        if not caps or not getattr(caps, "supports_owner_scoping", False):
            raise StorageCapabilityError(
                "Storage 未声明 supports_owner_scoping；不能安全读取 owner 数据")
        snapshot = self._store.load(scope.tenant_id, owner=scope.owner(),
                                    include_archived=include_archived,
                                    include_superseded=include_superseded)
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
            if str(row.get("op") or "add") in {"add", "supersede"}:
                # 卡的操作来源也必须由可信 Scope 覆盖。模型可以生成 card，
                # 但不能伪造“是谁写的”；Store 会把它作为普通明文字段保留。
                row["card"] = {
                    **dict(row.get("card") or {}),
                    "mount": row["mount"],
                    "source_actor": scope.actor.as_dict(),
                }
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

        # target 的当前 mount 也属于权限边界，不能只校验 mutation 顶层字段。
        # 模型可以猜中一个没展示给它的 ID；若 Store 在 owner 全桶里直接执行，
        # agent-private 请求就能改掉同 owner 的 shared 卡。
        try:
            target_snapshot = self._snapshot(
                scope, include_archived=True, include_superseded=True)
        except StorageCapabilityError as exc:
            return OperationReceipt(
                error="storage_lacks_capabilities:owner_scoping",
                trace={**trace, "detail": str(exc)[:200]})
        except Exception as exc:  # noqa: BLE001
            return OperationReceipt(
                error=f"storage_failed:{type(exc).__name__}",
                trace={**trace, "detail": str(exc)[:200]})
        if (expected_revision is not None
                and target_snapshot.revision != expected_revision):
            return OperationReceipt(error="revision_conflict", trace=trace)
        by_id = {str(card.get("id") or ""): card
                 for card in target_snapshot.cards}
        allowed_mounts = set(scope.mounts())
        has_targets = False
        for row in stamped:
            op = str(row.get("op") or "add")
            for target in _mutation_targets(row):
                has_targets = True
                current = by_id.get(target)
                if current is None:
                    continue  # Store 的幂等缓存或 not-found 语义负责。
                current_mount = str(current.get("mount") or DEFAULT_MOUNT)
                if current_mount not in allowed_mounts:
                    return OperationReceipt(error="record_not_found", trace=trace)
                if (op in {"update", "archive", "supersede"}
                        and current_mount != str(row.get("mount") or mount)):
                    return OperationReceipt(error="target_mount_mismatch", trace=trace)

        # 只要授权判断读过 target 状态，提交就必须绑定同一 revision；否则
        # authorize 与 apply 之间 target 可被移到未授权 mount（TOCTOU）。
        effective_revision = (expected_revision if expected_revision is not None
                              else target_snapshot.revision if has_targets
                              else None)

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
                expected_revision=effective_revision,
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


def _mutation_targets(mutation: dict) -> tuple[str, ...]:
    op = str(mutation.get("op") or "add")
    if op not in {"update", "archive", "supersede", "delete", "promote"}:
        return ()
    values = [mutation.get("record_id"), mutation.get("target_id")]
    values.extend(list(mutation.get("target_ids") or ()))
    return tuple(dict.fromkeys(
        str(value).strip() for value in values if str(value or "").strip()))


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
