"""模型把双引号写进 JSON 字符串却不转义 —— 修它，而不是丢掉整批卡。

## 背景（2026-09-12 prod 事故）

模型引用用户原话时会这么写：

    "summary": "他一句"没抓住重点"就否了"

内层引号没转义 → json.loads 报 Expecting ',' delimiter → **整批卡丢掉**。
再问一次也一样：同样的输入必然同样的输出。

而落卡失败**不推进游标**，所以同一条消息会被反复重放。prod 实测：
152 个有落卡活动的用户里 58 个连续两天 0 成功。用户的感受是
「io 不再记东西了，做梦也不整理了」——后者是因为没有新卡，dream 判 seed 不够。

## 为什么不靠提示词让模型记得转义

软约束：大多数时候有效、偶尔无声失败，而模型本来就不擅长在长文本里维持转义
状态。这里有确定的结构办法，就该用结构。
"""
from __future__ import annotations

import json

import pytest

from memgarden.prompts.capture import parse_capture_cards
from memgarden.text.card_text import repair_unescaped_quotes

CARD = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
        '"threads":["领导"],"summary":{SUMMARY},'
        '"content":"正文足够长，能过内容闸的那种，讲清了这件事的前后经过。",'
        '"importance":0.7,"pulse":0.5}]}')


def test_a_quoted_phrase_inside_a_value_is_repaired():
    """最常见的形态：引用用户原话。"""
    broken = CARD.replace("{SUMMARY}", '"他一句"没抓住重点"就否了"')
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    fixed = json.loads(repair_unescaped_quotes(broken))
    # 🔴 引号必须还在内容里 —— 修的是转义，不是把内容改掉
    assert fixed["cards"][0]["summary"] == '他一句"没抓住重点"就否了'


def test_the_parser_recovers_instead_of_dropping_the_batch():
    """端到端：坏 JSON 进去，卡出来。"""
    broken = CARD.replace("{SUMMARY}", '"领导说"重点不对"，我很挫败"')
    cards, err = parse_capture_cards(broken, strict=False)
    assert err is None, err
    assert len(cards) == 1
    assert "重点不对" in cards[0]["summary"]


def test_valid_json_is_returned_byte_identical():
    """🔴 正常回复**一个字节都不能动**。

    修复只跑在出错路径上。这条测试守住这一点 —— 否则这个修复本身
    就成了一个会悄悄改内容的风险。
    """
    good = CARD.replace("{SUMMARY}", '"方案被否了，周末重做"')
    assert repair_unescaped_quotes(good) == good
    # 已经正确转义的也不许再动
    escaped = CARD.replace("{SUMMARY}", r'"他说\"不行\"就走了"')
    assert repair_unescaped_quotes(escaped) == escaped
    assert json.loads(repair_unescaped_quotes(escaped))["cards"][0]["summary"] \
        == '他说"不行"就走了'


def test_quotes_at_the_very_end_of_a_value_still_close_the_string():
    """引号正好在值末尾时，它是收尾，不该被转义。"""
    broken = CARD.replace("{SUMMARY}", '"他只说了"算了""')
    fixed = json.loads(repair_unescaped_quotes(broken))
    assert fixed["cards"][0]["summary"] == '他只说了"算了"'


def test_multiple_broken_values_in_one_reply():
    """一条回复里多处都坏 —— 全都要修好。"""
    broken = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
              '"threads":["领导"],"summary":"他说"不行"","content":"'
              '领导原话是"重点不对"，他加了两个通宵，这句话让他很挫败，'
              '决定周末重做一版。","importance":0.7,"pulse":0.5}]}')
    cards, err = parse_capture_cards(broken, strict=False)
    assert err is None, err
    assert '不行' in cards[0]["summary"]
    assert '重点不对' in cards[0]["content"]


def test_keys_and_structure_are_never_touched():
    """只动字符串内部，键名和结构分隔符一概不碰。"""
    good = '{"a": "x", "b": ["y", "z"], "c": {"d": 1}}'
    assert repair_unescaped_quotes(good) == good
    assert json.loads(repair_unescaped_quotes(good)) == json.loads(good)


