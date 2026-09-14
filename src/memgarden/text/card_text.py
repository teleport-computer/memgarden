"""卡片字段的内容校验 —— capture / dream 共用的「这是真内容,还是把模板抄回来了?」闸。

背景(2026-07-26):一个跑 minimax-M3 的用户,记忆花园里出现了两类垃圾卡:
  1. 标题很长、正文只有 `...` —— 模型把叙述塞进 summary,把示例里的 `...` 抄进 content;
  2. summary/content 都是 `[thickened summary]` 这类方括号占位 —— 模型根本没填,
     只是把输出示例的骨架复述了一遍。

两类都能通过原来的解析:JSON 合法、字段存在、非空。原来的唯一内容判据是
「summary 和 content **全空**才丢」,于是占位符一路写进信封、落进花园,用户能亲眼看到。

这里把判据从「有没有字段」升级成「字段里是不是真内容」。**硬字段**(summary/content)
不合格 = 整行打回;**软字段**(bucket/threads)不合格 = 就地清洗(丢掉那一条),
但在 strict 模式下同样会触发一次重试 —— 软字段抄模板是「模型在复述骨架」的强信号。

模块保持纯函数、零 I/O,以便 resident 与 V2 两条运行时共用一份判据。
"""
from __future__ import annotations

import json
import re
import unicodedata

from . import card_guard, reasoning
from .leak_signals import GENERIC_SIGNALS, LeakSignals
from ..prompts.buckets import normalize_bucket_language

# 卡上会被用户亲眼看到的文字字段(bucket/threads 也显示在花园里)。
_VISIBLE_TEXT_FIELDS = ("summary", "content", "bucket")

# 输出示例里出现过的骨架文字。弱模型常原样抄回来。
_TEMPLATE_FRAGMENTS = (
    "一段厚的正文",
    "被并/被厚化/被取代的卡",
    "merge/supersede 时填被并",
    "拿不准的矛盾",
    "merge | thicken | supersede",
    "add | merge | supersede",
    "event | fact | quote | moment",
    "thickened summary",
    "thickened content",
)

# 方括号/尖括号/花括号占位:[thickened summary]、<summary>、{content}、【正文】。
# 刻意不含圆括号 —— 「(他终于答应去看医生)」是合法写法。
_BRACKET_PLACEHOLDER_RE = re.compile(r"^[\[<{【]\s*[^\]>}】]{0,120}\s*[\]>}】]$")

# 整段就是这么一个占位词时才算(不做子串匹配,避免误伤正常句子)。
_PLACEHOLDER_WORD_RE = re.compile(
    r"^(?:tbd|todo|n/?a|none|null|nil|undefined|placeholder|example|summary|content|"
    r"bucket|thread|threads|string|text|xxx+|待填|待补|占位|示例|同上|略|无)$",
    re.IGNORECASE,
)

# 墓碑注记(2026-08-05 2026-08-05 墓碑卡事故):弱模型把 supersede 语义理解反,往用户可见
# 字段里写「已被 <卡id> 取代——原文」这类记账文。**短语+hex 组合判**——裸判
# 「已被…取代」会误伤正常散文(「旧手机已被新手机取代」),跟上一串 ≥8 位 hex
# 才是系统 id 泄漏的强证据。花园内真实 id 的精确匹配在 dream_gates.known_id_in_text
# (需要 card_map 上下文);这里是无上下文的兜底,capture/dream 四条 lane 全覆盖。
_TOMBSTONE_MARKER_RE = re.compile(
    r"(?:已被\s*[0-9a-f]{8,}|(?:superseded|replaced)\s+by\s+[0-9a-f]{8,})",
    re.IGNORECASE,
)

# 下限刻意压到只能拦「明显没写」(单字残留),不评判写得好不好 —— 卡片写得薄是
# 模型能力问题,不该由这道闸来判死;它只负责拦占位符和空壳。
MIN_SUMMARY_CHARS = 2
MIN_CONTENT_CHARS = 2

