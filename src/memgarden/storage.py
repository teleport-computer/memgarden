"""存储 port —— 内核只对着这个接口说话，实现由调用方注入。

## 为什么后端不只是数据库

同一个内核可能挂在**任何**记忆系统上（关系库、文档库、mem0 / engram、
使用者自己的实现）。对方有自己的格式和规矩，不一定支持我们的全部操作。
会真撞上的一例：

    内核：把这三张旧卡标记为「被取代」（保留链条，不删）
    关系库适配器：好，改个状态字段
    某个外部记忆库：我没有「被取代」这个概念，只能删掉或覆盖内容

全链路按**明文**设计。传输和磁盘层面的安全由部署环境决定，不进入这里的
字段、签名或能力声明。

所以接口留一个口子：**适配器声明自己支持哪些能力**。这条现在定成本几乎为零；
等适配器都写完再改，全部要返工。

## 能力分两类，不能一视同仁

这是 codex code_review（2026-08-14）纠正的一处：原实现把所有缺失能力都当成
「可降级」，写条日志继续跑。但其中两项缺了**没有正确的降级路径**：

    缺 supersede    → 降级成「覆盖旧卡」会破坏「永远不硬删」这条红线，
                      前后链条丢了就不可追溯，写日志救不回来
    缺 atomic_batch → 降级成「逐条写」会留下半完成状态（两张 active 卡，
                      或旧卡已退休而新卡没写成），这是数据损坏不是体验下降

所以它们是**正确性前置条件**：缺了就拒绝对应操作，而不是降级。
另外两项（custom_fields / metadata_sort）缺了只是能力退化，可以降级但必须上报。

## 声明必须显式

``Capabilities`` 的原始六项没有默认值；后加的 Maintenance 两项默认 False，
兼容旧构造形状但仍 fail closed。原实现默认全 True 会把漏声明当成「全支持」，
正好错在最危险的方向。
官方参考实现用 ``FULL_CAPABILITIES``。

## 现状

官方提供 ``InMemoryStore`` 与 ``SqliteStore`` 两个完整参考实现。外部 Store
实现这个 Port 后，可直接复用 MountedGarden 的权限、生命周期、CAS、幂等、
mutation 校验、整理账本和分页语义，不需要再抄一份业务编排。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Capabilities:
    """一个存储后端能做什么。

    原始六项没有默认值，防止适配器漏写时被当成支持。后来增加的两项
    Maintenance 能力默认 False：旧适配器升级时不会因构造参数变化直接崩溃，
    但必须主动声明 True 才会开启整理，仍然是 fail closed。
    """

    supports_supersede: bool
    """能不能「标记为被取代」而不是删掉。

    ⚠️ **正确性前置条件，不可降级。** 做梦消矛盾、capture 的 supersede 都依赖它。
    缺了只能拒绝该操作 —— 降级成覆盖会破坏「永远不硬删」，链条丢了不可追溯。
    """

    supports_atomic_batch: bool
    """能不能把一批 mutation 当作一个原子单位。

    ⚠️ **正确性前置条件，不可降级。**「写新卡 + 标记旧卡」必须一起成或一起败。
    缺了只能拒绝复合 mutation —— 逐条写会留下半完成状态。
    """

    supports_custom_fields: bool
    """能不能原样保留 bucket / threads 这些自定义字段。

    可降级：塞进对方的 metadata 或正文，检索与展示会退化但数据不损坏。
    """

    supports_metadata_sort: bool
    """能不能按元数据（重要度 / 时间 / 状态）排序并分页。

    可降级：把候选拉回本地再排，量大时延迟和内存变差但结果正确。
    """

    supports_hard_delete: bool
    """能不能真删 —— 用户主动删除、合规删除。

    ⚠️ **正确性前置条件，不可降级。** 用户说「把这条忘掉」时，降级成归档
    等于**当着用户的面撒谎**：界面说删了，库里还在。缺了只能拒绝 delete，
    让宿主把「这个后端不支持真删」如实告诉用户。

    以前这一项只出现在 ``OPTIONAL_CONTRACT`` 名单里、``Capabilities`` 上却没有
    字段，于是 ``getattr(caps, "supports_hard_delete", None)`` 恒为 ``None``，
    检查永远通不过也永远不报错 —— fail-open 正好错在最危险的方向。
    """

    supports_owner_scoping: bool
    """能不能在**查询层**把结果限制在一个 memory owner 内。

    ⚠️ **正确性前置条件，不可降级。** 降级成「把整个租户的卡load回来再在
    内存里按 owner 过滤」看起来等价，实际不是：漏过滤一处就是越权读，
    而且不报错；量大时还会把别人的数据整批读进本进程内存。
    """

    supports_maintenance_state: bool = False
    """能不能持久化 Maintenance 账本，并与卡 mutation 原子提交。"""

    supports_monotonic_seed_generation: bool = False
    """能不能按 mount 提供只增不减的原始卡计数。

    Maintenance 用它区分「从未处理过的新卡」与仍然存在的净卡数。没有它时，
    hard delete 会把水位倒退，后续新卡可能被静默漏掉，因此只能关闭整理能力。
    """


#: 全部支持 —— 官方参考实现和大多数关系库适配器都能达到这一档。
FULL_CAPABILITIES = Capabilities(
    supports_supersede=True,
    supports_atomic_batch=True,
    supports_custom_fields=True,
    supports_metadata_sort=True,
    supports_hard_delete=True,
    supports_owner_scoping=True,
    supports_maintenance_state=True,
    supports_monotonic_seed_generation=True,
)

#: 缺了就必须拒绝对应操作的能力（不是「降级后继续」）。
CORRECTNESS_CRITICAL = frozenset({
    "supports_supersede", "supports_atomic_batch",
    "supports_hard_delete", "supports_owner_scoping",
    "supports_maintenance_state", "supports_monotonic_seed_generation",
})


# --------------------------------------------------------------------------- #
# 契约分层：必需 vs 可选
# --------------------------------------------------------------------------- #
#
# ## 为什么要分
#
# 契约测试如果把「按元数据排序」这类动作也算成必需，那么**一大批合法后端
# 永远过不了**—— 有的后端只当一个不透明的键值存储用，`importance` 这种字段
# 它根本不参与，排序发生在取回之后的另一层。
#
# 硬跑的结果是：50 个用例 30 个跳过 20 个通过，报告是绿的，**但什么也没证明**。
#
# 所以拆开：
#
#     必需   任何存储都做得到。做不到就不是一个合格的 Garden 存储，直接拒绝
#     可选   做不到很正常。**显式声明不支持**，由 Garden 在可证明正确时降级
#
# 关键区别在于「声明不支持」和「静默跳过」：前者是一条可读的信息
# （宿主知道这个后端不能排序，会把候选拉回本地排），后者是一个洞。

#: 必需能力 —— 缺任何一项，这个后端就不该被当作 Garden 存储。
REQUIRED_CONTRACT = (
    "stable_identity",   # id 稳定，且不会因为删除而回退撞号
    "write",             # 能写
    "read",              # 能读
    "get_by_id",         # 能按 id 精确取
    "idempotency",       # 同一个幂等键重放不写第二遍；同键不同内容要报冲突
    "revision_conflict", # 版本不匹配要拒绝，而不是闷头覆盖
    "atomic_batch",      # 一批改动要么全成要么全不成
)

#: 可选能力 —— 做不到就声明，Garden 会降级。
OPTIONAL_CONTRACT = (
    "metadata_sort",     # 按重要度/时间排序分页。不认识这些字段的后端 → 拉回本地排
    "metadata_filter",   # 按字段过滤
    "full_text_search",  # 全文检索
    "vector_search",     # 向量检索
    "custom_fields",     # 原样保留 bucket / threads
    "hard_delete",       # 真删（用户主动删除、合规删除）
)


def contract_report(caps: "Capabilities", *, optional: tuple[str, ...] = ()) -> dict:
    """一个后端满足契约到什么程度。

    返回内容无关的结构，供接入方在文档/CI 里展示。
    **不支持的可选能力是「声明」不是「失败」** —— 报告里分开列。
    """
    supported_optional = set(optional)
    return {
        "required": {
            name: bool(getattr(caps, f"supports_{name}", True))
            if hasattr(caps, f"supports_{name}") else True
            for name in REQUIRED_CONTRACT
        },
        "optional": {name: name in supported_optional for name in OPTIONAL_CONTRACT},
    }


@dataclass(frozen=True)
class Degradation:
    """一条能力退化：少了什么、退化成什么、代价是什么。

    只用于**可降级**的能力。正确性前置条件不产出 Degradation，
    而是让相应操作直接被 ``ensure_supported`` 拒绝。
    """

    capability: str
    fallback: str
    cost: str


def mutations_digest(mutations: list[dict]) -> str:
    """这批改动的内容指纹，用来判断「同一个幂等键送来的是不是同一批东西」。

    ``sort_keys`` 是必需的：同样的内容，dict 的键序不该算成两批改动 ——
    否则调用方换个 Python 版本、或者字段拼装顺序变了，重放就会误报冲突。

    摘要只用于比对，不参与存储内容，所以用 sha256 截断即可。
    """
    import hashlib

    blob = json.dumps(mutations, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def apply_digest(
    mutations: list[dict], maintenance_state: dict | None = None,
) -> str:
    """原子 apply 的完整内容指纹。

    整理账本和 mutations 是同一次提交的一部分，幂等摘要也必须覆盖两者；
    否则相同 no_op 配不同水位会误命中旧回执，账本静默不推进。
    非 Maintenance 写入维持旧摘要格式，避免普通已落库幂等键升级后失效。
    """
    if maintenance_state is None:
        return mutations_digest(mutations)
    return mutations_digest([{
        "mutations": mutations,
        "maintenance_state": maintenance_state,
    }])


class PartialFailure(RuntimeError):
    """一批改动里，**前几条已经落库、后面失败了**。

    ## 为什么必须有这个类型

    两个官方 Store 都是原子的，所以在它们上面永远不会抛这个。但契约是给
    **任意适配器**用的，而「原子批量」是一项可以声明不支持的能力
    （``supports_atomic_batch``）—— 做不到的后端就会产生这种状态。

    以前这种情况只能抛一个普通异常，调用方看到的是「这批失败了」，
    于是它有两个选择，而**两个都是错的**：

        当成全失败去重试  → 已经落库的那几条被写第二遍（重复的记忆卡）
        当成全成功        → 没落库的那几条永远丢了，且没人知道

    正确的做法是知道**分界线在哪**：``applied`` 里是已经生效的，
    ``failed_at`` 是断掉的位置。调用方据此只重放剩下的那部分。

    ⚠️ 抛这个之前，适配器应当**已经尽力回滚**。它表示的是「回滚不了、
    库里现在是半成品」，不是「我懒得回滚」。
    """

    def __init__(self, applied: list[dict], failed_at: int,
                 cause: str = "") -> None:
        self.applied = list(applied)
        self.failed_at = int(failed_at)
        self.cause = cause
        super().__init__(
            f"partial failure: {len(self.applied)} mutation(s) already applied, "
            f"failed at index {self.failed_at}"
            + (f": {cause}" if cause else "")
        )


class MutationRejected(ValueError):
    """这条改动在当前库状态下**做不了**（目标不存在、改了不该改的字段…）。

    和 ``UnknownMutation`` 分开：那个是「格式不认识」（调用方的 bug），
    这个是「格式对但做不到」（可能只是并发下目标已经没了）。两者的处置不同。

    以前这里抛的是裸 ``KeyError`` —— 适配器只能靠猜来判断该重试还是该报错。
    """


class RevisionConflict(RuntimeError):
    """``expected_revision`` 与库里当前版本不一致 —— 有人在你读到之后改过了。

    调用方应当重读快照、重新决策，**不要**盲目重试同一批改动：那批改动是基于
    过期状态算出来的。
    """

    def __init__(self, expected: str, current: str) -> None:
        self.expected, self.current = expected, current
        super().__init__(f"revision conflict: expected {expected}, now {current}")


class IdempotencyConflict(RuntimeError):
    """同一个幂等键，两次送来的**内容不一样**。

    幂等键的语义是「同一个请求重放，别写第二遍」。同 key 不同内容不是重放，
    是两个不同的请求撞了 key —— 多半是调用方的键生成有 bug（比如键里没带上
    这批改动的标识）。

    **这里必须报错，不能返回第一次的结果。** 静默返回旧结果会让第二批改动凭空
    消失，而调用方以为写成功了 —— 用户那边的表现是「说了话但没记住」，
    且没有任何错误可查。
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"idempotency key {key!r} was already used for a different set of mutations"
        )


