"""从模型输出里切出 JSON 对象时，花括号计数必须认得字符串。

## 背景

旧的切块只数 ``{`` / ``}``，不管它们在不在 JSON 字符串里。于是一条完全合法的回复：

    {"cards":[{"summary":"...","content":"他说 { 这个符号"}]}

多出来的那个 ``{`` 让计数永远回不到 0 → 返回空 → 报 ``no_json_object``，
**合法输出被整份丢掉**。反过来，字符串里的 ``}`` 会让切块提前结束、切出半截。

## 兼容边界

模型经常输出**不合法**的 JSON（最常见的是没转义的引号），这时「在不在字符串里」
本身就不可信。所以只在「认字符串的切法得到的块能直接 json.loads」时才用它；
否则一律退回旧切法 —— 旧切法能处理的输入，结果一个字节都不变。

唯一的例外在 Capture 的出错路径上：旧切法被字符串里的 ``}`` 切断、而认字符串的块
更长时，引号修复先试那个完整块（``quote_repair_candidates``）。``extract_json_block``
本身不变，Dream/Migrate 也不因此多容错。
"""
from __future__ import annotations

import json
import random

import pytest

from memgarden.prompts.capture import parse_capture_cards
from memgarden.prompts.dream import parse_dream_consolidations
from memgarden.prompts.migrate import parse_migrated_cards
from memgarden.text import reasoning
from memgarden.text.card_text import (
    extract_json_block,
    quote_repair_candidates,
    repair_unescaped_quotes,
)

BODY = "正文足够长，能过内容闸的那种，讲清了这件事的前后经过。"


def _capture(content: str, summary: str = "一张正常的卡") -> str:
    return json.dumps({"cards": [{
        "action": "add", "type": "event", "bucket": "工作", "threads": ["领导"],
        "summary": summary, "content": content, "importance": 0.7, "pulse": 0.5,
    }]}, ensure_ascii=False)


# ── 旧切法的冻结副本：回归基准 ────────────────────────────────────────────

def _legacy_first_balanced(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def _legacy_extract(raw: str) -> str:
    text = str(raw or "")
    ok, reply = reasoning.strip_reasoning(text)
    if ok:
        extracted = _legacy_first_balanced(reply)
        if extracted:
            return extracted
    return _legacy_first_balanced(text)


def _frozen_551d13b_extract(raw: str) -> str:
    """551d13b 的 extract_json_block（两种扫描 + 选块规则）冻结副本。"""
    def scan(text: str, start: int, aware: bool) -> str:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            ch = text[index]
            if aware and in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if aware and ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        return ""

    def first(raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
        start = text.find("{")
        if start < 0:
            return ""
        legacy, aware = scan(text, start, False), scan(text, start, True)
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

    text = str(raw or "")
    ok, reply = reasoning.strip_reasoning(text)
    if ok:
        extracted = first(reply)
        if extracted:
            return extracted
    return first(text)


def _loads_ok(block: str, *, repair: bool) -> bool:
    for candidate in ([block, repair_unescaped_quotes(block)] if repair else [block]):
        try:
            json.loads(candidate)
            return True
        except (ValueError, TypeError):
            pass
    return False


# ── C2：字符串里的花括号 ──────────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    "他说 { 这个符号" + BODY,
    "他说 } 这个符号" + BODY,
    "左 {{ 右 }}} 乱序 }{" + BODY,
    '他说"{不行}"就走了' + BODY,          # json.dumps 会转义成 \"
    "反斜杠 \\ 后面跟 { " + BODY,          # 字符串里的 \\ 不能吞掉下一个引号
    '结尾就是转义引号\\"{',
])
def test_braces_inside_strings_do_not_count(content):
    raw = _capture(content)
    assert extract_json_block(raw) == raw
    cards, err = parse_capture_cards(raw, strict=False)
    assert err is None, err
    assert cards[0]["content"] == content


def test_the_reported_example_now_parses():
    raw = '{"cards":[{"title":"t","content":"他说 { 这个符号"}]}'
    assert extract_json_block(raw) == raw
    assert json.loads(extract_json_block(raw))["cards"][0]["content"] == "他说 { 这个符号"


