"""一个花园用哪种语言 —— 算法在这里，数据由宿主喂。

## 为什么这条属于内核

判断「这个花园是中文的还是英文的」有两部分：

    数据    这个人的身份卡、历史记忆、客户端 locale、已有的桶名
            → 宿主的，内核碰不到

    算法    给定这些证据，怎么得出结论
            → 通用的，任何接入方都要做同一件事

算法留在宿主侧的代价是真实的：**2026-08-24 宿主 io 出过一次线上事故**，一个中文
用户的花园两天内整个翻成英文。根因是它自己实现的判据 —— 比 CJK 与拉丁**字符数**，
而中英文桶名长度根本不对等：

    中文桶平均  3.4 字符（「工作」）
    英文桶平均 11.6 字符（「Our relationship」）
    → 一个英文桶顶三个半中文桶

「6 个中文桶 + 3 个英文桶」就判成英文花园。**算法在内核里，别的接入方就不必再踩
一遍这个坑。**

## 两条设计约束

**一个桶一票，长度不参与。** 上面那次事故的直接教训。

**平票保持原状（默认中文）。** 不对称是刻意的：把一个花园翻成另一种语言，是用户
**能立刻看见的破坏**（他的记忆突然换了语言）；保持不变最多是"没跟上"。两种错误
的代价差得远，判据就该偏向不动。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")

#: 桶名清单常以一行文本传进来。这些分隔符覆盖中英两种写法。
_SPLIT = re.compile(r"[、,/\n]+")

DEFAULT_LOCALE = "zh-Hans"


def split_bucket_names(text: str | Iterable[str]) -> list[str]:
    """把桶名清单规整成一个个名字。已经是列表就原样清洗。"""
    if isinstance(text, str):
        parts: Sequence[str] = _SPLIT.split(text)
    else:
        parts = list(text)
    return [str(p).strip() for p in parts if str(p).strip()]


def count_bucket_languages(buckets: str | Iterable[str]) -> tuple[int, int]:
    """返回 (中文桶数, 英文桶数)。**按桶计，不按字符计。**

    含任何 CJK 的算中文桶；不含 CJK 但含拉丁字母的算英文桶。
    两者都不含的（纯数字、纯符号）不投票 —— 它们不携带语言信息。
    """
    zh = en = 0
    for name in split_bucket_names(buckets):
        if _CJK.search(name):
            zh += 1
        elif _LATIN.search(name):
            en += 1
    return zh, en


def garden_language_from_buckets(
    buckets: str | Iterable[str],
    *,
    default: str = DEFAULT_LOCALE,
) -> str | None:
    """已有桶名指向哪种语言。没有可用证据时返回 ``None``，让宿主接着看别的信号。

    返回 ``None`` 而不是 ``default``，是为了让「没有证据」和「证据指向默认语言」
    这两件事在调用方那里可分辨 —— 前者该继续找别的依据，后者不该。
    """
    zh, en = count_bucket_languages(buckets)
    if not zh and not en:
        return None
    return default if zh >= en else ("en" if default != "en" else "zh-Hans")


def decide_garden_language(
    buckets: str | Iterable[str] = "",
    *,
    fallbacks: Sequence[str | None] = (),
    default: str = DEFAULT_LOCALE,
) -> dict:
    """完整的判定，带**依据**一并返回，供宿主落库观测。

    ``fallbacks`` 是宿主按优先级排好的其它信号（用户明说的语言偏好、身份卡文本的
    主语言、客户端 locale…）—— 内核不知道它们从哪来，只负责在**没有桶**时依次取用。

    为什么要返回依据：语言判错是「看得见症状、看不见原因」的一类问题 —— 用户只会
    说「怎么变英文了」。没有这条记录，事后查不到当时算的是什么、凭什么算的。

    返回的字段全部**内容无关**：语言标签、依据名、桶的计数。桶名本身是用户写的
    内容，不出现在返回值里。
    """
    zh, en = count_bucket_languages(buckets)
    if zh or en:
        return {
            "locale": default if zh >= en else ("en" if default != "en" else "zh-Hans"),
            "basis": "existing_buckets",
            "bucket_zh": zh,
            "bucket_en": en,
            "bucket_total": zh + en,
        }
    for i, hint in enumerate(fallbacks):
        if hint:
            return {"locale": str(hint), "basis": f"fallback_{i}",
                    "bucket_zh": 0, "bucket_en": 0, "bucket_total": 0}
    return {"locale": default, "basis": "default",
            "bucket_zh": 0, "bucket_en": 0, "bucket_total": 0}