@dataclass(frozen=True)
class UnsupportedOperation(RuntimeError):
    """缺少正确性前置条件时，对应操作被拒绝。"""

    capability: str
    operation: str
    why: str

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        return f"{self.operation} 需要 {self.capability}：{self.why}"


_DEGRADABLE_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "supports_custom_fields",
        "由 Store Adapter 把 bucket/threads 映射进其 metadata 或正文",
        "按桶/线索的检索与展示失效，做梦的归并判断拿不到结构信息",
    ),
    (
        "supports_metadata_sort",
        "把候选拉回本地再排序",
        "记忆量大时每轮选卡的延迟和内存变差",
    ),
)

_CRITICAL_REASONS: dict[str, str] = {
    "supports_hard_delete": (
        "降级成归档等于当着用户的面撒谎：界面说删了、库里还在"
    ),
    "supports_owner_scoping": (
        "降级成「全租户读回来再在内存里过滤」漏一处就是越权读，而且不报错"
    ),
    "supports_supersede": (
        "降级成覆盖会破坏「永远不硬删」——旧卡的前后链条丢失且不可追溯，"
        "写日志救不回来"
    ),
    "supports_atomic_batch": (
        "降级成逐条写会留下半完成状态：两张 active 卡，或旧卡已退休而新卡没写成"
    ),
    "supports_maintenance_state": (
        "缺少与卡 mutation 原子提交的持久账本，整理会重复执行或漏掉已完成状态"
    ),
    "supports_monotonic_seed_generation": (
        "缺少只增不减的原始卡水位，hard delete 后会静默漏掉后续新增卡"
    ),
}