@pytest.mark.parametrize("wrap", [
    "{json}",
    "```json\n{json}\n```",
    "```\n{json}\n```",
    "好的，这是结果：\n{json}\n以上。",
    "<think>草稿 {\"cards\": [ 没写完</think>\n{json}",
    "说明文字里有个\"引号，然后\n```json\n{json}\n```",
])
def test_fences_prose_and_thinking_still_work(wrap):
    obj = _capture("他说 { 这个符号" + BODY)
    raw = wrap.replace("{json}", obj)
    assert extract_json_block(raw) == obj


def test_dream_and_migrate_share_the_fix():
    dream_raw = json.dumps({"consolidations": [{
        "op": "merge", "card_ids": ["a1", "b2"], "rationale": "同一件事",
        "result": {"bucket": "工作", "threads": [], "summary": "一张正常的卡",
                   "content": "他说 { 这个符号" + BODY},
    }], "questions_to_ask": []}, ensure_ascii=False)
    rows, _questions, err = parse_dream_consolidations(dream_raw, strict=False)
    assert err is None, err
    assert rows[0]["result"]["content"].startswith("他说 {")

    migrate_raw = json.dumps({"upgrades": [{
        "id": "m1", "bucket": "工作", "threads": [], "summary": "一张正常的卡",
        "content": "他说 } 这个符号" + BODY,
    }]}, ensure_ascii=False)
    rows, unmigrated, err = parse_migrated_cards(migrate_raw, allowed_ids={"m1"})
    assert err is None, err
    assert unmigrated == [] and rows[0]["content"].startswith("他说 }")


def test_unescaped_quotes_plus_braces_still_reach_the_repair_path():
    """不合法 JSON（没转义的引号）不能因为切块改了就丢掉 0.20.1 的修复。

    ``他说"算了`` 这个奇数引号会把认字符串的扫描带偏：它把后面真正的 ``{``
    当成字符串内容、提前在一个错的 ``}`` 收尾。这时必须退回旧切法的整块。
    """
    from memgarden.text.card_text import _brace_count_block, _string_aware_block

    raw = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
           '"summary":"他说"算了","content":"' + BODY + '","sub":{"a":"她"好"}}]}')
    # 前提：两种扫描在这条输入上确实给出不同的块（否则这条测试什么都没测）
    assert _string_aware_block(raw, 0) not in ("", raw)
    assert extract_json_block(raw) == _legacy_extract(raw) == raw
    cards, err = parse_capture_cards(raw, strict=False)
    assert err is None, err
    assert cards[0]["summary"] == '他说"算了'


# ── 既有花括号、又要补引号：Capture 用完整的块去修 ─────────────────────────

BRACE_AND_QUOTE = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
                   '"summary":"一张正常的卡",'
                   '"content":"她说"好"然后看 } 这个符号，后面还有足够长的正文。"}]}')


@pytest.mark.parametrize("wrap", ["{json}", "```json\n{json}\n```", "好的：\n{json}\n以上。",
                                  "<think>草稿 {</think>\n{json}"])
def test_capture_repairs_the_complete_block_when_a_brace_sits_in_the_quoted_value(wrap):
    """Codex 复审 Important：旧切法在 ``看 }`` 处把块切断，只把半截交给修复 → 报
    ``json_decode_error``。认字符串的切法给出的是完整对象，只是它本身也要修引号才能
    解析，所以以前不会被选中。Capture 现在先修完整块，修不动再退回旧块。
    """
    raw = wrap.replace("{json}", BRACE_AND_QUOTE)
    candidates = quote_repair_candidates(raw)
    assert candidates == [BRACE_AND_QUOTE, extract_json_block(raw)]
    assert len(candidates[1]) < len(candidates[0])
    cards, err = parse_capture_cards(raw, strict=False)
    assert err is None, err
    assert cards[0]["content"] == "她说\"好\"然后看 } 这个符号，后面还有足够长的正文。"


def test_dream_and_migrate_do_not_gain_quote_repair():
    """Dream/Migrate 只接受能直接解析的块，这次不扩大它们的容错。"""
    dream_raw = ('{"consolidations":[{"op":"merge","card_ids":["a1","b2"],"rationale":"同一件事",'
                 '"result":{"bucket":"工作","threads":[],"summary":"一张正常的卡",'
                 '"content":"她说"好"然后看 } 这个符号，后面还有足够长的正文。"}}],"questions_to_ask":[]}')
    rows, _q, err = parse_dream_consolidations(dream_raw, strict=False)
    assert rows == [] and err.startswith("json_decode_error"), err
    migrate_raw = ('{"upgrades":[{"id":"m1","bucket":"工作","threads":[],"summary":"一张正常的卡",'
                   '"content":"她说"好"然后看 } 这个符号，后面还有足够长的正文。"}]}')
    rows, unmigrated, err = parse_migrated_cards(migrate_raw, allowed_ids={"m1"})
    assert rows == [] and unmigrated == ["m1"] and err.startswith("json_decode_error"), err


