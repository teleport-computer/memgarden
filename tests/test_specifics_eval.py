"""Test the eval's failure detection, not the model's memory quality."""
import io
import json

from evals.specifics import CORPUS, ask, build_prompt, evaluate, judge


def test_openrouter_probe_bounds_output_and_records_usage(monkeypatch):
    def respond(request, *, timeout):
        body = json.loads(request.data)
        assert timeout == 180 and body["max_tokens"] == 4096
        assert body["provider"]["only"] == ["deepseek"]
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["max_price"] == {"prompt": 2, "completion": 5}
        assert body["reasoning"] == {"enabled": False}
        return io.BytesIO(b'{"usage":{"cost":0.001},"choices":[]}')

    monkeypatch.setattr("urllib.request.urlopen", respond)
    response = ask("https://openrouter.ai/api/v1/chat/completions", "synthetic", "model", "prompt",
                   max_tokens=4096, route="deepseek")
    assert response["usage"]["cost"] == 0.001


def cases():
    return {row["id"]: row for row in map(json.loads, CORPUS.read_text().splitlines())}


def test_parse_failure_is_not_a_successful_empty_capture():
    case = cases()["capture-smalltalk"]
    assert all(evaluate(case, '{"cards": []}')["checks"].values())
    assert not all(evaluate(case, "not json")["checks"].values())
    assert not all(judge(case, [], "malformed")["checks"].values())


def test_nonempty_smalltalk_or_unaccepted_suggestion_fails():
    for name in ("capture-smalltalk", "capture-unaccepted-advice"):
        result = judge(cases()[name], [{"summary": "Bought a dress", "content": "For Nora"}], None)
        assert not result["checks"]["count"]


def test_year_invented_only_in_retrieval_cues_fails():
    case = cases()["capture-book-zh"]
    card = {"summary": "买书", "content": case["window"],
            "retrieval_cues": ["2025年9月12日"]}
    result = judge(case, [card], None)
    assert result["invented_years"] == ["2025"]
    assert not result["checks"]["grounded_years"]
    card["retrieval_cues"] = ["9月12日"]
    assert judge(case, [card], None)["checks"]["grounded_years"]


def test_dream_probe_catches_lost_value_wrong_target_and_noop():
    case = cases()["dream-merge-en"]
    content = " ".join(c["content"] for c in case["cards"])
    proposal = {"op": "merge", "card_ids": case["target_ids"],
                "rationale": "The same gift purchase and its delivery.",
                "result": {"summary": "Nora's birthday gift", "content": content,
                           "bucket": "Family", "threads": ["Nora"]}}

    def reply():
        return json.dumps({"consolidations": [proposal], "questions_to_ask": []})

    assert all(evaluate(case, reply())["checks"].values())
    proposal["result"]["content"] = content.replace("48", "")
    assert not evaluate(case, reply())["checks"]["concrete_values"]
    proposal["result"]["content"] = content
    proposal["card_ids"] = ["gift_a"]
    assert not evaluate(case, reply())["checks"]["targets"]
    assert not all(evaluate(case, '{"consolidations": [], "questions_to_ask": []}')["checks"].values())


def test_corpus_and_prompts_cover_both_languages_and_lanes():
    rows = cases()
    assert len(rows) == 7
    assert {(c["locale"], c["lane"]) for c in rows.values()} == {
        ("en", "capture"), ("zh-Hans", "capture"), ("en", "dream"), ("zh-Hans", "dream")}
    for case in rows.values():
        assert case["why"] and len(build_prompt(case)) > 100