def test_unrepairable_shapes_still_report_a_parse_failure():
    """修不动就照旧报失败 —— 不猜、不吞。

    只处理"未转义引号"这一种：那一种有确定判据。缺逗号、单引号这些没有，
    乱修会把"解析失败"变成"解析成了别的意思"，后者更糟。
    """
    for bad in ('{"cards":[{"action":"add",}]}',       # 多余逗号
                "{'cards':[]}",                        # 单引号
                '{"cards":[{"action":"add"'):          # 截断
        cards, err = parse_capture_cards(bad, strict=False)
        assert not cards
        assert err, f"{bad!r} 竟然解析成功了"


def test_empty_and_degenerate_input():
    for value in ("", None, "{}"):
        out = repair_unescaped_quotes(value)
        assert out == (value or "")


# ── 歧义形态：宁可解析失败，也不静默改掉意思 ─────────────────────────────

MISSING_COMMA = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
                 '"threads": ["alpha" "beta"],"summary":"一张正常的卡",'
                 '"content":"正文足够长，能过内容闸的那种，讲清了这件事的前后经过。",'
                 '"importance":0.7,"pulse":0.5}]}')


def test_missing_comma_between_array_strings_is_not_repaired():
    """🔴 数组里两个字符串之间漏了逗号：``["alpha" "beta"]``。

    旧判据看到 alpha 后面那个引号的下一个非空白字符是 ``"``（不是 ``, } ] :``），
    就当成内容引号转义掉 —— 结果拼成一个 ``alpha" "beta`` 的字符串，**解析成功、
    静默落库**，两个线索变成了一个错的。这种形态和「内容里的引号」无法区分，
    所以不修：原样返回，让解析照旧失败、宿主去重问。
    """
    with pytest.raises(json.JSONDecodeError):
        json.loads(MISSING_COMMA)
    assert repair_unescaped_quotes(MISSING_COMMA) == MISSING_COMMA
    with pytest.raises(json.JSONDecodeError):
        json.loads(repair_unescaped_quotes(MISSING_COMMA))


@pytest.mark.parametrize("strict", [True, False])
def test_missing_comma_reaches_the_host_as_a_parse_failure(strict):
    """端到端：漏逗号的回复必须报解析失败，而不是产出一张线索被拼坏的卡。"""
    cards, err = parse_capture_cards(MISSING_COMMA, strict=strict)
    assert cards == []
    assert err and err.startswith("json_decode_error"), err


@pytest.mark.parametrize("between", ["", " ", "\n    ", "\t"])
def test_quote_then_another_string_is_ambiguous_with_any_spacing(between):
    """漏逗号时两个字符串之间有没有空白、是什么空白，都一样判歧义。"""
    broken = MISSING_COMMA.replace('"alpha" "beta"', f'"alpha"{between}"beta"')
    cards, err = parse_capture_cards(broken, strict=False)
    assert cards == [] and err, (between, cards)


def test_missing_comma_between_members_is_not_repaired():
    """对象成员之间漏逗号（``"s" "content":...``）同样不修。"""
    broken = MISSING_COMMA.replace('"summary":"一张正常的卡",', '"summary":"一张正常的卡" ')
    assert repair_unescaped_quotes(broken) == broken
    cards, err = parse_capture_cards(broken, strict=False)
    assert cards == [] and err


def test_one_ambiguous_quote_aborts_the_whole_repair():
    """同一份回复里既有可修的引号、又有歧义的漏逗号 —— 整份不修。

    出现歧义说明「在不在字符串里」这个状态已经不可信，只修一半同样可能改掉意思。
    """
    broken = MISSING_COMMA.replace('"一张正常的卡"', '"他说"不行"就走了"')
    assert repair_unescaped_quotes(broken) == broken
    cards, err = parse_capture_cards(broken, strict=False)
    assert cards == [] and err


def test_a_quoted_phrase_without_a_comma_after_it_still_repairs():
    """歧义规则要窄：C1 修完，引号后面跟普通文字的正常形态照修不误。"""
    broken = ('{"cards":[{"title":"t","content":"她说"好的"然后走了"}]}')
    assert json.loads(repair_unescaped_quotes(broken))["cards"][0]["content"] \
        == '她说"好的"然后走了'


def test_a_quoted_phrase_ending_the_value_is_not_treated_as_ambiguous():
    """``"他只说了"算了""`` —— 引号紧贴着收尾引号、后面接字段结束，不算歧义。"""
    broken = CARD.replace("{SUMMARY}", '"他只说了"算了""')
    fixed = json.loads(repair_unescaped_quotes(broken))
    assert fixed["cards"][0]["summary"] == '他只说了"算了"'


