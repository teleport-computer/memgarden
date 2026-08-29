"""GardenComponent 的输入输出契约 —— 明文、带版本、可 JSON 序列化。

## 这些结构属于 Garden 自己

它们**不是**「所有记忆系统的通用世界标准」。bucket、thread、supersede 是
Memory Garden 的产品判断，别的记忆系统没有也不该被要求有。

这个区分是 2026-08-29 定的，起因是一次范围偏移：为了让接口能兼容一个
「可能只有 add/search」的假想系统，差点把 Garden 自己也缩成 CRUD ——
那样得到的不是可插拔的 Garden，是丢了产品特点的最低公分母。

    Garden 自己的结构    第一阶段就完整定义、版本化      ← 本文件
    所有记忆的通用标准    不在第一阶段冻结              ← 由 Runtime 侧的薄接口负责

## 明文

全链路明文。密文、envelope、enclave、密钥分发都不在这里 —— 宿主要加密就在
自己的 adapter 里加解密，转成明文再喂进来。基础设施层的 TLS / 磁盘加密透明存在，
不影响这些字段的语义。

## 版本

每个结构带 ``schema_version``。字段只增不减；要删要改语义就升版本号。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

SCHEMA_VERSION = 1

#: 逻辑可见范围。只定「归谁、谁能读写」，不定密码学。
Mount = Literal["agent-private", "user-private", "family-shared", "workspace-shared"]
DEFAULT_MOUNT: Mount = "agent-private"


# --------------------------------------------------------------------------- #
# 过程可观测
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Step:
    """编排过程里的一步 —— 供宿主记轨迹。

    ## 为什么必须有这个

    把编排收进组件之后，宿主**会丢掉它原本看得见的东西**：第一次问了什么、
    模型回了什么、为什么要重问。宿主 io 现在就在记这些（trajectory_recorder），
    换成组件之后如果看不到，可观测性就是净退步 —— 那样这层门面是亏的。

    所以组件必须能把每一步汇报出来。宿主传一个回调，想记什么记什么。

    ## 内容无关

    ``detail`` 里只放长度、计数、错误码这类**内容无关**的量。
    提示词和模型回复本身通过 ``prompt`` / ``reply`` 单独给 —— 它们含用户内容，
    宿主自己决定要不要落库、要不要脱敏。**默认不会被塞进 trace。**
    """

    kind: str                      # prompt_built / model_called / parsed / retrying / done
    purpose: str = ""              # capture / dream / migrate
    attempt: int = 0
    detail: dict = field(default_factory=dict)
    #: 含用户内容 —— 宿主自己决定怎么处理，内核不落库。
    prompt: str | None = None
    reply: str | None = None


#: 宿主传进来的步骤回调。
StepSink = Any


# --------------------------------------------------------------------------- #
# 谁在操作
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Actor:
    """这次操作代表谁。**由宿主的认证上下文注入，不能信客户端或模型自报。**

    内核不做鉴权 —— 它没有能力判断一个 user_id 是不是真的。它只把 actor 原样
    带进记录和 trace，让宿主能追溯。
    """

    user_id: str = ""
    agent_id: str = ""
    session_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 落卡（capture）
# --------------------------------------------------------------------------- #

@dataclass
class CaptureRequest:
    """「这段对话里有什么值得记的」。

    ``window`` 是已经渲染好的对话文本 —— 谁是谁、怎么截断、通话转写怎么展开，
    都是宿主的事（它才知道自己的消息结构和说话人名字）。内核只读这段文本。
    """

    window: str
    actor: Actor = field(default_factory=Actor)
    mount: Mount = DEFAULT_MOUNT

    #: 花园语言（"zh-Hans" / "en"）。**必填，没有默认值** —— 默认成某种语言
    #: 等于把一套分类法硬塞给所有使用者。宿主用 garden_language 那套证据自己算。
    locale: str = ""

    #: 已有的桶名和线索，渲染好的一行。给模型收敛用，**不参与语言判定**。
    buckets: str = ""
    threads: str = ""
    #: 身份卡正文，渲染好的。内核不认识宿主的身份数据结构。
    identity: str = ""
    #: 已有卡片的索引，渲染好的。模型要 supersede 时从这里挑 target_id。
    cards: str = ""

    ai_name: str = ""
    user_name: str = ""
    naming_rule: str | None = None

    #: 哪把「什么值得记」的尺子（见 policies）。留空 = 日常聊天档。
    policy: str | None = None

    #: 幂等键。同一批对话重放时防止写两遍；宿主自己保证它对同一批输入稳定。
    idempotency_key: str = ""

    schema_version: int = SCHEMA_VERSION


@dataclass
class CaptureResult:
    """落卡的结果 —— **是「该这么改」的指令，不是「已经写好了」。**

    内核不写库。宿主拿到 ``mutations`` 之后自己落库（可能还要加密、要过自己的
    权限闸），落完的真实结果由宿主掌握。

    ``retried`` / ``error`` 一起构成可观测性：模型第一次吐了脏东西、重问一次
    救回来了，这件事宿主要能看见 —— 否则「偶尔丢记忆」永远查不出原因。
    """

    #: Garden 原生的改动指令。宿主没有自己的写入格式时直接用这个。
    mutations: list[dict] = field(default_factory=list)

    #: 解析并过闸之后的**原始卡**（含 ``action`` / ``target_id``）。
    #:
    #: 为什么两个都给：宿主往往有自己的写入格式。宿主 io 的 action 里带加密信封和
    #: 通话溯源，它需要从卡本身构造，而 ``mutations`` 已经把 action/target_id
    #: 拆到外层了 —— 逼它拆回去再拼一遍是无谓的往返，还容易在往返里丢字段。
    #:
    #: 两者是同一批卡的两种表达，不是两批数据。
    cards: list[dict] = field(default_factory=list)

    #: 打回重问了几次。>0 说明模型第一次的输出不合格。
    retried: int = 0
    #: 非 None = 这批彻底失败了，宿主应当让 job 失败而不是当成「没什么可记的」。
    #: 报成 noop 会让游标推进，这批对话就永远不会再被看一眼。
    error: str | None = None
    #: 内容无关的观测量：看了多长的窗口、产出几张、模型调了几次。
    trace: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @property
    def nothing_worth_keeping(self) -> bool:
        """真的没什么可记 vs 出错了 —— 这两件事必须分得开。"""
        return not self.mutations and self.error is None


# --------------------------------------------------------------------------- #
# 三种写入来源 —— 语义不同，不能混成一个
# --------------------------------------------------------------------------- #
#
# ## 为什么必须分开
#
# 「这段对话里有什么值得记」和「把我三年的聊天记录导进来」和「帮我记一下我不吃辣」
# 看起来都是写记忆，但**三者的判断尺子完全不同**：
#
#     自动落卡   要克制 —— 什么都记 = 真正重要的卡被噪声挤出召回名额
#     历史导入   要宽 —— 用户主动给的三年记录，漏掉才是失职；
#                但也最容易一次灌进几百张、把花园淹掉
#     用户明说   **不许判断** —— 他说「记一下」，我们的活是记下来，不是评估值不值得
#
# 混成一个接口的后果很具体：用同一把「克制」的尺子去处理用户明说的请求，
# 模型会自作主张判定「这不值得记」，然后什么都没发生、也不报错。

@dataclass
class ImportRequest:
    """历史导入：用户主动交出一批过去的材料。

    和自动落卡的区别在**判断尺度**：这是用户交出来的东西，宁可多记；
    但也要防一次灌爆花园，所以 ``max_cards`` 由宿主定上限。
    """

    material: str
    actor: Actor = field(default_factory=Actor)
    mount: Mount = DEFAULT_MOUNT
    locale: str = ""
    #: 这批材料是什么（聊天记录 / 日记 / 备忘录…）。只作提示词里的背景交代。
    material_kind: str = ""
    #: 用哪把尺子。留空 = ``history_import``（比日常聊天宽：用户主动交出来的
    #: 东西，漏掉才是失职）。``curated_archive`` 是「几乎全收」那一档。
    policy: str | None = None
    #: 一次最多产出多少张。**必须有上限** —— 三年的聊天记录一次蒸出几百张，
    #: 之后的召回会被这批淹没，而用户看不出发生了什么。
    max_cards: int = 50
    ai_name: str = ""
    user_name: str = ""
    idempotency_key: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class CuratedWriteRequest:
    """用户明说要记的一件事。

    ⚠️ **这条路不做「值不值得记」的判断。** 用户说了「记一下我不吃辣」，
    我们的活是把它记好（写清楚、归对桶），不是评估该不该记。
    拿自动落卡那把克制的尺子来量，模型会判「这不值得」然后什么都不发生 ——
    用户以为记住了，其实没有，而且没有任何错误可查。
    """

    text: str
    actor: Actor = field(default_factory=Actor)
    mount: Mount = DEFAULT_MOUNT
    locale: str = ""
    #: 用户指定的桶；留空则由组件归类。
    bucket: str = ""
    idempotency_key: str = ""
    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 导出与用户删除
# --------------------------------------------------------------------------- #

@dataclass
class ExportRequest:
    """把这个人的记忆导出来给他。

    **这是用户的权利，不是我们的功能。** 所以默认导出**全部** mount 里
    他有权访问的内容，包括已归档和已被取代的 —— 「你删掉的那条我还留着」
    和「你看不到自己删过什么」都是不该有的状态。
    """

    actor: Actor = field(default_factory=Actor)
    mounts: tuple[Mount, ...] = ()
    #: 含已归档 / 已被取代的。默认**含** —— 导出是给用户看他的全部数据。
    include_archived: bool = True
    schema_version: int = SCHEMA_VERSION


@dataclass
class ExportResult:
    records: list[dict] = field(default_factory=list)
    #: 内容无关的统计，供宿主核对完整性。
    counts: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# private → shared 的提升
# --------------------------------------------------------------------------- #

@dataclass
class PromoteRequest:
    """把一条私有记忆提升到共享范围。

    **必须是显式操作，不能是副作用。** 「我跟这个助手说的悄悄话」变成
    「全家都能看到」是一次不可逆的可见性变更 —— 用户必须是主动做的，
    而且宿主必须先做完权限校验（内核不自行授予权限）。

    反方向（shared 收回 private）刻意不提供：内容一旦共享出去，
    收回是安全幻觉 —— 别人可能已经看过、记住、转述了。要真收回只能删。
    """

    record_id: str
    to_mount: Mount
    actor: Actor = field(default_factory=Actor)
    #: 宿主的权限校验结论。内核不自行判断谁能写哪个 mount。
    authorized: bool = False
    reason: str = ""
    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 想起来（recall + context）
# --------------------------------------------------------------------------- #

@dataclass
class ContextRequest:
    """「这一轮该想起哪几张」。"""

    query: str = ""
    actor: Actor = field(default_factory=Actor)
    #: 读哪些 mount。宿主按已认证的权限填 —— 内核不自行授予。
    mounts: tuple[Mount, ...] = (DEFAULT_MOUNT,)
    #: 候选卡片。宿主已经做完生命周期过滤和权限过滤 —— 内核不认识
    #: 你的 is_archived / 权限字段，也没资格替你判断谁能看什么。
    candidates: list[dict] = field(default_factory=list)
    limit: int = 8
    schema_version: int = SCHEMA_VERSION


@dataclass
class ContextResult:
    """挑出来的卡 + 为什么挑它们。

    ``record_ids`` 在前、``blocks`` 在后是刻意的：宿主可以只用 id 自己回填内容
    （数据在它手里、权限也在它手里），也可以直接用渲染好的 blocks。
    """

    record_ids: list[str] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    #: 每张卡是被哪一段选中的（转折点 / 最近 / 相关）。
    #: 排查「为什么想不起来」时这条是唯一的线索。
    trace: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 整理（dream / maintenance）
# --------------------------------------------------------------------------- #

@dataclass
class MaintenanceRequest:
    """「该整理一遍了吗；要整理的话怎么合」。"""

    #: 当前可用的卡（宿主已按可见性/归属过滤完）。
    cards: list[dict] = field(default_factory=list)
    #: **含已被取代的全部卡**，用来算只增不减的水位线。留空则退回用 ``cards``。
    #: 分开传是必要的：被整理退休掉的旧卡仍要计入水位，否则水位会因为一次整理
    #: 而回退，下次立刻又触发整理 —— 整理会没完没了。
    all_cards: list[dict] = field(default_factory=list)

    #: 上次整理时记下的账 —— **这是宿主的持久状态**，内核不存东西。
    #: 没有它就判断不出「这次和上次比多了多少新卡」，只能每次都整理一遍。
    last_signature: str = ""
    last_seed_card_count: int = 0

    actor: Actor = field(default_factory=Actor)
    mount: Mount = DEFAULT_MOUNT
    locale: str = ""
    ai_name: str = ""
    user_name: str = ""
    #: 最近的对话，渲染好的。整理时用来判断哪些记忆已经过时。
    recent_conversations: str = ""
    #: 只看要不要整理、不真的整理。宿主的调度器用它决定要不要排这个活。
    dry_run: bool = False
    idempotency_key: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class MaintenanceResult:
    """整理的结果。同样是**指令**，不是已经改完了。"""

    needed: bool = False
    mutations: list[dict] = field(default_factory=list)
    error: str | None = None
    trace: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 展示投影 —— 给 Runtime 的通用记忆列表用
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BrowseItem:
    """一条记忆在**通用**记忆列表里长什么样。

    ## 为什么要有这层投影

    Garden 自己的界面可以完整用 bucket / threads / supersede 那套。
    但 Runtime 的通用 Browse 页面要能显示**任何**记忆组件的内容 ——
    别人可能没有「桶」这个概念。

    所以投影只要三个必需字段。没有 ``group_label`` 时 UI **平铺或显示
    「未分类」，不能空白** —— 一个不产出桶的组件接进来，用户看到的不该是
    一个空页面，那看起来像数据丢了。

    ⚠️ 这是**展示协议**，不是要求 Garden 删掉 bucket/thread。
    Garden 原生展示照常用它们（sevenfloor §10）。
    """

    record_ref: str          # 必需：稳定 id，宿主据此回填内容
    display_text: str        # 必需：一行能看懂的话
    mount: str               # 必需：这条属于哪个可见范围
    occurred_at: str = ""    # 建议
    updated_at: str = ""     # 建议
    provider: str = ""       # 建议：哪个组件产出的
    group_label: str = ""    # 可选：能分组就分，不能就平铺
    tags: tuple[str, ...] = ()   # 可选

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", (), None)}


def to_browse_item(record: dict, *, provider: str = "memgarden") -> BrowseItem:
    """把一张 Garden 卡投影成通用展示项。

    ``bucket`` 落到 ``group_label``、``threads`` 落到 ``tags`` —— 但那只是
    Garden 这一家的映射；别的组件有别的映射，UI 只认投影后的字段。
    """
    card = record.get("card") if isinstance(record.get("card"), dict) else record
    return BrowseItem(
        record_ref=str(record.get("record_id") or record.get("id") or ""),
        display_text=str(card.get("summary") or ""),
        mount=str(record.get("mount") or DEFAULT_MOUNT),
        occurred_at=str(card.get("occurred_at") or ""),
        updated_at=str(record.get("updated_at") or ""),
        provider=provider,
        group_label=str(card.get("bucket") or ""),
        tags=tuple(str(x) for x in (card.get("threads") or [])),
    )


# --------------------------------------------------------------------------- #
# 给模型的工具
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolDefinition:
    """一个能给模型调用的工具。``parameters`` 是 JSON Schema。"""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)
    actor: Actor = field(default_factory=Actor)
    mounts: tuple[Mount, ...] = (DEFAULT_MOUNT,)
    schema_version: int = SCHEMA_VERSION


@dataclass
class ToolResult:
    ok: bool = True
    #: 给模型看的文本。
    content: str = ""
    #: 需要宿主执行的改动（工具要写记忆时）。同样只是指令。
    mutations: list[dict] = field(default_factory=list)
    error: str | None = None
    schema_version: int = SCHEMA_VERSION


__all__ = [
    "SCHEMA_VERSION", "Mount", "DEFAULT_MOUNT", "Actor", "Step", "StepSink",
    "CaptureRequest", "CaptureResult",
    "ImportRequest", "CuratedWriteRequest",
    "ExportRequest", "ExportResult", "PromoteRequest",
    "ContextRequest", "ContextResult",
    "MaintenanceRequest", "MaintenanceResult",
    "ToolDefinition", "ToolCall", "ToolResult",
    "BrowseItem", "to_browse_item",
]
