"""做梦的「值不值得」判据 —— 纯计算，不碰库、不看时钟、不调模型。

## 这一层管什么

    内核（本模块）  我这儿攒了多少新东西？乱到该整理了吗？
    宿主（io）      现在是不是合适的时机？夜里吗？上次那个跑完了吗？刚失败过吗？

拆开的理由：**「什么算攒够了」是 Garden 内部结构的知识**（只数哪种卡、签名怎么算），
换个记忆库实现就完全不同 —— 一个向量库可能永远不需要整理。而「夜间窗口 / 防重 /
失败退避」跟哪个记忆库无关，那是宿主的调度。

原实现把两者混在 ``proactive/dream_scheduler.py`` 的一个函数里，于是 Garden 的
知识住在 io 的调度代码里。本模块把前一半搬回来。

## 一条不显然但关键的规则：只数「非做梦产生」的卡

做梦自己写出来的卡**不计入水位线**，否则会自激：做梦 → 产生新卡 → 又满足触发条件
→ 又做梦，每晚空转。而且做梦退休掉的旧种子卡**仍然算数**（种子集合从全部卡里筛，
不是从可用卡里筛），所以这条线只增不减。

## 时间不进这里

「距上次多久」「是不是夜里」都需要看时钟，属于宿主。内核只比较两个数字：
现在的种子卡数 vs 上次做梦时的种子卡数。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .records import Supersede

#: 整理产物的来源标记。内核只需要认出**这一个**语义：
#: 「这张卡是不是上次整理产出的」——用来防止整理产物自己喂自己
#: （整理完立刻又满足「攒够了」，无限循环）。
#:
#: 取值是 io 的历史约定，留作默认值；别的宿主用别的名字时，
#: 把自己的值传给 `seed_cards(..., consolidated_source=...)` 即可 ——
#: 内核不需要知道宿主还有哪些别的来源（那 17 个已于 2026-08-17 搬回 io）。
DREAM_SOURCE = "memory_dream"

#: 参与签名的字段。只用不可变或极少变的字段 —— 签名要能代表「花园的形状」，
#: 不能因为某张卡被读了一次（last_referenced_at 变了）就变。
_SIGNATURE_FIELDS = ("id", "source", "created_at", "occurred_at")


@dataclass(frozen=True)
class DreamSnapshot:
    """当前花园的形状。全部来自明文元数据，不需要解密。"""

    card_count: int
    seed_card_count: int
    signature: str


@dataclass(frozen=True)
class DreamLedger:
    """上次做梦时记下的账。"""

    last_seed_card_count: int = 0
    last_signature: str = ""


@dataclass(frozen=True)
class DreamVerdict:
    """值不值得整理，以及为什么。

    ``reason`` 沿用原实现的字符串，调用方的日志与指标口径不变。
    """

    needed: bool
    reason: str
    new_cards: int


def seed_cards(all_cards: Sequence[Mapping[str, Any]], *,
               consolidated_source: str | None = None) -> list[Mapping[str, Any]]:
    """筛出计入水位线的卡：**排除做梦自己产出的**。

    注意入参是「全部卡」而不是「可用卡」—— 被做梦退休掉的旧种子卡仍要算数，
    否则水位线会因为一次整理而回退，下次又触发。
    """
    marker = str(consolidated_source or DREAM_SOURCE)
    return sorted(
        (c for c in all_cards if str(c.get("source") or "").strip() != marker),
        key=lambda c: str(c.get("id") or ""),
    )


def snapshot_signature(cards: Sequence[Mapping[str, Any]]) -> str:
    """花园形状的指纹。同一批卡（顺序无关）恒等，增删任何一张就变。"""
    material = "\n".join(
        "|".join(str(card.get(key) or "") for key in _SIGNATURE_FIELDS)
        for card in cards
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def dream_snapshot(
    *,
    available_cards: Sequence[Mapping[str, Any]],
    all_cards: Sequence[Mapping[str, Any]],
    consolidated_source: str | None = None,
) -> DreamSnapshot:
    """从两份已过滤的卡列表算出快照。

    ``available_cards`` —— 当前可用的卡（宿主已按可见性/归属过滤）。
    ``all_cards``       —— 含已被取代的全部卡，用来算只增不减的水位线。
    """
    seeds = seed_cards(all_cards, consolidated_source=consolidated_source)
    return DreamSnapshot(
        card_count=len(available_cards),
        seed_card_count=len(seeds),
        signature=snapshot_signature(seeds),
    )


def needs_dream(
    snapshot: DreamSnapshot,
    ledger: DreamLedger,
    *,
    min_new_cards: int,
) -> DreamVerdict:
    """值不值得整理。**只看数量与指纹，不看时间、不看内容。**

    不看内容是硬约束而非偷懒：桶名和正文都在密文里，要判断「有没有重复的桶」
    就得把整个花园读一遍 —— 每天对每个用户做一次，代价扛不住。
    相似桶检测只能放进真正的整理阶段。

    ``min_new_cards`` 由宿主传入（它是可配置的运行参数，不是 Garden 的常量）。
    """
    if snapshot.card_count <= 0:
        return DreamVerdict(False, "no_memory_cards", 0)

    new_cards = max(0, snapshot.seed_card_count - max(0, ledger.last_seed_card_count))

    if ledger.last_signature and ledger.last_signature == snapshot.signature:
        return DreamVerdict(False, "already_dreamed", new_cards)

    if new_cards < max(0, min_new_cards):
        return DreamVerdict(False, "not_enough_new_cards", new_cards)

    return DreamVerdict(True, "due", new_cards)


def dream_idempotency_key(
    ledger: DreamLedger,
    snapshot: DreamSnapshot,
    *,
    extra: Sequence[Any] = (),
) -> str:
    """同一个花园状态重复触发时产出同一个键，避免排重复的活。

    ``extra`` 让宿主把自己那侧的材料也拌进来（io 拌的是对话轮数）。
    它是参数而不是内核字段 —— 轮数要查聊天记录，不属于花园的形状。
    """
    material = "|".join(
        [
            str(ledger.last_signature or ""),
            str(snapshot.signature or ""),
            str(snapshot.seed_card_count),
            *(str(item) for item in extra),
        ]
    )
    return "dream:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# 从「模型的整理建议」到「存储能执行的改动」
# --------------------------------------------------------------------------- #

def consolidations_to_mutations(
    consolidations: "Sequence[Mapping[str, Any]]",
    *,
    mount: str = "",
) -> list[dict]:
    """把整理建议转成 typed mutation。**这一步归 Garden，不下放给宿主。**

    ## 为什么必须在这里转

    建议和存储用的是**两套不同的 op 词汇表**：

        建议侧   merge / thicken / supersede      「该怎么整理」
        存储侧   add / update / supersede / …     「该怎么改库」

    2026-09-02 之前 :meth:`GardenComponent.run_maintenance` 直接把建议原样
    当作 ``mutations`` 返回（只加了个 mount 字段），于是交给官方 Store 就是：

        ValueError: unknown op: merge

    症状在用户那头是「io 说整理好了，但记忆没变」。

    让每个接入方各写一份转换是更糟的选择：这里面藏着「旧卡要保留不能删」
    「N 张收敛成 1 张必须原子」这些语义，写错了不会报错，只会悄悄丢记忆。

    ## 三种建议其实是同一个形状

        merge      讲同一件事的几张 → 合成更完整的一张
        thicken    零散的小提及 → 并进它们本来该属于的那张
        supersede  内容矛盾 → 新的取代旧的

    都是「N 张旧卡收敛成 1 张新卡，旧的全部保留并指向新的」，所以统一落成一条
    :class:`~memgarden.records.Supersede`。**保留 ``consolidation_op`` 字段**记下
    原本是哪一种 —— 存储行为相同，但「为什么被整理掉」对用户和排查都有意义。

    ## 不做的事

    - **不删任何卡。** 三种建议没有一种意味着删除；用户主动删除走别的路。
    - **不自己造新卡 id。** 由 Store 分配 —— 各家造法不同，撞 id 只是时间问题。
    - 建议里缺 target 或缺正文的，:func:`~memgarden.prompts.dream.parse_dream_consolidations`
      已经在上游丢掉了，这里不再重复过滤。
    """
    out: list[dict] = []
    for row in consolidations or ():
        if not isinstance(row, Mapping):
            continue
        targets = [str(i).strip() for i in (row.get("card_ids") or ()) if str(i).strip()]
        result = row.get("result") if isinstance(row.get("result"), Mapping) else {}
        if not targets or not result:
            continue
        card = dict(result)
        if mount:
            card.setdefault("mount", mount)
        payload = Supersede(
            target_ids=tuple(targets),
            card=card,
            mount=mount or "agent-private",
        ).as_dict()
        # 每条破坏性改动都要带得住的理由 —— 上游 parse 已经强制要求它非空。
        rationale = str(row.get("rationale") or "").strip()
        if rationale:
            payload["rationale"] = rationale
        # 存储行为和普通 supersede 一样,但保留「本来是哪一种整理」——
        # 用户问「我那张卡去哪了」时,答案是 merge 还是 supersede 不一样。
        payload["consolidation_op"] = str(row.get("op") or "").strip().lower()
        out.append(payload)
    return out