def test_a_shorter_string_aware_block_is_never_offered_for_repair():
    """奇数个裸引号把认字符串的扫描带偏、提前收尾时，那个短块修出来会是截断的对象。"""
    raw = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
           '"summary":"他说"算了","content":"' + BODY + '","sub":{"a":"她"好"}}]}')
    assert quote_repair_candidates(raw) == [raw]
    assert quote_repair_candidates("没有 JSON") == []
    valid = _capture("他说 } 这个符号" + BODY)
    assert quote_repair_candidates(valid) == [valid]


# ── 回归：旧切法能用的输入，结果逐字节不变 ─────────────────────────────────

def _random_text(rng: random.Random) -> str:
    alphabet = list("abc 你好说了") + ['"', "{", "}", "[", "]", ":", ",", "\\", "\n"]
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))


def _corpus() -> list[str]:
    rng = random.Random(20260914)
    wraps = ["{j}", "```json\n{j}\n```", "前言 {j} 后记", "<think>{{草稿</think>{j}",
             "<thinking>\"}</thinking>\n```\n{j}\n```"]
    out: list[str] = []
    for _ in range(3000):
        doc = {"cards": [{"summary": _random_text(rng), "content": _random_text(rng),
                          "threads": [_random_text(rng) for _ in range(rng.randint(0, 3))]}]}
        text = json.dumps(doc, ensure_ascii=rng.random() < 0.3)
        roll = rng.random()
        if roll < 0.4:
            # 模型式损坏：把若干个 \" 还原成裸引号
            text = text.replace('\\"', '"', rng.randint(1, 3))
        elif roll < 0.5:
            text = text[: rng.randint(1, len(text))]           # 截断
        out.append(rng.choice(wraps).replace("{j}", text))
    return out


def test_inputs_the_legacy_extractor_handled_are_byte_identical():
    """旧切法切出来、且能 json.loads（或经引号修复后能）的输入，新切法必须给出同一个块。

    允许变化的只有旧切法本来就失败的输入：
    - 旧块被字符串里的括号切坏 / 切过头 → 新块必须能直接解析；
    - 旧切法根本没切出块 → 新切法可以给出一个块，交给后面照常解析或报错。
    """
    fixed, still_legacy = 0, 0
    for raw in _corpus():
        legacy = _legacy_extract(raw)
        new = extract_json_block(raw)
        if legacy and _loads_ok(legacy, repair=True):
            assert new == legacy, (raw, legacy, new)
            still_legacy += 1
            continue
        if new == legacy:
            continue
        if legacy:
            assert _loads_ok(new, repair=False), (raw, legacy, new)
        if _loads_ok(new, repair=False):
            fixed += 1
    # 语料确实覆盖到了两类：旧切法能用的，和被字符串里的括号坑掉的
    assert still_legacy > 100 and fixed > 100, (still_legacy, fixed)


def test_extraction_is_unchanged_by_the_candidate_refactor():
    """extract_json_block（Dream/Migrate 和 Capture 的第一次解析都用它）逐字节等于 551d13b。"""
    corpus = _corpus() + [BRACE_AND_QUOTE, "```json\n" + BRACE_AND_QUOTE + "\n```"]
    for raw in corpus:
        assert extract_json_block(raw) == _frozen_551d13b_extract(raw), raw


def test_valid_json_never_reaches_the_repair_and_repair_leaves_it_untouched():
    """合法 JSON：Capture 第一次 json.loads 就成功；即便被误调用，修复也逐字节不动。"""
    rng = random.Random(3)
    for _ in range(2000):
        doc = {"cards": [{"summary": _random_text(rng), "content": _random_text(rng),
                          "threads": [_random_text(rng) for _ in range(rng.randint(0, 3))],
                          "n": [rng.random(), None, True, {"k": [[]]}]}]}
        text = json.dumps(doc, ensure_ascii=rng.random() < 0.3, indent=rng.choice([None, 1]))
        assert repair_unescaped_quotes(text) == text
        assert json.loads(extract_json_block(text)) == doc