_FORMAT_ERROR_PREFIX = "invalid_card_content"
_AFTER_RETRY_ERROR_PREFIX = "invalid_card_content_after_retry"


def _strip_leading_fence(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        # Agents sometimes wrap JSON in a leading ```json fence despite the
        # instruction. Preserve the three former parsers' exact tolerance.
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return text


def _brace_count_block(text: str, start: int) -> str:
    """Legacy scan: count every brace, whether or not it sits inside a string."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def _string_aware_block(text: str, start: int) -> str:
    """JSON-lexing scan: braces inside ``"..."`` (with ``\\`` escapes) don't count."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def _balanced_candidates(raw: str) -> tuple[str, str]:
    """Both scans from the first ``{``: ``(legacy brace-count, string-aware)``.

    Both start at the same index and end at some ``}``, so when they differ
    the shorter one is a prefix of the longer one.
    """
    text = _strip_leading_fence(raw)
    start = text.find("{")
    if start < 0:
        return "", ""
    return _brace_count_block(text, start), _string_aware_block(text, start)


def _choose_block(legacy: str, aware: str) -> str:
    if aware == legacy:
        return legacy
    if aware:
        try:
            json.loads(aware)
        except (ValueError, TypeError):
            pass
        else:
            return aware
    return legacy or aware


def _first_balanced_json_object(raw: str) -> str:
    """Return the first balanced ``{...}`` block, preserving legacy leniency.

    Two scans from the first ``{``:

    - **string-aware** — ignores braces inside JSON strings, so valid output
      like ``{"content": "他说 { 这个符号"}`` is not cut short or discarded;
    - **brace-count** (legacy) — counts every brace.

    Model output is often *invalid* JSON (typically unescaped quotes), and then
    "inside a string" can't be trusted: an odd quote flips the lexer and hides
    real braces. So the string-aware block wins only when it is valid JSON by
    itself. A block that parses can only differ from the legacy one when the
    legacy scan was fooled by a brace inside a string (its block was cut inside
    a string, or ran past the object) — i.e. the legacy result was broken.
    Every other input gets exactly the legacy block.

    Invalid JSON that has a brace inside a string *and* needs the quote repair
    (``"她说"好"然后看 } 这个"``) still gets the legacy block here (cut at that
    ``}``). Capture recovers it through :func:`quote_repair_candidates`; Dream
    and Migrate don't repair, so for them it stays ``json_decode_error``.
    """
    return _choose_block(*_balanced_candidates(raw))


def _extraction_source(raw: str) -> str:
    text = str(raw or "")
    ok, reply = reasoning.strip_reasoning(text)
    if ok and _first_balanced_json_object(reply):
        return reply
    return text


def extract_json_block(raw: str) -> str:
    """Extract JSON from model output without mistaking thinking for the reply.

    Some thinking-capable relays mix ``<think>`` blocks into the same text as
    the requested JSON. Reasoning naturally contains brace-shaped drafts, so
    scanning the raw string from its first ``{`` can select a balanced but
    invalid pseudo-object and discard the valid public reply that follows.

    Scan the reply after the shared thinking parser removes all well-formed
    thinking blocks. If that yields no object—or the thinking parser fails
    closed and returns an empty reply—scan the original text as a compatibility
    fallback. The fallback is important: parser lanes are intentionally more
    permissive than the user-visible reply path and must not reject any shape
    the legacy extractor could parse.
    """
    return _first_balanced_json_object(_extraction_source(raw))


def quote_repair_candidates(raw: str) -> list[str]:
    """Blocks Capture may run :func:`repair_unescaped_quotes` on, in preference order.

    Only for the error path (the :func:`extract_json_block` block already failed
    ``json.loads``). The list always ends with that block. When the
    string-aware scan found a **longer** block — the legacy scan stopped at a
    ``}`` that the string-aware scan reads as string content, e.g.
    ``"content":"她说"好"然后看 } 这个符号"`` — the longer block comes first:
    if it repairs, it is the complete object, while the legacy block was cut in
    the middle of a string.

    A *shorter* string-aware block is never offered: that means an unescaped
    quote flipped the lexer and it closed early, so repairing it could parse a
    truncated object.
    """
    legacy, aware = _balanced_candidates(_extraction_source(raw))
    chosen = _choose_block(legacy, aware)
    out: list[str] = []
    if aware and len(aware) > len(chosen):
        out.append(aware)
    if chosen:
        out.append(chosen)
    return out


_WS = " \t\r\n"
# 字符串外的标量（数字 / true / false / null）由「不是空白、不是这些结构符」的字符
# 组成。修复不判断标量本身合不合法（``True``、``.7`` 留给 ``json.loads`` 去拒），
# 只用它来推进「下一个该出现什么」。
_STRUCTURAL = '{}[]:,"'


def repair_unescaped_quotes(block: str) -> str:
    """把模型写在**对象成员的值**里、却没转义的双引号补上转义。

    只应在 ``json.loads`` 已经失败之后调用 —— 合法 JSON 原样返回，但调用方
    不该依赖这一点去对正常回复跑修复。

    ## 为什么需要它（2026-09-12 prod 事故）

    模型引用用户原话时会这么写：

        "summary": "他一句"没抓住重点"就否了，我加了两个通宵"

    那两个内层引号没转义 → ``json.loads`` 报 ``Expecting ',' delimiter`` →
    **整批卡被丢掉**。再问一次也一样（同样的输入必然同样的输出）。

    prod 实测：152 个有落卡活动的用户里 58 个因此连续两天 0 成功 ——
    因为失败不推进游标，同一条消息被反复重放。

    ## 原则：有歧义就不修

    修复失败，宿主会重问模型；修复"成功"却改掉了意思（数组元素被拼成一个、
    键名吞掉后面的字段）会静默落库，后者更糟。所以任何一处看不准，
    **整份原样返回**，让调用方照旧报解析失败。

    ## 判据：按 JSON 结构走一遍，而不是只看引号后面的那个字符

    扫描时跟踪对象/数组的嵌套，以及"下一个该出现的是键 / 冒号 / 值 / 逗号或收尾"。
    每个字符串按所在位置分成三种角色。字符串里遇到未转义的 ``"`` 时，看它后面
    （跳过空白）的下一个字符 ``c``：

        角色          c 是 , } ] : 或文本结束          c 是其它字符
        对象的键      只有 : 算收尾，其余整份放弃        整份放弃
        数组元素      收尾                             整份放弃
        对象成员的值  收尾                             内容里的引号，转义成 \\"

    收尾之后照 JSON 语法继续检查：键后面必须是 ``:``，值后面必须是 ``,`` 或 ``}``，
    元素后面必须是 ``,`` 或 ``]``；``{`` ``[``、字符串、标量只能出现在该出现值的
    位置。**任何一处不符合，整份放弃。** 走到结尾时对象/数组没闭合、字符串没收尾，
    同样放弃。

    为什么这样够用（也是为什么不再维护"引号后面跟什么 token 算歧义"的白名单）：

    - **漏冒号** ``"is_sensitive" True,"pulse":0.5``：``is_sensitive`` 在键的位置，
      它的收尾引号后面不是 ``:`` → 放弃。后面跟的是 ``True``、``.7``、``NaN``、
      ``'x'`` 还是别的都一样。
    - **数组元素之间分隔符写错** ``["alpha"， "beta"]``、``["alpha” "beta"]``、
      ``["alpha" "beta"]``、``[["a"、"b"]]``：数组元素里出现后面不是 ``,`` ``]``
      的引号 → 放弃。元素没有键，"吞掉了下一个元素"在结构上暴露不出来，所以
      元素里一概不修。
    - **值把后面的成员吞进来** ``"summary":"s"， "content":"c"``：把 ``s"`` 当内容，
      一路走到 ``content`` 的收尾引号 —— 它后面是 ``:``，字符串在这里收尾，
      而值后面出现 ``:`` 不合语法 → 放弃。吞掉后续成员必然要经过下一个
      ``"键":``，所以都会在这里暴露；对象之间写错分隔符（``}，{``）则在字符串外
      直接不合语法。

    ## 支持范围（诚实地说）

    能修：**对象成员的值**里的引号，后面跟着普通文字、数字、别的标点，例如
    ``她说"好的"然后走了``、``他报价"1000"块``、``约在"3点"见``、
    ``He said "ok" and left``、``He said "ok" 5, then left``、
    ``他说"好"、"行"``，以及引语在值末尾的 ``"他只说了"算了""``。

    修不了（按设计，报解析失败）：

    - 值里的引号后面紧跟 ``,`` ``}`` ``]`` ``:``，例如 ``她说"好的", 然后走了``、
      ``标题是"备忘": 周末重做``：这和"字段在这里结束"在字符上无法区分。
    - **数组元素**和**键名**里的裸引号，例如 ``"threads":["他说"算了"那次"]``
      —— 代价是这类线索要重问；换来的是漏逗号/错分隔符的数组不会被静默拼成
      一个元素。
    - 结构本身还有别的错（缺逗号、多逗号、单引号、截断……）。

    已知局限（会按字面解析，而不是报错）：

    - 值里的引号后面恰好是 ``,``，残文又拼成合法 JSON：``"她说"好的","content":"..."``
      与模型真的这么写无法区分。这不是修复引入的，原文本身就是合法 JSON 的读法。
    - 值末尾多敲了一个引号：``"好的""`` 会被读成 ``好的"``，与"引语恰好在值末尾"
      无法区分。
    - 值里的引号后面紧跟 ``}``，而对象恰好能在这里闭合：``{"content":"a "b" }"}``
      读成 ``a "b``，后面的 ``"}`` 被当成块外的残文（取块时旧切法就在这个 ``}``
      处收尾；见 :func:`quote_repair_candidates`）。

    ## 为什么不用提示词让模型"记得转义"

    那是软约束：大多数时候有效、偶尔无声失败。而这里有确定的结构办法。
    模型本来就不擅长在长文本里维持转义状态。
    """
    text = str(block or "")
    if not text:
        return text
    n = len(text)

    def next_non_space(pos: int) -> int:
        while pos < n and text[pos] in _WS:
            pos += 1
        return pos

    out: list[str] = []
    stack: list[str] = []          # 未闭合的 "{" / "["
    # 期待：value / key / key_or_end（刚进对象）/ value_or_end（刚进数组）
    #      / colon / comma（值之后：逗号或收尾）/ done（根已闭合，之后任何非空白字符
    #      都过不了下面的位置检查）
    expect = "value"
    changed = False
    i = 0
    while i < n:
        ch = text[i]
        if ch in _WS:
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            if expect in ("key", "key_or_end"):
                role = "key"
            elif expect in ("value", "value_or_end"):
                role = "value" if stack and stack[-1] == "{" else "element"
            else:
                return text                         # 该出现冒号/逗号的地方来了字符串
            out.append(ch)
            i += 1
            while True:
                if i >= n:
                    return text                     # 字符串没收尾
                c = text[i]
                if c == "\\":
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                if c != '"':
                    out.append(c)
                    i += 1
                    continue
                j = next_non_space(i + 1)
                nxt = text[j] if j < n else ""
                if role == "key":
                    if nxt != ":":
                        return text                 # 漏冒号 / 键名里有引号
                elif nxt not in (",", "}", "]", ":", ""):
                    if role == "element":
                        return text                 # 数组元素里的引号：可能是错分隔符
                    out.append('\\"')               # 值里的内容引号：补转义
                    changed = True
                    i += 1
                    continue
                out.append(c)                       # 字符串收尾
                i += 1
                break
            expect = "colon" if role == "key" else ("comma" if stack else "done")
            continue
        if ch in "{[":
            if expect not in ("value", "value_or_end"):
                return text
            stack.append(ch)
            expect = "key_or_end" if ch == "{" else "value_or_end"
        elif ch in "}]":
            opener, empty_ok = ("{", "key_or_end") if ch == "}" else ("[", "value_or_end")
            if not stack or stack[-1] != opener or expect not in ("comma", empty_ok):
                return text
            stack.pop()
            expect = "comma" if stack else "done"
        elif ch == ":":
            if expect != "colon":
                return text
            expect = "value"
        elif ch == ",":
            if expect != "comma":
                return text
            expect = "key" if stack[-1] == "{" else "value"
        else:
            if expect not in ("value", "value_or_end"):
                return text                         # 例如值后面的 ，、；
            while i < n and text[i] not in _WS and text[i] not in _STRUCTURAL:
                out.append(text[i])
                i += 1
            expect = "comma" if stack else "done"
            continue
        out.append(ch)
        i += 1
    if not changed or stack or expect != "done":
        return text
    return "".join(out)


def _is_substantive_char(ch: str) -> bool:
    """字母或数字(任何语种)。标点、省略号、空白、emoji、组合符号都不算。

    ⚠️ 用 unicodedata 而不是字符区间白名单:早先那版只列了 ASCII/CJK/假名/谚文,
    阿拉伯文、西里尔文、希伯来文的整张卡实义字数会算成 0 → 整语种被误判成
    「只有标点」而全部打回(codex 07-26 review P1-1)。判「有没有字」这件事必须
    语种中立,否则这道闸会变成对非拉丁非 CJK 用户的静默封杀。
    """
    return unicodedata.category(ch)[0] in {"L", "N"}


def substantive_len(text: str) -> int:
    """有效字符数(不含标点/空白/emoji)。"""
    return sum(1 for ch in (text or "") if _is_substantive_char(ch))


def placeholder_reason(text: str) -> str | None:
    """``text`` 是模板残留而非真内容时返回一个短码,否则 None。"""
    s = (text or "").strip()
    if not s:
        return "empty"
    if _BRACKET_PLACEHOLDER_RE.match(s):
        return "bracket_placeholder"
    if _PLACEHOLDER_WORD_RE.match(s):
        return "placeholder_word"
    for fragment in _TEMPLATE_FRAGMENTS:
        if fragment in s:
            return "template_fragment"
    if _TOMBSTONE_MARKER_RE.search(s):
        # 「已被 c42ebb96… 取代」:整理动作的记账注记被当成了内容本身。
        return "tombstone_marker"
    if substantive_len(s) == 0:
        # 纯标点/省略号/emoji:"..."、"…"、"。"、"— —"
        return "no_substantive_chars"
    return None


def card_text_rejection(*, summary: str, content: str, guard: bool = True,
                        signals: LeakSignals = GENERIC_SIGNALS) -> str | None:
    """硬字段体检。返回 ``None``=可以落库,否则 ``"<字段>_<原因>"``。

    与旧判据的差别:旧的是「两个都空才拦」,现在是**两个都必须是真内容**。
    「长标题 + 空正文」正是用户看到的第一类垃圾卡。

    ``guard``:是否额外跑「模型原始输出泄漏」检测(harmony 标记 / 报错回显 / 撕裂尾巴)。
    硬字段命中泄漏 = 整卡打回(与占位符同待遇,走同一个 ``invalid_card_content:*`` 重问路)。
    调用层传入 ``card_guard.guard_enabled()`` 作 kill switch;默认 ON。
    """
    reason = placeholder_reason(summary)
    if reason:
        return f"summary_{reason}"
    reason = placeholder_reason(content)
    if reason:
        return f"content_{reason}"
    if guard:
        # 硬字段用从严判据(强证据 / ≥2弱共现)—— 误杀=整卡丢弃,代价高。
        if card_guard.hard_field_pollution_reason(summary, signals):
            return "summary_protocol_leak"
        if card_guard.hard_field_pollution_reason(content, signals):
            return "content_protocol_leak"
    if substantive_len(summary) < MIN_SUMMARY_CHARS:
        return "summary_too_short"
    if substantive_len(content) < MIN_CONTENT_CHARS:
        return "content_too_short"
    return None


def sanitize_card_labels(
    *, bucket: str, threads: list[str], guard: bool = True, lang_text: str = "",
    signals: LeakSignals = GENERIC_SIGNALS,
) -> tuple[str, list[str], list[str]]:
    """清洗软字段。返回 ``(bucket, threads, reasons)``,``reasons`` 供调用方观测。

    ⚠️ ``reasons`` **不参与**打回判定 —— 硬内容(summary/content)完全正常、
    只是 ``bucket="无"`` 的卡不值得为它多烧一次 BYOK 调用;软字段的定义就是
    「能就地修好」(codex 07-26 review P1-2)。当前两个 parser 都只取清洗结果、
    不消费 ``reasons``(纯模块无 logger),所以线上暂时看不到软字段洗了什么;
    要观测就在调用侧接进 trajectory,别改成打回条件。

    ``lang_text``:卡片正文(summary+content),用来把 COMMON 桶归到卡片语言(中文卡的
    "Pets" → "宠物")。**这是 Q3 的收口点**:``normalize_bucket_language`` 原来只在明文
    actions 路径跑,capture/dream/migrate 提前封信封、绕过了它;现在软字段清洗这层(三条路
    共用)统一跑一次。留空则不归一(向后兼容旧调用)。自定义/未知桶原样通过。
    """
    reasons: list[str] = []
    clean_bucket = (bucket or "").strip()
    reason = placeholder_reason(clean_bucket) if clean_bucket else None
    if reason:
        reasons.append(f"bucket_{reason}")
        clean_bucket = ""
    # 软字段的「模型原始输出泄漏」:桶另查精确 taxonomy denylist,命中即丢(降级到默认桶
    # 由调用层做);threads 逐项丢脏项、留干净项。硬字段(summary/content)才整卡打回。
    elif guard and clean_bucket and card_guard.bucket_pollution_reason(clean_bucket, signals):
        reasons.append("bucket_protocol_leak")
        clean_bucket = ""
    # Q3:干净桶按卡片语言归一(COMMON 桶换语言;自定义桶不动)。
    if clean_bucket and lang_text:
        clean_bucket = normalize_bucket_language(clean_bucket, lang_text)
    clean_threads: list[str] = []
    for thread in threads or []:
        text = str(thread or "").strip()
        if not text:
            continue
        reason = placeholder_reason(text)
        if reason:
            reasons.append(f"threads_{reason}")
            continue
        if guard and card_guard.field_pollution_reason(text, signals):
            reasons.append("threads_protocol_leak")
            continue
        clean_threads.append(text)
    return clean_bucket, clean_threads, reasons


_USER_TOKEN_RE = re.compile(r"用户|(?i:\buser\b)")


def count_user_token_residuals(card: dict) -> int:
    """确定性改写之后,可见字段里还剩几处「用户」/「user」。

    ⚠️ 这数的是**「用户/user」这个 token 出现了几次**,不是「称谓泄漏了几次」——
    其中一部分完全可能是本人在正当地聊自己的产品用户(「用户留存这个月掉了」)。
    别把它当人称泄漏率读(codex 07-26 review P2)。

    这是这道防线的**诚实刻度**:改写判据只保留紧邻谓词锚点,刻意不猜词性 ——
    「用户体验了新功能」既可能是本人试用、也可能是泛用户试用,词法上无法区分,
    猜错会确定性地改坏本人真实内容。所以必然有残留。

    残留率是我们唯一能验证 ①转写标签 + ②prompt 去前缀到底管不管用的信号 ——
    否则只能靠猜。两条运行时都接:V2 记进 trajectory,resident 记进 job extra。
    """
    total = 0
    for field in _VISIBLE_TEXT_FIELDS:
        value = card.get(field)
        if isinstance(value, str):
            total += len(_USER_TOKEN_RE.findall(value))
    threads = card.get("threads")
    if isinstance(threads, list):
        for thread in threads:
            if isinstance(thread, str):
                total += len(_USER_TOKEN_RE.findall(thread))
    return total


def format_error(reasons: list[str], *, after_retry: bool = False) -> str:
    """把逐行的拒绝码收敛成一个可进 job status 的短 reason。

    ``after_retry=True`` 换一个前缀:那是「打回重问之后还是全脏」的终局,
    调用方必须让 job **失败**(而不是伪装成 noop 推进 frontier),
    data-track 上也要能和第一问的打回分开看。
    """
    prefix = _AFTER_RETRY_ERROR_PREFIX if after_retry else _FORMAT_ERROR_PREFIX
    seen: list[str] = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    if not seen:
        return prefix
    return f"{prefix}:" + ",".join(seen[:3])


def is_card_format_error(err: str | None) -> bool:
    """这个 reason 是不是「值得原样打回去重来一次」的格式问题。

    只认内容闸**第一问**发的码；不包含 provider 故障或 JSON 解析失败。
    空正文等解析失败是否值得重问，由 ``is_retryable_parse_error`` 单独判断。
    ``invalid_card_content_after_retry`` 也刻意不在此列:那已经是第二问的终局,
    再打回就成了死循环。
    """
    return bool(err) and str(err).split(":", 1)[0] == _FORMAT_ERROR_PREFIX


def is_retryable_parse_error(err: str | None, *, raw: str | None = None) -> bool:
    """Whether a memory parser failure deserves one corrective model call.

    Keep :func:`is_card_format_error` narrow because other callers use it to
    identify content-gate failures specifically. Capture and Dream also retry
    a balanced object that failed JSON decoding. This
    commonly means a thinking model put a pseudo-JSON draft before its valid
    answer. An empty successful reply also deserves a corrective call, using
    the same session retry budget (not a new provider retry loop). Nonempty
    prose keeps the existing terminal policy; explicit truncation is handled
    by the session before parsing, and provider errors stay with the host.
    """
    prefix = str(err or "").split(":", 1)[0]
    return (is_card_format_error(err) or prefix == "json_decode_error"
            or (prefix == "no_json_object" and raw is not None and not raw.strip()))


_REASON_TEXT = {
    "summary_empty": "summary 是空的",
    "summary_bracket_placeholder": "summary 还是方括号占位(例如 [thickened summary])",
    "summary_placeholder_word": "summary 只填了一个占位词",
    "summary_template_fragment": "summary 抄了输出示例里的说明文字",
    "summary_no_substantive_chars": "summary 只有标点/省略号,没有字",
    "summary_too_short": "summary 太短,看不出这张卡是什么",
    "content_empty": "content 是空的",
    "content_bracket_placeholder": "content 还是方括号占位",
    "content_placeholder_word": "content 只填了一个占位词",
    "content_template_fragment": "content 抄了输出示例里的说明文字(例如「一段厚的正文」)",
    "content_no_substantive_chars": "content 只有省略号/标点,没有正文",
    "content_too_short": "content 太短,不是一段正文",
    "summary_tombstone_marker": "summary 写成了「已被 <卡id> 取代」这类整理注记 —— 应该写合并/整理后的内容本身,卡片字段里永远不要出现卡 id",
    "content_tombstone_marker": "content 写成了「已被 <卡id> 取代」这类整理注记 —— 应该写合并/整理后的完整正文,卡片字段里永远不要出现卡 id",
    "summary_contains_card_id": "summary 里出现了内部卡 id —— 卡片字段是本人会看到的记忆内容,不要引用任何卡 id",
    "content_contains_card_id": "content 里出现了内部卡 id —— 卡片字段是本人会看到的记忆内容,不要引用任何卡 id",
    "summary_protocol_leak": "summary 里混进了模型的原始输出/协议残片(harmony 标记、报错回显或撕裂的 JSON),不是真正的记忆内容",
    "content_protocol_leak": "content 里混进了模型的原始输出/协议残片,不是真正的记忆内容",
    "bucket_protocol_leak": "bucket 混进了协议残片/机器分类串",
    "threads_protocol_leak": "threads 里有条目混进了协议残片",
    "bucket_bracket_placeholder": "bucket 是占位符",
    "bucket_placeholder_word": "bucket 只填了一个占位词",
    "bucket_no_substantive_chars": "bucket 只有标点",
    "bucket_template_fragment": "bucket 抄了示例",
    "threads_bracket_placeholder": "threads 里有占位符",
    "threads_placeholder_word": "threads 里有占位词",
    "threads_no_substantive_chars": "threads 里有只剩省略号的条目",
    "threads_template_fragment": "threads 抄了示例",
}


def _problem_text(err: str | None) -> str:
    codes = str(err or "").split(":", 1)[-1].split(",") if err else []
    parts = [_REASON_TEXT.get(code.strip()) for code in codes if code.strip()]
    named = [p for p in parts if p]
    return "；".join(named) if named else "有字段没有真正填写"


def build_format_retry_prompt(prompt: str, err: str, *, empty_example: str) -> str:
    """在原 prompt 后面追加一段「打回重做」的说明,用于同一 lane 的第二次尝试。

    刻意保留原 prompt 全文(而不是只发一段纠错):模型这一轮是无状态的,
    上下文(现有的卡/这段对话)必须再给一次,否则它只能凭空编。
    """
    return (
        f"{prompt}\n\n"
        "【上一次的输出被打回,请重做】\n"
        f"问题:{_problem_text(err)}。\n"
        "这不是 JSON 语法错误 —— 是字段里装的不是真内容:要么把输出示例里的占位符"
        "(`...`、方括号里的说明、「一段厚的正文」之类)原样抄了回来或留了空,"
        "要么写成了整理动作的注记(「已被 xxx 取代」、引用卡 id)。\n"
        "这些卡是本人会亲眼看到的记忆:占位符会显示成空白卡片,"
        "注记和卡 id 会显示成一张看不懂的坏卡。\n"
        "\n"
        "再来一次,仍然只输出 JSON,并且:\n"
        "· summary:一句真实的话,一眼看出这张卡是什么。不能是 `...`、`[...]`、空字符串。\n"
        "· content:完整的一段正文(至少一两句),写清楚发生了什么。不能只有省略号,"
        "也不能只是把 summary 重复一遍。\n"
        "· 字段里写的是记忆内容本身 —— 不要写「已被 X 取代」这类说明,不要出现任何卡 id。\n"
        "· bucket / threads:写具体的词,不要照抄示例里的 `...`。\n"
        f"· 如果其实没有值得写的,就输出 {empty_example} —— 空结果完全可以接受,"
        "比填占位符好得多。\n"
    )


def build_truncation_retry_prompt(prompt: str) -> str:
    """重问一次被输出上限截断的抽取，保持原预算但要求更紧凑。"""
    return (
        f"{prompt}\n\n"
        "【上一次输出因长度上限被截断，请完整重做】\n"
        "请保持原有 JSON 结构，只保留必要信息并更简洁地表达，确保 JSON 完整闭合。\n"
    )