def plan_degradations(caps: Capabilities) -> list[Degradation]:
    """算出这个后端要承受哪些**可接受的**能力退化。

    只覆盖 custom_fields / metadata_sort。正确性前置条件不在这里 ——
    那两项缺失时不是「降级运行」，而是相应操作必须被拒绝，见 ``ensure_supported``。

    **调用方必须把非空结果上报**（日志/指标/用户可见），不允许丢弃。
    """
    return [
        Degradation(capability=name, fallback=fallback, cost=cost)
        for name, fallback, cost in _DEGRADABLE_RULES
        if not getattr(caps, name)
    ]


def missing_critical(caps: Capabilities) -> list[str]:
    """列出缺失的正确性前置条件。非空即代表这个后端只能跑受限的操作集。"""
    return sorted(name for name in CORRECTNESS_CRITICAL if not getattr(caps, name))


def ensure_supported(caps: Capabilities, *, operation: str, requires: str) -> None:
    """在执行需要前置条件的操作前调用；不满足直接抛。

    例：做梦要 supersede 一批旧卡之前
    ``ensure_supported(caps, operation="dream.supersede", requires="supports_supersede")``
    """
    if getattr(caps, requires):
        return
    raise UnsupportedOperation(
        capability=requires,
        operation=operation,
        why=_CRITICAL_REASONS.get(requires, "该后端不支持这项能力"),
    )


