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

    mutations: list[dict] = field(default_factory=list)
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
    "ContextRequest", "ContextResult",
    "MaintenanceRequest", "MaintenanceResult",
    "ToolDefinition", "ToolCall", "ToolResult",
]
