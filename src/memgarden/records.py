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
    """用一张新卡取代**一张或多张**旧卡，旧卡留着并指向新卡。

    **这是编辑动作，不是删除。** 用户查历史时应该还能看到旧的那几张，
    以及它们被什么取代了 —— 记忆的演变本身是有价值的信息。

    ## 为什么要支持多个 target

    整理(Dream)提出的三种动作 —— merge / thicken / supersede —— 落到存储上
    **是同一个形状**：N 张旧卡收敛成 1 张新卡，旧的全部标记为被这张新卡取代。

        merge      讲同一件事的几张卡 → 合成更完整的一张
        thicken    零散的小提及 → 并进它们本来该属于的那张
        supersede  内容矛盾 → 新的取代旧的

    只有单个 ``target_id`` 的话，一次 merge 要拆成多条 mutation，而后面几条
    需要引用前一条**才刚生成的新卡 id** —— 那个 id 在批次提交前根本不存在。
    结果要么让调用方自己造 id（各家造法不同，撞 id 只是时间问题），要么放弃
    原子性。所以让一条 mutation 直接表达「这 N 张收敛成这 1 张」。

    ``target_id`` 保留，等价于 ``target_ids`` 只有一个元素 —— 老调用方不受影响。
    """

    target_id: str = ""
    #: 多张旧卡收敛成一张时用这个。与 ``target_id`` 二选一，同时给则合并去重。
    target_ids: tuple[str, ...] = ()
    card: Card | None = None
    op: str = "supersede"
    requires: tuple[str, ...] = ("supersede",)

    def targets(self) -> tuple[str, ...]:
        """规范化之后的目标列表 —— 调用方只认这一个入口，别自己拼两个字段。"""
        out: list[str] = []
        for candidate in (self.target_id, *self.target_ids):
            value = str(candidate or "").strip()
            if value and value not in out:
                out.append(value)
        return tuple(out)


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


class InvalidMutation(ValueError):
    """线上格式不合法 —— 缺必填字段、字段类型不对。

    和 :class:`UnknownMutation` 分开，是因为调用方的处置不同：未知 op 多半是
    版本不匹配（该升级），字段错多半是构造代码有 bug（该修）。
    """


def validate_mutations(payload: "list[dict]") -> list[Mutation]:
    """把线上格式严格校验成类型化改动。**进存储之前的唯一关口。**

    ## 为什么要有这一步

    以前是「送到 Store，Store 不认识就抛」。问题有三个：

    1. 每个 Store 实现各自判一遍，判得松紧不一 —— 官方 SQLite 抛 ValueError，
       别人的适配器可能默默跳过那条，于是记忆悄悄少了一条而没有任何报错。
    2. 错误在**存储层**冒出来，调用方看到的是「unknown op: merge」这种和自己
       代码毫无关系的话（sevenfloor 2026-09-02 实测到的正是这句）。
    3. 批次里第 3 条不合法时，前 2 条可能已经写进去了 —— 取决于 Store 有没有
       事务，而那不该由 mutation 的合法性来决定。

    所以统一在这里：**不合法就根本不进 Store**。

    ## 校验什么

    - op 必须认识；
    - 各 op 的必填字段必须在（``add`` 要 card，``supersede`` 要 target 和 card，
      ``update`` 要 record_id 和 changes，``archive``/``delete`` 要 target）；
    - 字段类型必须对得上。

    **不校验**卡的内容是否有意义 —— 那是 :mod:`memgarden.text` 的闸做的事，
    两者关注点不同：这里管「结构对不对」，那里管「内容值不值得写」。
    """
    out: list[Mutation] = []
    for index, row in enumerate(payload or []):
        if not isinstance(row, dict):
            raise InvalidMutation(f"第 {index} 条不是对象：{type(row).__name__}")
        try:
            mutation = mutation_from_dict(row)   # 未知 op 抛 UnknownMutation
        except UnknownMutation:
            raise
        except TypeError as exc:
            # dataclass 的必填字段缺了会抛裸 TypeError。原样冒出去的话，
            # 调用方看到的是「Card.__init__() missing 1 required positional
            # argument」—— 和他写的那条 mutation 对不上号。
            raise InvalidMutation(f"第 {index} 条字段不合法：{exc}") from exc
        _require_fields(index, mutation)
        out.append(mutation)
    return out


def _require_fields(index: int, m: Mutation) -> None:
    def _fail(what: str) -> None:
        raise InvalidMutation(f"第 {index} 条 {m.op} 缺 {what}")

    if isinstance(m, Add):
        if m.card is None:
            _fail("card")
    elif isinstance(m, Supersede):
        if not m.targets():
            _fail("target_id / target_ids")
        if m.card is None:
            _fail("card（取代旧卡必须给出新卡，否则那条记忆就没了）")
    elif isinstance(m, Update):
        if not str(m.record_id or "").strip():
            _fail("record_id")
        if not isinstance(m.changes, dict) or not m.changes:
            _fail("changes")
    elif isinstance(m, (Archive, Delete)):
        if not str(getattr(m, "target_id", "") or "").strip():
            _fail("target_id")


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
    "UnknownMutation", "InvalidMutation", "mutation_from_dict",
    "validate_mutations", "required_capabilities",
]
