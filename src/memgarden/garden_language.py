"""一个花园用哪种语言 —— 算法在这里，证据由宿主喂。

## 判据里没有桶名，这是刻意的

最容易想到的做法是「看这个花园现在的桶名是中文还是英文」。**那是错的，而且错得
不显眼**，所以这段要写清楚，免得以后有人"顺手加回去"。

**理由一：桶名是输出，不是输入。** 桶是 AI 自己写的。拿它当判据，等于让上一轮的
输出决定下一轮的输入 —— 一个自我强化的环。宿主 io 在 2026-08-24 被这个环咬过：
更早一个 bug 让约 1/3 的中文记忆贴上了英文桶，这些残留被读成「这是英文花园」，
于是新卡全用英文桶，英文桶更多……真实用户的中文花园两天内整个翻成了英文。

**理由二（更致命）：桶名里根本没有语言信息。** 桶名大量是专有名词 —— 人名、公司名、
项目名、品牌名：

    工作、健康、James、Sarah、Mike        ← 一个中文用户,给三个朋友各建了个桶
    工作、James、OpenAI、GitHub、Figma    ← 一个中文用户,在记几个项目

按「拉丁字母的桶算英文票」来数，这两个花园都会被判成英文。可这个人从头到尾说中文。
**人名不该被翻译，也不该被当成语言证据。** 数票方式怎么改都救不了这一条 —— 问题不在
怎么数，在于压根不该数它。

## 那「别让 工作 和 Work 并存」谁来管

归一化，不是语言判定。见 :func:`memgarden.prompts.buckets.normalize_bucket_language`：
它只动**固定配对表里的通用桶**（健康 ↔ Health），自定义桶原样放行。所以 James 还是
James，而花园语言由下面这些证据决定。

两件事分开做的好处正在于此：**语言跟着人走，桶名跟着内容走。**

## 证据顺序

    ① 用户明说的语言偏好      最强,任何东西都不该盖过它
    ② 他实际在用什么语言写    身份卡正文、最近的消息 —— 真实信号,但要够量才算数
    ③ 客户端 locale          弱,只是设备设置,不一定代表他想用什么语言记东西
    ④ 默认

**平票/证据不足时保持默认，不翻转。** 不对称是刻意的：把一个花园翻成另一种语言，是
用户**能立刻看见的破坏**（他的记忆突然换了语言）；保持不变最多是"没跟上"。两种错误
的代价差得远，判据就该偏向不动。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")

#: 桶名清单常以一行文本传进来。这些分隔符覆盖中英两种写法。
_SPLIT = re.compile(r"[、,/\n]+")

DEFAULT_LOCALE = "zh-Hans"

#: 「他在用什么语言写」要多少**词**才算数。低于这个量的样本噪声太大 —— 一句
#: "ok thanks" 不该把一个中文花园推向英文。
MIN_WRITING_EVIDENCE = 12
#: 优势方要占到多少比例才算数。中文用户天天夹英文技术词，60% 出头才判得动。
MIN_WRITING_CONFIDENCE = 0.62


def split_bucket_names(text: str | Iterable[str]) -> list[str]:
    """把桶名清单规整成一个个名字。已经是列表就原样清洗。"""
    if isinstance(text, str):
        parts: Sequence[str] = _SPLIT.split(text)
    else:
        parts = list(text)
    return [str(p).strip() for p in parts if str(p).strip()]


def count_bucket_languages(buckets: str | Iterable[str]) -> tuple[int, int]:
    """返回 (含 CJK 的桶数, 只含拉丁字母的桶数)。

    ⚠️ **这是观测量，不是判据。** 不要拿它决定花园语言 —— 上面模块开头写了为什么
    （人名、公司名、项目名全是拉丁字母，却不携带任何语言信息）。

    留着它是为了诊断：「这个花园判成中文，但 9 个桶里 7 个是拉丁字母」是一条值得
    记一笔的观测 —— 可能是归一化没生效，也可能人家就是有一堆外国朋友。**值得看一眼，
    不值得据此改语言。**
    """
    zh = en = 0
    for name in split_bucket_names(buckets):
        if _CJK.search(name):
            zh += 1
        elif _LATIN.search(name):
            en += 1
    return zh, en


#: 一串连续的拉丁字母算一个词。撇号让 "don\'t" 不被拆成两个。
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:\'[A-Za-z]+)*")


def count_language_units(text: str) -> tuple[int, int]:
    """返回 ``(中文词数, 英文词数)``。

    ⚠️ **按词计，不按字符计。** 中文一个汉字≈一个词，英文一个词≈5 个字母。直接比
    字符数，中文永远吃亏 —— 一个中文用户随口夹几个技术词（review / pipeline /
    manager），拉丁字符就过半了，判据会说他是英文用户。

    这跟 2026-08-24 那次事故是**同一个错**：那次是「中文桶 2 字符 vs 英文桶 15
    字符」，这次是「中文词 1 字符 vs 英文词 5 字符」。教训一样 —— **先把单位对齐
    再比大小**。
    """
    return len(_CJK.findall(text or "")), len(_LATIN_WORD.findall(text or ""))


def writing_language(texts: str | Iterable[str], *, default: str = DEFAULT_LOCALE) -> tuple[str | None, float, int]:
    """这个人实际在用什么语言写。返回 ``(语言 | None, 把握度, 证据词数)``。

    证据不足或两边势均力敌时返回 ``None`` —— 让调用方接着看下一级证据，而不是
    拿一个 51:49 的结论去翻别人的花园。
    """
    blob = texts if isinstance(texts, str) else "\n".join(str(t or "") for t in texts)
    cjk, latin = count_language_units(blob)
    total = cjk + latin
    if total < MIN_WRITING_EVIDENCE:
        return None, 0.0, total
    confidence = max(cjk, latin) / total
    if confidence < MIN_WRITING_CONFIDENCE:
        return None, confidence, total
    other = "en" if default != "en" else "zh-Hans"
    return (default if cjk > latin else other), confidence, total


def decide_garden_language(
    *,
    explicit: str | None = None,
    written: str | Iterable[str] = (),
    locale: str | None = None,
    default: str = DEFAULT_LOCALE,
) -> dict:
    """按证据强弱定这个花园的语言，并把**依据**一并返回，供宿主落库观测。

    为什么要返回依据：语言判错是「看得见症状、看不见原因」的一类问题 —— 用户只会
    说「怎么变英文了」。没有这条记录，事后查不到当时算的是什么、凭什么算的。
    2026-08-24 那次事故正是如此：症状明显，却没有一条记录能说明判据看到了什么。

    返回的字段全部**内容无关**：语言标签、依据名、证据的词计数。用户写的文本本身
    不出现在返回值里。

    ⚠️ 签名里**没有 buckets**。这是设计，不是遗漏 —— 见模块开头。
    """
    if explicit:
        return {"locale": str(explicit), "basis": "explicit_preference",
                "evidence_units": 0, "confidence": 1.0}

    lang, confidence, chars = writing_language(written, default=default)
    if lang:
        return {"locale": lang, "basis": "writing_language",
                "evidence_units": chars, "confidence": round(confidence, 3)}

    if locale:
        return {"locale": str(locale), "basis": "client_locale",
                "evidence_units": chars, "confidence": 0.0}

    return {"locale": default, "basis": "default",
            "evidence_units": chars, "confidence": 0.0}
