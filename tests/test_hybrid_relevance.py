"""T523 batch 2a: dense + lexical fusion with soft quotas inside the shortlist."""
import math

import pytest

from memgarden.scoring import hybrid, relevance
from memgarden.scoring.hybrid import (
    VectorContractError,
    cosine,
    rrf_fuse,
    select_hybrid_context_memories_with_trace as select,
)


def _card(i, summary, **extra):
    return {"id": f"c{i:02}", "summary": summary,
            "created_at": "2026-09-01", "occurred_at": "2026-09-01", **extra}


# Unit vectors on a 3-d "meaning" axis: [plant-pot, raincoat, camping-lamp].
POT = [1.0, 0.0, 0.0]
COAT = [0.0, 1.0, 0.0]
LAMP = [0.0, 0.0, 1.0]
NEAR_POT = [0.95, 0.31, 0.0]


def garden():
    return [
        _card(0, "花盆的釉色是雾蓝杏砂"),           # semantic hit only ("容器" ≠ "花盆")
        _card(1, "雨衣放在玄关左柜第二层"),           # unrelated to the pot query
        _card(2, "露营灯保修档案编号 NP-4286"),       # lexical exact-code card
        _card(3, "去了一趟摩天轮", roles=["turning_point"], created_at="2027-01-01"),
    ]


def test_semantic_only_hit_is_selected_and_lexical_only_code_is_kept():
    pool = garden()
    vecs = {"c00": POT, "c01": COAT, "c02": LAMP}
    chosen, trace = select(pool, "阳台上那个容器外面是什么颜色", query_vector=NEAR_POT,
                           card_vectors=vecs, min_cosine=0.5)
    ids = [c["id"] for c in chosen]
    assert ids[0] == "c00"                         # only the pot passes the vector gate
    assert "c01" not in ids and "c03" not in ids   # unrelated / no-vector turning point excluded
    assert chosen[0]["selection"]["lane"] == "vector"
    assert chosen[0]["selection"]["vector_rank"] == 1 and chosen[0]["selection"]["lexical_rank"] is None
    # exact code query: the lexical lane carries it when no card passes the vector gate
    diagonal = [0.577, 0.577, 0.577]   # cos ≈ 0.577 to every axis card
    chosen2, trace2 = select(pool, "NP-4286 保修", query_vector=diagonal, card_vectors=vecs, min_cosine=0.9)
    assert [c["id"] for c in chosen2] == ["c02"]
    assert chosen2[0]["selection"]["lane"] == "lexical" and trace2["counts"]["vector_eligible"] == 0


def test_unrelated_recent_and_turning_cards_cannot_fill_a_quota():
    pool = garden()
    chosen, trace = select(pool, "容器颜色", query_vector=NEAR_POT,
                           card_vectors={"c00": POT, "c01": COAT, "c03": COAT},
                           min_cosine=0.5, cap=8)
    assert [c["id"] for c in chosen] == ["c00"]
    assert trace["counts"]["fused"] == 1 and trace["counts"]["selected"] == 1


def test_no_query_vector_degrades_to_lexical_lane_honestly():
    pool = garden()
    chosen, trace = select(pool, "露营灯保修", query_vector=None, card_vectors=None, min_cosine=0.5)
    assert trace["vector_lane"] == "absent"
    assert [c["id"] for c in chosen] == ["c02"]
    assert chosen[0]["selection"]["vector_rank"] is None
    assert select(pool, "", query_vector=None, card_vectors=None, min_cosine=0.5)[0] == []


def test_fusion_order_beats_time_inside_shortlist_and_is_input_order_independent():
    pool = [
        _card(0, "花盆的釉色是雾蓝杏砂", created_at="2020-01-01"),
        _card(1, "另一个花盆", created_at="2027-01-01"),
        _card(2, "第三个花盆", created_at="2026-01-01"),
    ]
    vecs = {"c00": POT, "c01": [0.6, 0.8, 0.0], "c02": [0.7, 0.71, 0.0]}
    a, ta = select(pool, "花盆 釉色", query_vector=POT, card_vectors=vecs, min_cosine=0.5, cap=3)
    b, tb = select(pool[::-1], "花盆 釉色", query_vector=POT, card_vectors=vecs, min_cosine=0.5, cap=3)
    assert [c["id"] for c in a] == [c["id"] for c in b]
    # buckets decide seats, fusion decides order: the newest card (c01) takes a
    # "recent" seat but the top fusion pick still comes first
    assert a[0]["id"] == "c00" and a[0]["selection"]["fusion_rank"] == 1
    assert [t["fusion_rank"] for t in ta["selected"]] == [1, 2, 3]
    assert {t["bucket"] for t in ta["selected"]} >= {"recent", "query"}


def test_rrf_math_and_weights():
    fused = rrf_fuse({"vector": {"a": 1, "b": 2}, "lexical": {"b": 1}},
                     {"vector": 2.0, "lexical": 1.0}, k=20)
    assert math.isclose(fused["a"], 2 / 21)
    assert math.isclose(fused["b"], 2 / 22 + 1 / 21)
    assert fused["b"] > fused["a"]
    with pytest.raises(ValueError):
        rrf_fuse({"v": {"a": 0}}, {"v": 1.0})


def test_vector_contract_is_enforced():
    pool = garden()
    with pytest.raises(VectorContractError):
        select(pool, "x", query_vector=[1.0, 0.0], card_vectors={"c00": POT}, min_cosine=0.5)
    with pytest.raises(VectorContractError):
        select(pool, "x", query_vector=[float("nan"), 0, 0], card_vectors={"c00": POT}, min_cosine=0.5)
    with pytest.raises(VectorContractError):
        select(pool, "x", query_vector=POT, card_vectors={"c00": [0.0, 0.0, 0.0]}, min_cosine=0.5)
    with pytest.raises(VectorContractError):
        select(pool, "x", query_vector=POT, card_vectors={"c00": POT}, min_cosine=0.5,
               vector_model="e5-small@1", card_vector_models={"c00": "bge-m3@1"})
    with pytest.raises(ValueError):
        select(pool, "x", query_vector=POT, card_vectors={"c00": POT}, min_cosine=float("nan"))
    assert math.isclose(cosine([1, 0], [1, 0]), 1.0)


def test_thresholds_cap_and_no_vectors_echoed_in_trace():
    pool = garden()
    vecs = {"c00": POT, "c01": COAT, "c02": LAMP}
    assert select(pool, "花盆", query_vector=POT, card_vectors=vecs, min_cosine=0.5, cap=0)[0] == []
    chosen, trace = select(pool, "花盆", query_vector=POT, card_vectors={"c00": POT}, min_cosine=1.0)
    assert [c["id"] for c in chosen] == ["c00"]         # cosine == 1.0 passes an inclusive gate
    text = repr(trace)
    assert "query_vector" not in text and "[1.0, 0.0, 0.0]" not in text
    assert trace["mode"] == "hybrid" and trace["counts"]["with_vector"] == 1


def test_legacy_modes_are_untouched():
    pool = garden()
    assert relevance.select_context_memories(pool, "露营灯保修", mode="relevant")[0]["id"] == "c02"
    # default mode: turning point + two recent cards, exactly as before this module existed
    assert len(relevance.select_context_memories(pool, "")) == 3
    assert not hasattr(relevance, "select_hybrid_context_memories_with_trace")
