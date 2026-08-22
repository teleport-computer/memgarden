"""把你的记录翻译成内核认的卡片形状。

## 为什么需要它

内核只认**一种**卡：`summary` / `content` / `bucket` / `threads`。
你的库大概率不长这样：

    Notion      Name / Notes
    Obsidian    frontmatter.title / 正文
    你自己的    随便叫什么

翻译这件事归宿主 —— 让内核去适应每种宿主的字段名，等于把所有宿主的历史
都背进内核。但「怎么翻译」是有套路的，所以这里提供工具，不提供猜测。

## 最容易踩的坑：搜索语料

`summary` 是**给人看的**（会进日志、可能返回给客户端），
`search_text` 是**给机器比对的**（只在内部用）。两者必须分开：

    宿主 io 踩过 —— 翻译时只给了 summary/content，老卡的标题
    「那次你说想学吉他」退出了匹配范围，问「吉他」直接召不回来。

所以 `to_card()` 会把**所有**你声明为可搜索的字段拼进 `search_text`，
而 `summary` 只取第一个非空的可公开字段。

## 用法

    mapping = FieldMap(
        summary_fields=("Name",),          # 可公开的摘要从哪来
        text_fields=("Name", "Notes"),     # 参与搜索的全部字段
        private_fields=("Notes",),         # 参与搜索但绝不外泄
    )
    card = to_card({"Name": "养狗", "Notes": "养了一只柯基"}, mapping)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class FieldMap:
    """一个库的字段映射。不可变 —— 换库＝换实例，不是改字段。"""

    #: 取「一句话摘要」的优先级，第一个非空者胜出。
    #: ⚠️ 只放**可公开**的字段：这个值会进日志、可能返回给客户端。
    summary_fields: tuple[str, ...] = ("summary", "title")

    #: 参与搜索比对的全部字段。顺序即拼接顺序。
    text_fields: tuple[str, ...] = ("summary", "content")

    #: 参与搜索但绝不进摘要的字段（正文一类）。
    private_fields: tuple[str, ...] = ("content",)

    #: 元数据字段，原样带给内核（它只读不写）。
    passthrough_fields: tuple[str, ...] = (
        "id", "occurred_at", "created_at", "is_sensitive", "source", "roles",
    )


DEFAULT_FIELD_MAP = FieldMap()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def summary_of(record: dict, mapping: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """取可公开的一句话摘要。取不到就返回空串 —— **不用正文兜底**。

    返回空是合法结果：只有正文没有摘要的记录，摘要就该是空，
    它靠 `search_text` 进候选池，而不是靠把正文伪装成摘要。
    """
    if not isinstance(record, dict):
        return ""
    for key in mapping.summary_fields:
        text = _clean(record.get(key))
        if text:
            return text
    return ""


def search_text_of(record: dict, mapping: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """拼出参与搜索的全部文本。空字段跳过，不留空位。"""
    if not isinstance(record, dict):
        return ""
    parts = [_clean(record.get(k)) for k in mapping.text_fields]
    return " ".join(p for p in parts if p)


def private_text_of(record: dict, mapping: FieldMap = DEFAULT_FIELD_MAP) -> str:
    if not isinstance(record, dict):
        return ""
    parts = [_clean(record.get(k)) for k in mapping.private_fields]
    return " ".join(p for p in parts if p)


def to_card(record: dict, mapping: FieldMap = DEFAULT_FIELD_MAP) -> dict:
    """把一条记录翻成内核认的卡。

    ⚠️ 漏了这一步的后果是**静默的**：内核读不到文本就当这条没内容，
    整条丢出候选池 —— 症状是「东西在库里，但想不起来」。
    """
    if not isinstance(record, dict):
        return {}
    card = {
        "summary": summary_of(record, mapping),
        "content": private_text_of(record, mapping),
        "bucket": _clean(record.get("bucket")),
        "threads": [t for t in (record.get("threads") or []) if _clean(t)],
        "search_text": search_text_of(record, mapping),
    }
    for key in mapping.passthrough_fields:
        if key in record:
            card[key] = record[key]
    return card


def to_cards(records: Sequence[dict], mapping: FieldMap = DEFAULT_FIELD_MAP) -> list[dict]:
    return [to_card(r, mapping) for r in records if isinstance(r, dict)]
