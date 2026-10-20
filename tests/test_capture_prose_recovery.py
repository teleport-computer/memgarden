"""Bounded synthetic Capture recovery; no provider, host or user material."""
import json

import pytest

from memgarden.policies import get_policy
from memgarden.prompts.capture import parse_capture_cards
from memgarden.text.card_text import extract_json_block

PREFIX = '说明：{仅为示意}。下面是结果：\n'
ROW = {'action': 'add', 'type': 'fact', 'summary': '周末出行计划',
       'content': '计划周六早上坐火车去杭州，周日晚上回来。', 'bucket': '生活', 'threads': []}
DOC = json.dumps({'cards': [ROW]}, ensure_ascii=False)


def test_balanced_prose_recovers_card():
    assert parse_capture_cards(PREFIX + DOC) == parse_capture_cards(DOC)
    assert len(parse_capture_cards(PREFIX + DOC)[0]) == 1
    assert extract_json_block(PREFIX + DOC) == '{仅为示意}'


def test_balanced_prose_recovers_canonical_empty_reply():
    assert parse_capture_cards(PREFIX + '{"cards":[]}') == ([], None)


@pytest.mark.parametrize('policy', ['conversation_capture', 'history_import', 'curated_archive'])
def test_recovery_retains_policy_metadata_and_normalization(policy):
    row = {**ROW, 'occurred_at': '2026-10-01', 'role': 'user', 'is_sensitive': True}
    raw = json.dumps({'cards': [row]})
    actual = parse_capture_cards(PREFIX + raw, policy=policy)
    assert actual == parse_capture_cards(raw, policy=policy)
    assert actual[1] is None and len(actual[0]) == 1
    assert ('occurred_at' in actual[0][0]) == get_policy(policy).keep_dates


@pytest.mark.parametrize('strict', [True, False])
@pytest.mark.parametrize('change', [
    {'content': 'placeholder'}, {'role': 123}, {'is_sensitive': 'true'},
    {'occurred_at': 'not-a-date'},
])
def test_recovered_rows_still_run_content_and_metadata_gates(strict, change):
    raw = json.dumps({'cards': [{**ROW, **change}]})
    kwargs = dict(strict=strict, policy='curated_archive')
    expected = parse_capture_cards(raw, **kwargs)
    assert expected[1].startswith('invalid_card_content')
    assert parse_capture_cards(PREFIX + raw, **kwargs) == expected


@pytest.mark.parametrize('raw', [
    '说明 {\n' + DOC, '{"wrapper":\n' + DOC,
    PREFIX + '[' + DOC + ']', '[' + PREFIX + DOC + ']',
    PREFIX + '{"wrapper":' + DOC + '}',
    PREFIX + '<think>草稿\n' + DOC,
    '<think>草稿</think>' + PREFIX + DOC,
    PREFIX + '```json\n' + DOC + '\n```',
    PREFIX + DOC + '\n{"cards":[]}', PREFIX + DOC[:-1],
    PREFIX + DOC + '\n<think>草稿',
    PREFIX + '{"cards":[],"cards":[]}',
    PREFIX + '{"cards":[],"extra":1}',
    PREFIX + '{"cards":null}', PREFIX + '{"cards":[null]}',
    PREFIX + '{"cards":[{"action":"noop"}]}',
    PREFIX + json.dumps({'cards': [{**ROW, 'action': 'unknown'}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'action': []}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'summary': 1}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'content': ''}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'type': 'unknown'}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'threads': 'trip'}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'threads': [1]}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'bucket': {}}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'action': 'merge'}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'target_id': []}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'importance': float('inf')}]}),
    PREFIX + json.dumps({'cards': [{**ROW, 'pulse': float('nan')}]}),
    PREFIX + ' ' * 65536 + DOC,
])
def test_invalid_or_ambiguous_recovery_keeps_original_failure(raw):
    assert parse_capture_cards(raw)[1] is not None


def test_valid_first_object_schema_error_is_never_skipped():
    assert parse_capture_cards('{"wrong":[]}\n' + DOC)[1] == 'missing_cards_list'


def test_existing_success_and_noop_paths_remain_compatible():
    # These are recorded compatibility boundaries, not new safety promises.
    assert parse_capture_cards(DOC + '\n{"cards":[]}') == parse_capture_cards(DOC)
    assert parse_capture_cards('<think>草稿\n' + DOC) == parse_capture_cards(DOC)
    assert parse_capture_cards('{"cards":[{"action":"noop"}]}') == ([], None)


def test_quote_repair_still_precedes_new_recovery():
    raw = ('{"cards":[{"action":"add","type":"fact","summary":"周末计划",'
           '"content":"她说"好"然后看 } 这个符号，后面还有足够长的正文。"}]}')
    cards, error = parse_capture_cards(raw)
    assert error is None and len(cards) == 1
    assert cards[0]['content'].startswith('她说"好"然后看 }')


@pytest.mark.parametrize('change', [
    {'type': {}}, {'importance': True}, {'retrieval_cues': [1]},
])
def test_additional_shape_errors_cannot_disappear_into_defaults(change):
    raw = json.dumps({'cards': [{**ROW, **change}]})
    assert parse_capture_cards(PREFIX + raw)[1].startswith('json_decode_error')


def test_duplicate_nested_keys_and_overflow_are_rejected():
    for raw in [DOC.replace('"fact"', '"fact", "type":"event"'),
                DOC.replace('"action": "add"', '"importance":1e999,"action":"add"')]:
        assert parse_capture_cards(PREFIX + raw)[1].startswith('json_decode_error')


def test_invalid_row_mixed_with_valid_cannot_be_dropped_into_success():
    raw = json.dumps({'cards': [ROW, {'action': 'noop'}]})
    assert parse_capture_cards(PREFIX + raw)[1].startswith('json_decode_error')


def test_recovery_obeys_capture_card_limit():
    raw = json.dumps({'cards': [ROW, ROW, ROW]})
    expected = parse_capture_cards(raw)
    assert expected[1] is not None
    assert parse_capture_cards(PREFIX + raw) == expected


def test_host_supplied_protocol_guard_still_rejects_recovered_content():
    from memgarden.text.leak_signals import LeakSignals
    signals = LeakSignals(strong=(lambda text: 'synthetic_protocol' if 'PROTO_SENTINEL' in text else None,))
    raw = json.dumps({'cards': [{**ROW, 'content': 'PROTO_SENTINEL'}]})
    expected = parse_capture_cards(raw, signals=signals)
    assert expected[1] == 'invalid_card_content:content_protocol_leak'
    assert parse_capture_cards(PREFIX + raw, signals=signals) == expected


def test_recovered_unknown_target_still_uses_component_semantic_gate():
    from memgarden import CaptureRequest, GardenComponent
    request = CaptureRequest(window='纯合成出行计划', locale='zh-Hans', existing_cards=[])
    session = GardenComponent(model=None, max_capture_retries=0).capture_session(request)
    assert session.next_prompt() is not None
    raw = json.dumps({'cards': [{**ROW, 'action': 'merge', 'target_id': 'missing'}]})
    session.feed(PREFIX + raw)
    assert session.next_prompt() is None
    result = session.result()
    assert not result.cards