def test_adjacent_quotes_followed_by_more_text_are_ambiguous():
    """``["alpha""beta"]``：紧贴的第二个引号后面还跟着内容 → 是漏逗号的形状，不修。"""
    broken = MISSING_COMMA.replace('"alpha" "beta"', '"alpha""beta"')
    assert repair_unescaped_quotes(broken) == broken


def test_a_doubled_quote_after_a_key_is_not_repaired():
    """``"threads"": [...]`` —— 键名后多敲了一个引号。

    若按「紧贴引号 + 收尾符」放行，会得到键名 ``threads"``：解析成功、线索静默丢失。
    """
    broken = MISSING_COMMA.replace('"threads": ["alpha" "beta"]', '"threads"": ["alpha", "beta"]')
    assert repair_unescaped_quotes(broken) == broken
    cards, err = parse_capture_cards(broken, strict=False)
    assert cards == [] and err


# ── 修不了的形态：钉住它失败，而不是解析成别的意思 ─────────────────────────

def test_unescaped_quote_before_a_comma_is_out_of_scope_and_fails():
    """``她说"好的", 然后`` —— 引号后面紧跟逗号，和「字段在这里结束」长得一模一样。

    按设计修不了（见 repair_unescaped_quotes 的「支持范围」）。这里钉住的是：
    它必须**报解析失败**，不能被修成一张内容被截断的卡。
    """
    broken = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
              '"summary":"一张正常的卡",'
              '"content":"她说"好的", 然后走了，正文足够长，讲清了这件事的前后经过。",'
              '"importance":0.7,"pulse":0.5}]}')
    for strict in (True, False):
        cards, err = parse_capture_cards(broken, strict=strict)
        assert cards == []
        assert err and err.startswith("json_decode_error"), err


def test_unescaped_quote_before_a_colon_is_out_of_scope_and_fails():
    broken = ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
              '"summary":"一张正常的卡",'
              '"content":"标题是"备忘": 周末重做，正文足够长，讲清了这件事的前后经过。",'
              '"importance":0.7,"pulse":0.5}]}')
    cards, err = parse_capture_cards(broken, strict=False)
    assert cards == []
    assert err and err.startswith("json_decode_error"), err


# ── 漏冒号：引号后面跟着一个完整的 JSON 值 ─────────────────────────────────

def _card_with(fragment: str) -> str:
    return ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
            '"summary":"一张正常的卡",' + fragment + ','
            '"content":"正文足够长，能过内容闸的那种，讲清了这件事的前后经过。"}]}')


@pytest.mark.parametrize("fragment", [
    '"importance" 0.7,"pulse":0.5',       # 数字
    '"importance"0.7,"pulse":0.5',        # 紧贴
    '"pulse":0.5,"importance" -1',        # 负数，后面接 ,
    '"importance" 7e-1 ,"pulse":0.5',     # 指数 + 空白后再逗号
    '"is_sensitive" true,"pulse":0.5',
    '"is_sensitive" false,"pulse":0.5',
    '"role" null,"pulse":0.5',
    '"meta" {"a":1},"pulse":0.5',
    '"threads" ["领导"],"pulse":0.5',
])
def test_missing_colon_before_a_value_is_not_repaired(fragment):
    """🔴 键后面漏了冒号：``"importance" 0.7,"pulse":0.5``。

    旧判据看到引号后面是 ``0``（不是收尾符）就当内容引号转义，拼出一个
    ``importance" 0.7,"pulse`` 的键名：**解析成功**，importance 和 pulse 静默丢失。
    """
    broken = _card_with(fragment)
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    assert repair_unescaped_quotes(broken) == broken
    for strict in (True, False):
        cards, err = parse_capture_cards(broken, strict=strict)
        assert cards == []
        assert err and err.startswith("json_decode_error"), err


def test_the_reported_missing_colon_example_fails():
    broken = '{"cards":[{"summary":"s","importance" 0.7,"pulse":0.5}]}'
    assert repair_unescaped_quotes(broken) == broken
    with pytest.raises(json.JSONDecodeError):
        json.loads(repair_unescaped_quotes(broken))


def _content(value: str) -> str:
    return ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
            '"summary":"一张正常的卡","content":"' + value + '"}]}')