def describe_capabilities(caps: Capabilities) -> str:
    """渲染成一段给工程看的说明（日志/诊断用），措辞偏技术。"""
    lines: list[str] = []
    critical = missing_critical(caps)
    if critical:
        lines.append("⚠️ 该后端缺少正确性前置条件，相关操作会被拒绝：")
        for name in critical:
            lines.append(f"  · {name}：{_CRITICAL_REASONS[name]}")
    degradations = plan_degradations(caps)
    if degradations:
        lines.append("该后端缺少以下能力，已降级运行：")
        for d in degradations:
            lines.append(f"  · {d.capability}：{d.fallback} —— 代价：{d.cost}")
    if not lines:
        return "该存储后端支持全部能力，无降级、无受限操作。"
    return "\n".join(lines)


# 给接入方看的话术：不出现字段名，说清楚「你会失去什么」。
_USER_FACING_CRITICAL: dict[str, str] = {
    "supports_hard_delete": (
        "这个记忆库不支持真正的删除，只能归档。"
        "为避免界面显示已删除而内容仍在，用户主动删除会被拒绝"
    ),
    "supports_owner_scoping": (
        "这个记忆库不能按记忆归属人限制查询范围。"
        "为避免读到同一账户下别人的私有记忆，记忆功能会被关闭"
    ),
    "supports_supersede": (
        "这个记忆库不支持「标记为被取代」，只能覆盖或删除。"
        "为避免记忆被不可追溯地改掉，整理记忆时的消矛盾会被跳过"
    ),
    "supports_atomic_batch": (
        "这个记忆库不支持把一批改动作为整体提交。"
        "为避免出现改了一半的状态，需要多步完成的整理（合并、取代）会被跳过"
    ),
    "supports_maintenance_state": (
        "这个记忆库不能原子保存整理进度，因此后台记忆整理会被关闭"
    ),
    "supports_monotonic_seed_generation": (
        "这个记忆库不能可靠区分新增记忆与已删除记忆，因此后台记忆整理会被关闭"
    ),
}

