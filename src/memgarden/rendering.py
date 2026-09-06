"""把库里的卡渲染成模型看得懂的几段文本。

## 为什么这一步必须在包里，不能留给宿主

Capture 判断「这条要 add 还是 supersede 哪张旧卡」，靠的就是这几段。宿主自己
渲染的话，每个接入方渲染出的形状都不一样，而模型对形状敏感 —— 表现是
「同样的对话，换个 Runtime 记出来的东西不一样」，且没有任何报错。

更要命的是漏渲染：不给 ``cards``，模型看不见任何旧卡，于是**永远只会 add**。
同一件事说两次就是两张 active 卡，没人会发现，因为每一步都「成功」了。
"""
from __future__ import annotations

#: 索引里最多带多少张。太多会挤掉对话本身，也会让模型挑花眼。
DEFAULT_CARD_LIMIT = 60


def render_card_index(cards: list[dict], *, limit: int = DEFAULT_CARD_LIMIT) -> str:
    """已有卡的索引。**id 必须原样出现** —— supersede 的 target_id 从这里抄。

    模型抄不到真实 id 时会自己编一个，而编出来的 id 在库里不存在，
    那条 supersede 会被拒 —— 表现是「模型明明想更正，结果又新增了一张」。
    """
    rows = []
    for card in _by_importance(cards)[:limit]:
        rid = str(card.get("id") or "").strip()
        summary = str(card.get("summary") or "").strip()
        if not rid or not summary:
            continue
        bucket = str(card.get("bucket") or "").strip()
        prefix = f"[{bucket}] " if bucket else ""
        rows.append(f"- {rid}: {prefix}{summary}")
    return "\n".join(rows)


def render_buckets(cards: list[dict]) -> str:
    """已有桶名，一行。让模型复用而不是每次新造一个近义词。"""
    seen: list[str] = []
    for card in cards:
        name = str(card.get("bucket") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return "、".join(seen)


def render_threads(cards: list[dict], *, limit: int = 40) -> str:
    """已有线索，一行。同样是为了复用。"""
    seen: list[str] = []
    for card in cards:
        for thread in card.get("threads") or ():
            name = str(thread or "").strip()
            if name and name not in seen:
                seen.append(name)
    return "、".join(seen[:limit])


def _by_importance(cards: list[dict]) -> list[dict]:
    """重要的排前面，好在截断时留下该留的。

    取不到重要度就按 0 算 —— 不能让缺字段的卡排到最前面。
    """
    def key(card: dict) -> float:
        try:
            return -float(card.get("importance") or 0)
        except (TypeError, ValueError):
            return 0.0
    return sorted(cards, key=key)
