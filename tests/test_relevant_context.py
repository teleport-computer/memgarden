"""T510/T512: relevance-gated soft quotas, using the real lexical scorer."""
import pytest

from memgarden.scoring import relevance, selector


def cards():
    return [{"id": f"c{i:02}", "summary": f"露营灯保修档案编号 NP-{i:04}",
             "created_at": "2026-09-01", "occurred_at": "2026-09-01"} for i in range(12)]


def test_unrelated_recent_and_turning_cards_cannot_fill_a_quota():
    pool = [{"id": "unrelated", "summary": "摩天轮旅游", "roles": ["turning_point"],
             "created_at": "2027-01-01"}, *cards()]
    selected, trace = relevance.select_relevant_context_memories_with_trace(pool, "露营灯保修")
    assert len(selected) == 8 and "unrelated" not in {c["id"] for c in selected}
    assert all(c["selection"]["score"] >= .35 for c in selected)
    assert len([c for c in trace["selected"] if c["bucket"] == "query"]) == 6


@pytest.mark.parametrize("query", ["", "今天好累", "API project"])
def test_no_relevant_evidence_means_no_ambient_injection(query):
    assert relevance.select_relevant_context_memories_with_trace(cards(), query)[0] == []


def test_roles_are_semantic_not_title_prefixes_and_order_is_stable():
    pool = cards()
    pool[0]["roles"] = ["turning_point"]
    pool[1]["title"] = "转折｜露营灯保修"
    result, trace = relevance.select_relevant_context_memories_with_trace(pool, "露营灯保修")
    reverse, _ = relevance.select_relevant_context_memories_with_trace(pool[::-1], "露营灯保修")
    assert [c["id"] for c in result] == [c["id"] for c in reverse]
    assert trace["selected"][0]["id"] == "c00"
    assert trace["selected"][0]["bucket"] == "turning"
    assert all(c["bucket"] != "turning" for c in trace["selected"] if c["id"] == "c01")


def test_strict_threshold_cap_and_compatibility_are_explicit():
    assert relevance.select_relevant_context_memories_with_trace(cards(), "露营灯保修", cap=0)[0] == []
    assert relevance.select_relevant_context_memories_with_trace(cards(), "露营灯保修", min_relevance=1)[0] == []
    with pytest.raises(ValueError):
        relevance.select_relevant_context_memories_with_trace(cards(), "露营灯", min_relevance=float("nan"))
    # Historical default/Chain callers do not silently change policy on upgrade.
    assert len(relevance.select_context_memories(cards(), "")) == 2
    assert relevance.select_context_memories(cards(), "", mode="relevant") == []


def test_unwritten_open_thread_flag_no_longer_influences_index_score():
    item = {"id": "x", "summary": "露营灯保修", "salience": "medium"}
    assert selector._metadata_score(item) == selector._metadata_score({**item, "is_open_thread": True})
