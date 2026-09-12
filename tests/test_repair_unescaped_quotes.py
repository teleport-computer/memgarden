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
