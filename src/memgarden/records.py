"""Garden 的记录和改动指令 —— 正式字段、带版本、明文。

## 这些是 Garden 自己的结构，不是「所有记忆系统的通用标准」

bucket、thread、supersede 是 Memory Garden 的产品判断。别的记忆系统没有这些
概念，也不该被要求有 —— 那是 Runtime 侧薄接口要解决的事，不在这里。

这个区分是 2026-08-29 定的。起因是一次范围偏移：为了让接口能兼容一个
「可能只有 add/search」的假想系统，差点把 Garden 自己也缩成 CRUD。那样得到的
不是可插拔的 Garden，是丢了产品特点的最低公分母。

## 为什么改动要有类型

以前 mutation 是 ``list[dict]``，靠字符串约定操作类型。代价是四件事都做不了：

    推不出这批操作需要什么存储能力（要不要支持 supersede？要不要原子批量？）
    没法做 schema 校验 —— 打错一个键名要到线上才发现
    分不清「整理性归档」和「用户主动删除」—— 两者的审计和合规含义完全不同
    错误没法标准化 —— 调用方只能靠猜

## 明文

全链路明文。密文、envelope、密钥分发不在这里 —— 宿主要加密就在自己的 adapter
里做，转成明文再喂进来。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RECORD_SCHEMA_VERSION = 1

#: 记录的生命周期。**归档和删除是两件事**：
#:   archived   整理时被合并掉了，内容还在、可追溯，用户查历史能看到
#:   deleted    用户主动要求删掉，或合规要求 —— 不该还能被查出来
Lifecycle = Literal["active", "archived", "superseded", "deleted"]


@dataclass
class Card:
    """一张记忆卡。**面向人，不是面向检索的向量。**

    ``summary`` 是列表里那一行标题，``content`` 是点进去看到的正文。
    两个都必须是真内容 —— 空正文配长标题是用户看到的第一类垃圾卡。
    """

    summary: str
    content: str
    #: 分类键。同一类记忆裂成两个桶 = 检索时互相看不见。
    bucket: str = ""
    #: 可复用的线索标签，1–4 个。
    threads: list[str] = field(default_factory=list)
    #: 对理解这个人的未来价值。
    importance: float = 0.5
    #: 想起来时的情绪激活度。
    pulse: float = 0.0
    #: 事情发生的时间（不是写入时间）。
    occurred_at: str = ""
    #: 这张卡在花园里的角色，比如 ``turning_point``（人生节点）。
    role: str = ""
    #: 敏感内容 —— 宿主据此决定要不要在日常闲聊里主动提起。
    is_sensitive: bool = False
    #: 这张卡从哪来（哪次落卡、哪次导入）。
    source: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", [], None)}


@dataclass
class Record:
    """存起来之后的一张卡：卡的内容 + 身份 + 归属 + 生命周期。"""

    record_id: str
    card: Card
    mount: str = "agent-private"
    lifecycle: Lifecycle = "active"
    #: 乐观并发用。宿主自己定它怎么递增。
    revision: str = ""
    created_at: str = ""
    updated_at: str = ""
    #: 被哪张卡取代了（整理合并的结果）。
    superseded_by: str = ""
    schema_version: int = RECORD_SCHEMA_VERSION

    def as_dict(self) -> dict:
        out = asdict(self)
        out["card"] = self.card.as_dict()
        return {k: v for k, v in out.items() if v not in ("", None)}


# --------------------------------------------------------------------------- #
# 改动指令
# --------------------------------------------------------------------------- #

@dataclass
class Mutation:
    """一条改动。子类决定语义；``op`` 是线上格式里的判别键。"""

    op: str = ""
    mount: str = "agent-private"
    #: 同一批重放不写第二遍。宿主保证它对同一批输入稳定。
    idempotency_key: str = ""
    #: 乐观并发：期望的当前版本。不匹配则拒绝，让调用方重读重算。
    expected_revision: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}

    #: 这条改动需要存储支持什么能力。**由类型推出来，不靠调用方记得声明。**
    requires: tuple[str, ...] = ()


@dataclass
class Add(Mutation):
    """新增一张卡。"""

    card: Card | None = None
    op: str = "add"


@dataclass
class Update(Mutation):
    """就地修改一张卡。用于订正错字、补充字段 —— **不用于内容被推翻**，
    那种情况用 :class:`Supersede`，因为要保留「以前是这么认为的」这条链。"""

    record_id: str = ""
    changes: dict = field(default_factory=dict)
    op: str = "update"


@dataclass
class Supersede(Mutation):
    """用新卡取代旧卡，旧卡留着并指向新卡。

    **这是编辑动作，不是删除。** 用户查历史时应该还能看到旧的那张，
    以及它被什么取代了 —— 记忆的演变本身是有价值的信息。
    """

    target_id: str = ""
    card: Card | None = None
    op: str = "supersede"
    requires: tuple[str, ...] = ("supersede",)


@dataclass
class Archive(Mutation):
    """整理时把一张卡收起来，不再参与召回，但内容还在、可追溯。"""

    record_id: str = ""
    reason: str = ""
    op: str = "archive"


@dataclass
class Delete(Mutation):
    """**用户主动要求删除，或合规删除。**

    刻意和 archive 分开：两者的审计和合规含义完全不同。以前它们混在一个
    非类型化的 dict 里，从改动本身看不出这是「整理」还是「用户要删」——
    而后者需要审计记录、可能需要级联删除派生数据。

    ``requested_by`` 必填的原因也在这里：删除必须能追溯到是谁要求的。
    """

    record_id: str = ""
    requested_by: str = ""
    reason: str = ""
    op: str = "delete"
    requires: tuple[str, ...] = ("hard_delete",)


@dataclass
class NoOp(Mutation):
    """什么都不做，但要留个痕。

    看起来多余，实际有用：批量处理时「这一条看过了、结论是不用改」和
    「这一条漏了」必须分得开。
    """

    reason: str = ""
    op: str = "no_op"


_OPS: dict[str, type[Mutation]] = {
    "add": Add, "update": Update, "supersede": Supersede,
    "archive": Archive, "delete": Delete, "no_op": NoOp,
}


class UnknownMutation(ValueError):
    """线上格式里出现了不认识的 ``op``。

    **不静默忽略**：忽略一条改动意味着用户的一次表达凭空消失，
    而且没有任何错误可查。
    """


def mutation_from_dict(payload: dict) -> Mutation:
    """从线上格式还原成类型化的改动。"""
    op = str((payload or {}).get("op") or "").strip()
    cls = _OPS.get(op)
    if cls is None:
        raise UnknownMutation(f"unknown mutation op: {op!r}")
    data = {k: v for k, v in (payload or {}).items() if k not in {"op", "requires"}}
    if "card" in data and isinstance(data["card"], dict):
        known = {f for f in Card.__dataclass_fields__}
        data["card"] = Card(**{k: v for k, v in data["card"].items() if k in known})
    known_fields = set(cls.__dataclass_fields__)
    return cls(**{k: v for k, v in data.items() if k in known_fields})


def required_capabilities(mutations: list[Mutation]) -> set[str]:
    """这批改动一共需要存储支持哪些能力。

    **从类型推出来的，不靠调用方记得声明** —— 靠自觉的检查等于没有检查。
    """
    out: set[str] = set()
    for m in mutations:
        out |= set(m.requires)
    if len(mutations) > 1:
        # 多条改动要么全成要么全不成。半成功的批次会留下自相矛盾的状态：
        # 旧卡被标成 superseded，新卡却没写进去 —— 那条记忆就消失了。
        out.add("atomic_batch")
    return out


__all__ = [
    "RECORD_SCHEMA_VERSION", "Lifecycle", "Card", "Record",
    "Mutation", "Add", "Update", "Supersede", "Archive", "Delete", "NoOp",
    "UnknownMutation", "mutation_from_dict", "required_capabilities",
]