_USER_FACING_DEGRADED: dict[str, str] = {
    "supports_custom_fields": (
        "这个记忆库不能原样保存「桶」和「线索」，它们会被折叠进正文。"
        "按桶浏览和分类展示会不可用"
    ),
    "supports_metadata_sort": (
        "这个记忆库不能按重要度排序，需要每次把记忆全部取回本地再排。"
        "记忆变多之后回复会变慢"
    ),
}


def describe_for_user(caps: Capabilities) -> list[str]:
    """给**接入方**看的说明：接了这个记忆库，你会失去什么。

    与 ``describe_capabilities`` 的区别是措辞 —— 这里不出现
    ``supports_supersede`` 这类字段名，只讲后果。

    hx 2026-08-14 定：降级信息给用户看，让接入方知道自己失去了什么，
    而不是只写进日志。返回空列表代表「什么都没损失」，调用方可以不展示。
    """
    out: list[str] = []
    for name in missing_critical(caps):
        text = _USER_FACING_CRITICAL.get(name)
        if text:
            out.append(text)
    for d in plan_degradations(caps):
        text = _USER_FACING_DEGRADED.get(d.capability)
        if text:
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# 快照与写入结果（CAS 协议）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Snapshot:
    """一次读取的结果：这一批卡 + 它们的版本号。

    ``revision`` 是调用方做 CAS 的凭据 —— 基于这份快照算出来的 mutation
    必须带着它回来，否则并发写会基于过期快照覆盖别人刚写的卡。
    原实现只返回卡列表，调用方无从获得合法 token（codex code_review 2026-08-14）。
    """

    cards: list[dict]
    """这一批卡。全链路按**明文**设计。

    内核只要求：每张卡带得动打分需要的元数据（重要度 / 情绪强度 /
    最近被想起 / 状态）。正文长什么样、后端怎么存，内核不关心 ——
    这一层刻意不表达任何某一个宿主的内部格式，否则换个后端就不成立。
    """

    revision: Any

    #: 这份快照属于谁。调用方可以断言它和自己请求的 owner 一致 ——
    #: 存储实现把 owner 过滤漏掉时，这里是唯一能当场发现的地方。
    owner: str = ""
    #: 每个 mount 已接收过的原始 seed 总数，只增不减。Maintenance 用它计算
    #: 真正新增量，避免 hard delete 把计数缺口抵消。有 seed 卡却不提供对应
    #: mount 水位时 Maintenance 会 fail closed，不能静默退回净行数。
    seed_generations: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyResult:
    """一次写入的结果：每个动作的结局 + 写完之后的新版本号。

    ``revision`` 让调用方可以接着做下一次 CAS，不必重新 load。
    """

    results: list[dict] = field(default_factory=list)
    revision: Any = None