@pytest.mark.parametrize("value, expected", [
    ('He said "ok" and left', 'He said "ok" and left'),
    ('He said "yes" nothing more', 'He said "yes" nothing more'),
    ('a "lie" falsehood again', 'a "lie" falsehood again'),
    ('the "show" trueman style', 'the "show" trueman style'),
    ('a "void" nullable thing', 'a "void" nullable thing'),
    ('她说"好的"然后走了', '她说"好的"然后走了'),
    ('他报价"1000"块', '他报价"1000"块'),
    ('约在"3点"见', '约在"3点"见'),
    ('比分"2" 比 1', '比分"2" 比 1'),
    ('She said "no" null and left', 'She said "no" null and left'),
    # 551d13b 用「引号后面跟完整数字/字面量 + , } ]」的 token 白名单判漏冒号，
    # 这几条正文因此被误判放弃。结构化之后，值里的引号后面跟什么都不再是
    # 「漏冒号」的证据（漏冒号发生在键上），它们照修且内容逐字不变。
    ('He said "ok" 5, then left', 'He said "ok" 5, then left'),
    ('She said "no" null, fine', 'She said "no" null, fine'),
    ('He said "ok" true}', 'He said "ok" true}'),
    ('He wrote "x" [1], then left', 'He wrote "x" [1], then left'),
    ('He wrote "x" {y}', 'He wrote "x" {y}'),
    ('她说"[笑]"就走了', '她说"[笑]"就走了'),
    ('他说"好"、"行"、"没问题"', '他说"好"、"行"、"没问题"'),
    ('他说"好"，"行"；"走"/"停"', '他说"好"，"行"；"走"/"停"'),
    ('她说"好" "行"', '她说"好" "行"'),
])
def test_prose_quotes_that_are_not_ambiguous_still_repair(value, expected):
    fixed = json.loads(repair_unescaped_quotes(_content(value)))
    assert fixed["cards"][0]["content"] == expected


# ── 结构化判据：端到端，用一张除了这一处以外完全合法的卡 ─────────────────────
#
# 卡本身要能过内容闸 —— 否则 parse_capture_cards 会把它当脏卡滤掉、返回 ([], None)，
# 看起来「没落库」，实际上是把 bug 藏了起来。每条都先确认「改对之后」能落一张卡。

def _valid_card(*, threads: str = '["领导","加班"]',
                tail: str = '"is_sensitive":true,"pulse":0.5',
                content: str = "正文足够长，能过内容闸的那种，讲清了这件事的前后经过。") -> str:
    return ('{"cards":[{"action":"add","type":"event","bucket":"工作",'
            '"threads":' + threads + ',"summary":"一张正常的卡",'
            '"content":"' + content + '",' + tail + '}]}')


def _assert_decode_error(broken: str, corrected: str) -> None:
    cards, err = parse_capture_cards(corrected, strict=False)
    assert err is None and len(cards) == 1, ("对照组本身必须能落卡", corrected, err)
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    assert repair_unescaped_quotes(broken) == broken
    for strict in (True, False):
        cards, err = parse_capture_cards(broken, strict=strict)
        assert cards == [], (broken, cards)
        assert err and err.startswith("json_decode_error"), (broken, err)


@pytest.mark.parametrize("literal", [
    "True", "None", ".7", "+1", "01", "1.", "NaN", "Infinity", "-Infinity",
    "'x'", "0.5", "true", "null", '"yes"', "[1]", "{}",
])
@pytest.mark.parametrize("space", [" ", ""])
def test_missing_colon_after_a_key_is_never_repaired(literal, space):
    """🔴 Codex 复审 Critical 1：``"is_sensitive" True,"pulse":0.5``。

    551d13b 只认「完整的 JSON 数字/true/false/null」，``True``、``.7``、``NaN``、
    单引号字符串一概不认 → 引号被当内容转义 → 键名变成 ``is_sensitive" True,"pulse``，
    **解析成功**，pulse 静默变成默认值。现在判据落在结构上：键的收尾引号后面
    必须是 ``:``，与后面跟的是什么无关。
    """
    broken = _valid_card(tail=f'"is_sensitive"{space}{literal},"pulse":0.5')
    corrected = _valid_card(tail='"is_sensitive":true,"pulse":0.5')
    _assert_decode_error(broken, corrected)


