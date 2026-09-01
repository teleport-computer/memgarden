"""把模型输出里的「推理块」剥掉，再交给 JSON 解析。

## 为什么内核需要这个

Garden 要模型吐 JSON。而**会思考的模型自己就会**把推理写在同一段输出里
（DeepSeek-R1、QwQ 这类原生带 `<think>`，一些中转也会混进来）—— 不是谁教它的。

推理文字里天然有大量花括号形状的草稿。从第一个 ``{`` 开始扫，很可能扫到那些
草稿、拼出一个「括号配平但内容错误」的伪对象，把后面真正的答案丢掉。所以要先
把推理块剥干净再扫。

**这个需求跟任何具体宿主无关** —— 用 Garden 的人只要模型会思考就会遇到。

## 这不是「思维链功能」

分清楚两件事，它们只是长得像：

    这里做的     别让推理文字干扰我的 JSON 解析      ← 解析健壮性
    宿主做的     把推理抽出来给用户看、判断写没写完  ← 产品功能

后者是宿主的事：怎么措辞让模型思考、思考该多长、失败了给用户看什么、
用哪个环境变量控制 —— **一样都不属于「记忆的判断力」，所以一样都不在这里。**
早先这块曾经和宿主的整套思维链实现放在同一个包里，那是错的：接入方装
Garden 会连带拿到别人产品的人格文案和开关。

## 失败就失败关闭

剥完只要正文里还剩任何标签，就返回 ``ok=False`` —— 宁可退回「扫原文」的兜底
路径，也绝不把带标签的残文当成正文喂给解析器。
"""
from __future__ import annotations

import re

# 认这几种写法。longest-first，避免 "think" 抢在 "thinking" 前面匹配。
_TAG_WORDS = ("thinking", "reasoning", "thought", "think")
_TAG_ALT = "|".join(_TAG_WORDS)

# 标签名边界。**不能用 `\b`** —— `\b` 在 `t` 和 `-` 之间成立，于是
# `<thought-process>` 这种合法 XML/JSX 标签会被判成残留、整段白白失败关闭。
# XML 名称允许 `-` `.` `:`，边界必须显式排掉它们。
_NAME_END = r"(?![\w:.-])"

# 一整对同名标签。开闭必须同名（`(?P=tag)`），否则 `<think>…</reasoning>`
# 这种错配会被当成一块合法结构剥掉。
_PAIRED_BLOCK = re.compile(
    rf"<\s*(?P<tag>{_TAG_ALT}){_NAME_END}\s*>(?P<body>.*?)<\s*/\s*(?P=tag){_NAME_END}\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 剥完之后判「还有没有残留」。任何开或闭标签都算。
_RESIDUE = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT}){_NAME_END}", re.IGNORECASE)
# 孤立闭标签：推理按约定写在最前面，所以一个配不上对的 `</think>` 说明它前面
# 全是推理（开标签在上游某处被吃掉了）。
_LONE_CLOSE = re.compile(rf"<\s*/\s*(?:{_TAG_ALT}){_NAME_END}\s*>", re.IGNORECASE)
_ANY_TAG = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})\b", re.IGNORECASE)


def strip_reasoning(text: str) -> tuple[bool, str]:
    """返回 ``(ok, reply)``。

    ``ok=False`` 表示结构乱到不可信，``reply`` 为空 —— 调用方应当回退到扫原文，
    而不是拿一段可能被截断的正文继续解析。
    """
    raw = str(text or "")
    if not _RESIDUE.search(raw):
        # 没有标签就一个字节都不碰。这是快路径，也是第二道保险。
        return True, raw

    found_block = False

    def _take(match: "re.Match[str]") -> str:
        nonlocal found_block
        body = match.group("body") or ""
        # 块里还套着别的标签 = 结构已乱，留在原地，由下面的残留检查失败关闭。
        if _ANY_TAG.search(body):
            return match.group(0)
        if body.strip():
            found_block = True
        return "\n"

    reply = _PAIRED_BLOCK.sub(_take, raw)

    lone = _LONE_CLOSE.search(reply)
    if lone is not None:
        head = reply[: lone.start()].strip()
        # head 里还带标签 = 开闭错配或多层残骸，不是「开标签被吃掉」那种可救的形状。
        if _RESIDUE.search(head):
            return False, ""
        # 已经剥出过完整块、却还剩一个带内容的孤立闭标签 —— 结构本身就乱了。
        # 此时把 head 当推理会把真正的正文吞掉：
        #     <think>A</think>正文甲</think>正文乙  → 正文甲消失
        if found_block and head:
            return False, ""
        if head:
            found_block = True
        reply = reply[lone.end():]

    if _RESIDUE.search(reply):
        return False, ""
    if not found_block:
        # 有标签、却一块内容都没剥出来（例如只有一个空标签对）—— 同样不可信。
        return False, ""

    return True, re.sub(r"\n{3,}", "\n\n", reply).strip()