# --------------------------------------------------------------------------- #
# 存储 port
# --------------------------------------------------------------------------- #


@runtime_checkable
class StoragePort(Protocol):
    """内核对存储的全部要求。

    读侧返回的是卡本身（明文），内核在元数据上打分排序。要不要在传输或磁盘
    层面加密由部署环境决定，不进这个接口的字段和能力声明。

    写侧只有 ``apply`` 一个入口，因为「写新卡 + 标记旧卡」必须原子。
    不提供 save/update/delete 三个独立方法 —— 那样在并发下会丢卡
    （IO 现有实现用跨进程 advisory fence 包住整个 load→mutate→save）。
    """

    def capabilities(self) -> Capabilities:
        """声明这个后端支持哪些能力。每一项都要显式给。"""
        ...

    def load(self, tenant: str, *, owner: str, **filters: Any) -> Snapshot:
        """取出**该租户下该 owner** 的卡 + 版本号。

        🔴 ``owner`` 必须落到查询条件里，不能读回整个租户再由调用方过滤。
        两者的差别在出错时才看得见：漏一处过滤，前者读不到、后者读得到。

        ``revision`` 的作用域也必须是 ``(tenant, owner)`` —— 用全局或全租户的
        版本号会让两个互不相干的 owner 互相踢掉对方的 CAS，表现为
        「明明没人跟我抢，我的写入却一直冲突」。
        """
        ...

    def maintenance_state(self, tenant: str, *, owner: str, mount: str) -> dict:
        """读取 ``(tenant, owner, mount)`` 的整理账本。

        这是 Maintenance 正确性的必需契约，不是可选优化。账本必须和整理
        mutation 在 :meth:`apply` 的同一原子提交中更新；读不到时不能假装空账本
        继续跑，否则一次短暂存储故障会让同一批卡被重复整理。
        """
        ...

    def apply(
        self,
        tenant: str,
        mutations: list[dict],
        *,
        owner: str,
        idempotency_key: str,
        expected_revision: Any,
        maintenance_state: dict | None = None,
    ) -> ApplyResult:
        """把一批 mutation 作为一个原子单位写入。

        ``expected_revision`` 来自先前 ``load`` 的 ``Snapshot.revision``；
        与当前不符时适配器应拒绝写入（CAS 失败），由调用方重读后**重算**。
        纯新增（不依赖旧快照）可以传 ``None``。

        ``idempotency_key`` 保证同一批重放不产生第二份。

        卡片写入时间由存储维护，不由模型猜测：新增时补齐缺失的
        ``created_at`` / ``updated_at``；实际修改（含归档、取代、提升）更新
        ``updated_at``，不改 ``created_at``。读取、无变化操作和幂等重放
        不刷新时间，失败批次的时间也必须回滚。已有旧卡缺失的创建时间
        不补造；可信恢复在新增时显式提供的历史时间保留，普通 update
        不得覆盖这两个存储管理字段。``occurred_at`` 仍是原材料的发生时间。
        参考实现使用可注入的 ClockPort，在重放检查后取得一次批次写入时间；
        不把新生成的时间写进调用方 mutation 或重放指纹。

        ``maintenance_state`` 是整理账本（signature / seed_card_count / …）。
        给了就**必须和这批卡改动在同一个提交里**成或败：
        账本先走一步 → 这批整理永远不会重跑，改动丢了也没人知道；
        卡先走一步 → 下次照样整理同一批，重复合并。
        """
        ...