def test_missing_colon_before_the_last_member_is_not_repaired():
    broken = _valid_card(tail='"pulse":0.5,"is_sensitive" True')
    _assert_decode_error(broken, _valid_card())


@pytest.mark.parametrize("threads", [
    '["领导"， "加班"]', '["领导"，"加班"]', '["领导"、"加班"]', '["领导"； "加班"]',
    '["领导"; "加班"]', '["领导"/ "加班"]', '["领导" "加班"]', '["领导""加班"]',
    '["领导” "加班"]', '["领导”, "加班"]', '["领导"， “加班"]', '["领导“ "加班"]',
    '[["领导"， "加班"]]', '[["领导"、"加班"], ["x"]]', '["领导", ["加班"； "周末"]]',
])
def test_wrong_separator_between_array_elements_is_not_repaired(threads):
    """🔴 Codex 复审 Critical 2：``["alpha"， "beta"]`` 被拼成一个元素 ``alpha"， "beta``。

    数组元素没有键，「吞掉了下一个元素」在结构上暴露不出来。所以数组元素里
    出现后面不是 ``,`` ``]`` 的引号，整份放弃 —— 不再去枚举是哪种分隔符。
    """
    _assert_decode_error(_valid_card(threads=threads), _valid_card())


def test_a_bare_quote_inside_an_array_element_is_not_repaired():
    """代价（按设计）：线索里真有裸引号时也不修，报解析失败让宿主重问。

    ``["他说"算了"那次"]`` 与 ``["领导” "加班"]`` 在引号层面长得一样 ——
    后者的第二个元素开头引号，同样是「元素里后面跟着文字的引号」。
    """
    _assert_decode_error(_valid_card(threads='["他说"算了"那次"]'), _valid_card())


@pytest.mark.parametrize("tail", [
    '"is_sensitive":true， "pulse":0.5',
    '"is_sensitive":"x"， "pulse":0.5',
    '"is_sensitive":"x"、"pulse":"y"',
])
def test_wrong_separator_between_members_is_not_repaired(tail):
    """值后面写错分隔符：把 ``x"`` 当内容会一路吞到 ``pulse`` 的收尾引号，
    它后面是 ``:`` —— 值后面出现冒号不合语法，整份放弃。"""
    _assert_decode_error(_valid_card(tail=tail), _valid_card())


def test_wrong_separator_between_objects_is_not_repaired():
    one = ('{"action":"add","type":"event","bucket":"工作","summary":"一张正常的卡",'
           '"content":"她说"好的"然后走了，正文足够长，讲清了这件事的前后经过。"}')
    broken = '{"cards":[' + one + '，' + one + ']}'
    cards, err = parse_capture_cards('{"cards":[' + one + ',' + one + ']}', strict=False)
    assert err is None and len(cards) == 2
    cards, err = parse_capture_cards(broken, strict=False)
    assert cards == [] and err.startswith("json_decode_error"), err


def test_a_stray_quote_inside_a_key_is_not_repaired():
    broken = _valid_card(tail='"is_"sensitive":true,"pulse":0.5')
    _assert_decode_error(broken, _valid_card())


@pytest.mark.parametrize("content", [
    '她说"好的"然后走了，讲清了这件事的前后经过。',
    '他报价"1000"块，讲清了这件事的前后经过。',
    '约在"3点"见，讲清了这件事的前后经过。',
    'He said "ok" and left, and that is the whole story.',
    '他说"好"、"行"，讲清了这件事的前后经过。',
])
def test_legit_quotes_in_values_repair_end_to_end(content):
    cards, err = parse_capture_cards(_valid_card(content=content), strict=True)
    assert err is None, err
    assert cards[0]["content"] == content
    assert cards[0]["threads"] == ["领导", "加班"]
    assert cards[0]["is_sensitive"] is True and cards[0]["pulse"] == 0.5


