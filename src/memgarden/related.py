"""关联读取：宿主取回一张卡时，顺带给出与它**显式相连**或**同线索**的邻居。

## 用户视角

    用户问「那次搬家后来怎么样了」→ 模型取回「搬家」那张卡
    → 同时拿到三条短提示：它锚定的「看房」卡、它取代掉的旧「预算」卡（标成历史）、
      同一条线索「搬家」下的另一张卡
    → 模型知道还有这几张可以接着读，而不是只看到孤零零一张

## 语义（v1 从宿主 io 的实现逐字节移植，行为以黄金用例锁定）

- 关系只有三种，优先级 ``anchor`` > ``supersedes`` > ``thread``：
  ``anchor`` / ``supersedes`` 只看**源卡**上的 ``anchor_memory_ids`` /
  ``supersedes`` 字段（源 → 候选单向）；``thread`` 是源卡和候选卡的
  ``threads`` 有交集。
- 同一张候选被多张源卡命中时，保留最强的那条：显式链接胜过线索，
  其次按源卡 id 的字典序。
- 输出先放显式链接、再放线索邻居，各自按候选 id 排序，最后截到 ``cap``。
- 生命周期：``archived`` / ``deleted`` 永不出现；``superseded`` 只能沿
  **显式链接**出现，并在 ``status`` 里如实标出（模型要知道那是历史版本）。
  线索邻居里的历史卡一律不出现。
- 没有 summary 的卡丢弃 —— 关联项只给一句话提示，不外泄正文。
- **不做**（需要产品拍板）：反向找「取代了我的新卡」、多跳。

## 边界

:func:`one_hop` 是纯函数：候选必须由宿主**先按 owner / 可见性过滤好**。
内核不认识宿主的归属与权限字段，也没资格判断谁能看什么。

内核只认规范字段：``status``（active / superseded / archived / deleted）、
``superseded_by``，以及参考 Store 自己写的 ``archived``。宿主的其他生命周期
字段（例如 ``archived_at`` / ``archive_reason``）要在喂进来之前翻译成 ``status``；
摘要只读 ``summary``，宿主的旧标题字段同样要先翻译。

挂了 Store 的宿主用 :meth:`memgarden.MountedGarden.related`，归属、挂载点和
生命周期过滤由它自己做。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["one_hop", "links", "RELATIONS", "DEFAULT_CAP"]

#: 关系的全部取值，按优先级排列。
RELATIONS = ("anchor", "supersedes", "thread")

#: 默认最多返回几条关联项。和宿主 io 当前的读侧上限一致。
DEFAULT_CAP = 6

#: 摘要在关联项里的最大长度。关联项只是「还有这张可以读」的提示，不是正文。
_SUMMARY_CHARS = 120
#: 单个链接值的最大长度、每个链接字段最多取几个。超出的值当成坏数据丢掉，
#: 不截断 —— 截断后的 id 可能正好撞上另一张卡。
_LINK_CHARS = 160
_LINKS_PER_FIELD = 20

_RETIRED_STATUSES = frozenset({"archived", "superseded", "deleted"})


def links(value: object) -> list[str]:
    """把一个链接字段规范成去重的 id 列表（接受单个字符串或字符串列表）。"""
    values = [value] if isinstance(value, str) else value
    return list(dict.fromkeys(
        v for v in (values if isinstance(values, list) else [])
        if isinstance(v, str) and v and len(v) <= _LINK_CHARS
    ))[:_LINKS_PER_FIELD]


def _is_retired(card: Mapping[str, Any]) -> bool:
    if not isinstance(card, Mapping):
        return True
    return bool(
        card.get("archived") is True
        or str(card.get("status") or "").lower() in _RETIRED_STATUSES
        or str(card.get("superseded_by") or "").strip()
    )


def _summary(card: Mapping[str, Any]) -> str:
    return " ".join(str(card.get("summary") or "").strip().split())


def one_hop(
    sources: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    *,
    cap: int = DEFAULT_CAP,
) -> list[dict]:
    """源卡的一跳邻居。

    ``sources``：宿主刚取回的卡。``candidates``：同一 owner、宿主已过滤好
    可见性的卡（可以包含 superseded，函数自己决定它们能不能出现）。

    每项形如 ``{"id", "summary", "source_id", "relation", "status"}``；
    ``summary`` 折叠空白后最多 120 字。不递归，不读 ``superseded_by`` 反查。
    """
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError("cap must be a non-negative int")
    excluded = {c.get("id") for c in sources}
    found: dict[str, tuple[tuple[int, str, str], dict]] = {}
    for source in sources:
        anchors = links(source.get("anchor_memory_ids"))
        supersedes = links(source.get("supersedes"))
        threads = links(source.get("threads"))
        for card in candidates:
            mid = card.get("id")
            if not isinstance(mid, str) or mid in excluded:
                continue
            reason = ("anchor" if mid in anchors else "supersedes" if mid in supersedes
                      else "thread" if set(threads).intersection(links(card.get("threads")))
                      else "")
            if _is_retired(card) and not (
                reason in {"anchor", "supersedes"} and card.get("status") == "superseded"
            ):
                continue
            summary = _summary(card)
            if reason and summary:
                rank = (0 if reason != "thread" else 1, str(source.get("id") or ""), reason)
                if mid in found and found[mid][0] <= rank:
                    continue
                found[mid] = (rank, {
                    "id": mid, "summary": summary[:_SUMMARY_CHARS],
                    "source_id": source.get("id"), "relation": reason,
                    "status": str(card.get("status") or "active"),
                })
    ordered = sorted(found.values(), key=lambda item: (item[0][0], str(item[1]["id"])))
    return [item for _, item in ordered[:cap]]
