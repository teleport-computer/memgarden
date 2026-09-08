import json

import pytest

from memgarden.prompts import capture, dream, recall_fields


def card():
    return {"summary": "The parcel pickup code is AB-123.",
            "content": "The parcel pickup code is AB-123. It was left at the west gate on September 1.",
            "bucket": "life", "threads": ["parcel"], "importance": 0.7, "pulse": 0.4,
            "retrieval_cues": ["parcel", "pickup code", "2026-09-01"], "importance_level": 2}


def test_capture_cues_survive_parser_and_keep_legacy_importance():
    row = {**card(), "action": "add", "type": "fact"}
    rows, error = capture.parse_capture_cards(json.dumps({"cards": [row]}))
    assert error is None
    assert rows[0]["retrieval_cues"] == row["retrieval_cues"]
    assert rows[0]["importance"] == 0.4
    row.pop("retrieval_cues")
    row.pop("importance_level")
    rows, error = capture.parse_capture_cards(json.dumps({"cards": [row]}))
    assert error is None and rows[0]["importance"] == 0.7
    assert "retrieval_cues" not in rows[0]


def test_dream_cues_survive_parser_without_rewriting_body():
    result = card()
    rows, questions, error = dream.parse_dream_consolidations(json.dumps({"consolidations": [
        {"op": "thicken", "card_ids": ["old"], "rationale": "Preserve facts and repair missing retrieval hints.", "result": result}], "questions_to_ask": []}))
    assert error is None and not questions
    assert rows[0]["result"]["retrieval_cues"] == result["retrieval_cues"]
    assert rows[0]["result"]["content"] == result["content"]
    assert rows[0]["result"]["importance"] == 0.4


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
def test_five_levels_map_to_existing_normalized_storage(level):
    assert recall_fields.importance({"importance_level": level}, lambda v: 0.7) == level / 5


def test_cues_are_optional_bounded_strings_not_objects():
    assert recall_fields.retrieval_cues([{}, True, 12, None, " a  b ", "a b", "x" * 200, *map(str, range(8))]) == ["a b", "x" * 120, "0", "1", "2"]
    assert recall_fields.retrieval_cues("not a list") == []
    for invalid in [True, 0, 6, "3", None]:
        assert recall_fields.importance({"importance_level": invalid, "importance": 0.7}, float) == 0.7


def test_prompts_require_grounded_cues_event_time_and_five_levels():
    for prompt in [capture._CAPTURE_PROMPT_TEMPLATE, dream._DREAM_PROMPT_TEMPLATE]:
        assert "retrieval_cues" in prompt and "importance_level" in prompt
        assert "event time" in prompt and "Do not invent" in prompt
        assert "0.2/0.4/0.6/0.8/1.0" in prompt