@pytest.mark.parametrize("broken", [
    '{"a":"她说"好"然后","b":[1,2}}',        # 括号不配对
    '{"a":"她说"好"然后",}',                  # 对象里多逗号后直接收尾
    '{"a":"她说"好"然后","b":[1,]}',          # 数组里多逗号后直接收尾
    '{"b":[1]，"a":"她说"好"然后"}',          # 值后面是 ，
    '{"b":1 2,"a":"她说"好"然后"}',           # 两个标量之间没逗号
    '{"a":"她说"好"然后",,"b":1}',            # 重复逗号
    '{"b":1 "a":"她说"好"然后"}',             # 该出现逗号的地方来了字符串
    '{"b":1 "她说"好"然后"}',
    '{"a"::"她说"好"然后"}',                  # 重复冒号
    '{"a":"她说"好"然后"',                    # 截断：对象没闭合
    '{"a":"她说"好"然后',                     # 截断：字符串没收尾
    '{"a":"她说"好"然后"} x',                 # 根闭合后还有东西
    '{"a":"她说"好"然后"}]',
])
def test_any_structural_error_returns_the_block_unchanged(broken):
    """修复只补值里的引号；只要结构上还有别的错，就**整份原样返回**，不做半截修改。

    （这些输入即使做了半截修改也解析不了 —— 这里钉住的是「放弃 = 原样返回」
    这条契约，调用方和日志看到的都是模型的原文。）
    """
    assert json.loads(repair_unescaped_quotes('{"a":"她说"好"然后","b":[1,2]}'))["a"] == '她说"好"然后'
    assert repair_unescaped_quotes(broken) == broken


# ── 静默改义 fuzz：合法文档 + 一处结构损坏，修复后要么报错、要么逐字等于原文 ──

def _naive_repair(block: str) -> str:
    """v0.20.1 的原始判据（引号后面是 , } ] : 或结尾才算收尾），只用来证明 fuzz 有牙齿。"""
    out, in_string, escaped, n = [], False, False, len(block)
    for i, ch in enumerate(block):
        if escaped:
            out.append(ch); escaped = False; continue
        if ch == "\\":
            out.append(ch); escaped = True; continue
        if ch == '"':
            if not in_string:
                in_string = True; out.append(ch); continue
            j = i + 1
            while j < n and block[j] in " \t\r\n":
                j += 1
            if (block[j] if j < n else "") in (",", "}", "]", ":", ""):
                in_string = False; out.append(ch)
            else:
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


_WORDS = ["好的", "算了", "没抓住重点", "ok", "yes", "1000", "3点", "alpha", "beta", "领导"]


def _phrase(rng, *, quotes: bool) -> str:
    words = []
    for _ in range(rng.randint(1, 4)):
        word = rng.choice(_WORDS)
        roll = rng.random()
        if quotes and roll < 0.35:
            word = f'"{word}"'
        elif roll < 0.45:
            word += rng.choice(["{", "}", "[", "]"])
        words.append(word)
    return rng.choice(["", " ", "，", "、"]).join(words)


def _fuzz_doc(rng, *, quotes_in_threads: bool) -> dict:
    cards = []
    for _ in range(rng.randint(1, 3)):
        card = {"action": "add", "type": "event", "bucket": "工作",
                "threads": [_phrase(rng, quotes=quotes_in_threads and rng.random() < 0.3)
                            for _ in range(rng.randint(0, 3))],
                "summary": _phrase(rng, quotes=True), "content": _phrase(rng, quotes=True) + "正文",
                "importance": rng.choice([0.7, 1, 0]), "pulse": 0.5,
                "is_sensitive": rng.choice([True, False]), "role": rng.choice([None, "turning_point"])}
        if rng.random() < 0.4:
            card["meta"] = {"k": _phrase(rng, quotes=True), "n": [1, 2], "nested": [["a", "b"], ["c"]]}
        cards.append(card)
    return {"cards": cards}


def _dump(rng, doc) -> str:
    if rng.random() < 0.5:
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(doc, ensure_ascii=rng.random() < 0.2, indent=rng.choice([None, 2]))


def _structure(text: str):
    punct, strings, in_s, esc, start = [], [], False, False, 0
    for i, ch in enumerate(text):
        if in_s:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_s = False
                strings.append((start, i))
            continue
        if ch == '"':
            in_s, start = True, i
        elif ch in ",:":
            punct.append(i)
    return punct, strings


_LITERALS = ["True", "None", ".7", "+1", "01", "1.", "NaN", "Infinity", "'x'", "False"]
_SEPARATORS = ["，", "、", "；", ";", "/", "， ", "、 ", "；\n"]


