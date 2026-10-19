"""Synthetic Dream-only recovery boundaries; no provider or user data."""
import json

import pytest

from memgarden.prompts.dream import parse_dream_consolidations
from memgarden.text.card_text import extract_json_block

EMPTY = '{"consolidations":[],"questions_to_ask":[]}'
PREFIX = '说明：{仅为示意}。下面是结果：\n'
ROW = {'op': 'thicken', 'card_ids': ['a1'], 'rationale': '同一事件的补充',
       'result': {'summary': '周末出行', 'content': '周末计划坐火车去附近的城市散步。'}}


def test_balanced_prose_recovers_empty_dream_only():
    raw = PREFIX + EMPTY
    assert extract_json_block(raw) == '{仅为示意}'  # Shared extractor unchanged.
    assert parse_dream_consolidations(raw) == ([], [], None)


def test_recovered_rows_still_use_normal_semantics():
    raw = json.dumps({'consolidations': [ROW]}, ensure_ascii=False)
    assert parse_dream_consolidations(PREFIX + raw) == parse_dream_consolidations(raw)
    assert len(parse_dream_consolidations(PREFIX + raw)[0]) == 1


@pytest.mark.parametrize('strict', [True, False])
def test_recovered_content_rejection_is_not_hidden(strict):
    row = {**ROW, 'result': {'summary': 'placeholder', 'content': 'placeholder'}}
    raw = json.dumps({'consolidations': [row]})
    expected = parse_dream_consolidations(raw, strict=strict)
    assert expected[2].startswith('invalid_card_content')
    assert parse_dream_consolidations(PREFIX + raw, strict=strict) == expected


@pytest.mark.parametrize('raw', [
    '说明：左花括号是 {。下面是结果：\n' + EMPTY,
    '{"wrapper":\n' + EMPTY,
    PREFIX + '[' + EMPTY + ']',
    PREFIX + '{"wrapper":' + EMPTY + '}',
    '[' + PREFIX + EMPTY + ']',
    '{"wrapper": ' + PREFIX + EMPTY + '}',
    '<think>draft</think>' + PREFIX + EMPTY,
    PREFIX + '<think>draft\n' + EMPTY,
    PREFIX + '<think>draft</think>\n' + EMPTY,
    PREFIX + EMPTY + '\n' + EMPTY,
    PREFIX + EMPTY + '\n{}',
    PREFIX + EMPTY + '\n<think>draft',
    PREFIX + EMPTY[:-1],
    PREFIX + '"' + EMPTY + '"',
    PREFIX + '{"consolidations":[],"consolidations":[]}',
    PREFIX + '{"consolidations":[],"questions_to_ask":false}',
    PREFIX + '{"consolidations":[],"unexpected":1}',
    PREFIX + '{"consolidations":[{"op":"invalid"}]}',
    PREFIX + '{"consolidations":[null]}',
    PREFIX + json.dumps({'consolidations': [{**ROW, 'card_ids': []}]}),
    PREFIX + json.dumps({'consolidations': [{**ROW, 'rationale': ''}]}),
    PREFIX + json.dumps({'consolidations': [{**ROW, 'result': {'summary': 1, 'content': 'ok'}}]}),
    PREFIX + '{"consolidations":[],"questions_to_ask":[NaN]}',
    PREFIX + ' ' * 65536 + EMPTY,
])
def test_ambiguous_or_invalid_recovery_keeps_error(raw):
    assert parse_dream_consolidations(raw)[2] is not None


def test_schema_failure_of_first_valid_json_is_not_bypassed():
    assert parse_dream_consolidations('{"wrong":[]}\n' + EMPTY)[2] == 'missing_consolidations_list'


def test_existing_success_paths_are_not_redefined():
    # Explicitly record compatibility, not a claim that these shapes are safe.
    assert parse_dream_consolidations('<think>draft\n' + EMPTY)[2] is None
    assert parse_dream_consolidations(EMPTY + '\n{"questions_to_ask":["other"]}')[2] is None
    assert parse_dream_consolidations('{"consolidations":[{"op":"invalid"}]}')[2] is None