def _mutants(rng, text: str):
    punct, strings = _structure(text)
    commas = [p for p in punct if text[p] == ","]
    colons = [p for p in punct if text[p] == ":"]
    if commas:
        p = rng.choice(commas); yield "del_comma", text[:p] + text[p + 1:]
        p = rng.choice(commas); yield "dup_comma", text[:p] + "," + text[p:]
        p = rng.choice(commas); yield "cjk_separator", text[:p] + rng.choice(_SEPARATORS) + text[p + 1:]
    if colons:
        p = rng.choice(colons); yield "del_colon", text[:p] + text[p + 1:]
        p = rng.choice(colons); yield "dup_colon", text[:p] + ":" + text[p:]
        scalars = [p for p in colons if text[p + 1:].lstrip()[:1] not in ('"', "{", "[")]
        if scalars:
            p = rng.choice(scalars)
            k = p + 1
            while text[k] in " \n":
                k += 1
            while k < len(text) and text[k] not in ",}]\n ":
                k += 1
            yield "del_colon_literal", text[:p] + rng.choice([" ", ""]) + rng.choice(_LITERALS) + text[k:]
    ends = [e for (_s, e) in strings if text[e + 1:e + 2] == "," and text[e + 2:].lstrip()[:1] == '"']
    if ends:
        e = rng.choice(ends); yield "curly_no_comma", text[:e] + "” " + text[e + 2:]
        e = rng.choice(ends); yield "curly_keep_comma", text[:e] + rng.choice("”“’") + text[e + 1:]
        e = rng.choice(ends)
        yield "curly_open", text[:e + 1] + rng.choice(["，", "、"]) + " “" + text[e + 2:].lstrip()[1:]


def _capture_doc(raw: str, repair):
    from memgarden.text.card_text import extract_json_block, quote_repair_candidates
    block = extract_json_block(raw)
    try:
        return json.loads(block)
    except (ValueError, TypeError):
        for candidate in quote_repair_candidates(raw):
            try:
                return json.loads(repair(candidate))
            except (ValueError, TypeError):
                continue
    return None


def test_one_structural_typo_never_parses_into_a_different_document():
    """合法文档（值里带引号、花括号、嵌套数组）删/重复一个逗号或冒号、漏冒号后跟
    非标准字面量、逗号换成 ，、；; /、弯引号混用，再按一半概率把 \\" 还原成裸引号。

    解析层面只允许两种结果：报错，或者得到**逐字等于原文档**的结果。
    「解析成功但意思变了」必须是 0。
    """
    import random

    rng = random.Random(20260915)
    kinds: dict[str, int] = {}
    naive_changed = 0
    for _ in range(700):
        doc = _fuzz_doc(rng, quotes_in_threads=True)
        text = _dump(rng, doc)
        for kind, mutant in _mutants(rng, text):
            try:
                json.loads(mutant)
                continue            # 这处改动本身仍是合法 JSON（如 [1,2]→[12]），与修复无关
            except ValueError:
                pass
            if rng.random() < 0.5:
                mutant = mutant.replace('\\"', '"')
            raw = rng.choice(["{j}", "```json\n{j}\n```", "好的：\n{j}\n以上。"]).replace("{j}", mutant)
            kinds[kind] = kinds.get(kind, 0) + 1
            got = _capture_doc(raw, repair_unescaped_quotes)
            assert got is None or got == doc, (kind, mutant, got)
            naive = _capture_doc(raw, _naive_repair)
            naive_changed += naive is not None and naive != doc
    assert len(kinds) == 9 and min(kinds.values()) > 100, kinds
    # fuzz 有牙齿：同一批输入，旧的朴素判据会静默改义几百次
    assert naive_changed > 300, naive_changed


def test_quotes_in_values_repair_back_to_the_exact_document():
    """反方向：值里的裸引号必须逐字修回原文档。

    语料里值中的引号后面不会紧跟 ``, } ] :``（``_phrase`` 只把括号接在未加引号的词
    后面），所以这里没有「按设计修不了」的输入，要求 100% 修回。
    """
    import random

    rng = random.Random(7)
    checked = 0
    for _ in range(600):
        doc = _fuzz_doc(rng, quotes_in_threads=False)
        text = _dump(rng, doc)
        broken = text.replace('\\"', '"')
        if broken == text:
            continue
        try:
            json.loads(broken)
            continue
        except ValueError:
            pass
        checked += 1
        got = _capture_doc(broken, repair_unescaped_quotes)
        assert got == doc, broken
    assert checked > 200, checked
