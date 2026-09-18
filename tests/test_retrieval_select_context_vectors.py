"""retrieval.select_context 的向量通道（T523 步骤 2）。

要守的四件事：

1. **不给向量就逐字节不变**：结果和 trace 与纯 BM25 调用完全相同，trace 不多一个键 ——
   宿主升版本不会悄悄换尺子。
2. **换说法的卡能只靠向量进来**：一个词都不重合、被 BM25 闸挡在门外的卡，余弦过门就成候选；
   它带着自己的 BM25 分（0 或被闸时的分）和命中词，不重打分。
3. **配额和空轮的规矩不变**：不相关的转折点/最近卡没有向量、没有词法命中，仍然进不来；
   查询为空时向量不能替它偷偷注入记忆。
4. **契约错误大声失败**：缺 ``min_cosine``、模型标签不配、NaN 向量、负权重都抛，不静默降级。
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

_SRC_PATH = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

import pytest  # noqa: E402

from memgarden.retrieval import select_context  # noqa: E402
from memgarden.scoring.hybrid import VectorContractError  # noqa: E402


def _filler(n: int = 20) -> list[dict]:
    return [{"id": f"f{i:02d}", "summary": f"周{i}吃了一碗面", "content": "味道一般，排队很久。",
             "occurred_at": "2026-01-01", "created_at": "2026-01-01"} for i in range(n)]


def _garden() -> list[dict]:
    return [
        {"id": "tp-coffee", "summary": "决定戒掉咖啡", "content": "心悸之后决定不再喝咖啡。",
         "roles": ["turning_point"], "occurred_at": "2026-03-01", "created_at": "2026-03-01"},
        # 转折点，和查询无关，也没有向量 —— 不许为了凑配额混进来
        {"id": "tp-move", "summary": "搬到上海", "content": "离开杭州去上海工作。",
         "roles": ["turning_point"], "occurred_at": "2026-09-01", "created_at": "2026-09-01"},
        {"id": "new-unrelated", "summary": "买了一盆绿萝", "content": "放在窗台上。",
         "occurred_at": "2026-09-14", "created_at": "2026-09-14"},
        {"id": "coffee-1", "summary": "咖啡豆换成浅烘", "content": "浅烘咖啡豆酸度高一点。",
         "occurred_at": "2026-02-01", "created_at": "2026-02-01"},
        # 换说法：一个「咖啡」都没写，只有向量能把它找回来
        {"id": "paraphrase", "summary": "早上那杯提神的东西戒了", "content": "换成了茶。",
         "occurred_at": "2026-02-20", "created_at": "2026-02-20"},
    ] + _filler()


QUERY = "咖啡"
# 二维就够表达「像 / 不像」：query 指向 (1, 0)。
QV = [1.0, 0.0]
VECTORS = {
    "tp-coffee": [0.9, 0.1],
    "coffee-1": [0.95, 0.05],
    "paraphrase": [0.85, 0.15],
    "new-unrelated": [0.0, 1.0],   # 有向量但不像 —— 要落在 below_cosine
    "f00": [0.1, 0.9],
}


def _hybrid(**overrides):
    kwargs = dict(query_vector=QV, card_vectors=VECTORS, min_cosine=0.5)
    kwargs.update(overrides)
    return select_context(QUERY, _garden(), cap=4, **kwargs)


def test_without_query_vector_is_byte_identical_to_bm25():
    plain = select_context(QUERY, _garden(), cap=4)
    with_cards_only = select_context(QUERY, _garden(), cap=4, card_vectors=VECTORS)
    assert with_cards_only == plain
    assert "vector_lane" not in plain[1] and "hybrid" not in plain[1]
    assert plain[1]["mode"] == "bm25"
    assert all(c["selection"]["reason"] == "bm25_match" for c in plain[0])
    assert all(set(c["selection"]) == {"score", "coverage", "bucket", "reason",
                                       "matched_units", "version"} for c in plain[0])


def test_paraphrased_card_enters_on_the_vector_lane_alone():
    plain_ids = [c["id"] for c in select_context(QUERY, _garden(), cap=4)[0]]
    assert "paraphrase" not in plain_ids  # 纯词法找不到它 —— 这正是要修的鸿沟

    selected, trace = _hybrid()
    ids = [c["id"] for c in selected]
    assert "paraphrase" in ids
    pick = next(c for c in selected if c["id"] == "paraphrase")["selection"]
    assert pick["reason"] == "hybrid_rrf"
    assert pick["lanes"] == {"lexical": None, "vector": 3}
    assert pick["bm25"] == 0.0 and pick["matched_units"] == []
    assert pick["cosine"] == pytest.approx(0.9847, abs=1e-3)
    assert trace["mode"] == "hybrid" and trace["vector_lane"] == "active"
    assert trace["hybrid"]["vector_only"] == 1
    assert trace["hybrid"]["with_vector"] == 5 and trace["hybrid"]["vector_eligible"] == 3


def test_cards_on_both_lanes_outrank_single_lane_cards_and_output_is_in_fused_order():
    selected, _ = _hybrid()
    scores = [c["selection"]["score"] for c in selected]
    assert scores == sorted(scores, reverse=True)
    both = [c["id"] for c in selected
            if c["selection"]["lanes"]["lexical"] and c["selection"]["lanes"]["vector"]]
    single = [c["id"] for c in selected
              if None in c["selection"]["lanes"].values()]
    assert both and single
    assert selected.index(next(c for c in selected if c["id"] == both[-1])) < \
        selected.index(next(c for c in selected if c["id"] == single[0]))


def test_unrelated_cards_stay_out_and_below_cosine_is_reported():
    selected, trace = _hybrid()
    ids = {c["id"] for c in selected}
    assert not ids & {"tp-move", "new-unrelated", "f00"}
    reasons = {r["id"]: r["reason"] for r in trace["rejected_sample"]}
    assert reasons.get("new-unrelated") == "below_cosine"
    assert reasons.get("f00") == "below_cosine"
    assert all(r["selected"] is False for r in trace["rejected_sample"])


def test_lexically_gated_card_keeps_its_bm25_evidence_when_the_vector_lets_it_in():
    # 只有一个查询词命中、覆盖率低的卡会被 BM25 闸挡下；给它一个像的向量。
    garden = _garden() + [{"id": "gated", "summary": "咖啡 以及很多别的事情", "content":
                          "今天聊了工作、房租、健身、旅行和咖啡。",
                          "occurred_at": "2026-01-15", "created_at": "2026-01-15"}]
    vectors = {**VECTORS, "gated": [0.99, 0.01]}
    selected, _ = select_context("咖啡 旅行 房租 健身 工作 天气 电影", garden, cap=8,
                                 query_vector=QV, card_vectors=vectors, min_cosine=0.5)
    pick = next(c["selection"] for c in selected if c["id"] == "gated")
    assert pick["bm25"] > 0.0 and pick["matched_units"]
    assert pick["lanes"]["vector"] == 1


def test_empty_query_returns_nothing_even_with_vectors():
    selected, trace = select_context("", _garden(), query_vector=QV, card_vectors=VECTORS,
                                     min_cosine=0.5)
    assert selected == []
    assert trace["vector_lane"] == "skipped" and trace["selected"] == []


def test_zero_vector_weight_reduces_to_the_lexical_set():
    plain = [c["id"] for c in select_context(QUERY, _garden(), cap=4)[0]]
    hybrid = [c["id"] for c in _hybrid(vector_weight=0.0)[0]]
    assert hybrid == plain


def test_model_labels_must_match_when_given():
    ok, _ = _hybrid(vector_model="e5-small+p1",
                    card_vector_models={cid: "e5-small+p1" for cid in VECTORS})
    assert ok
    with pytest.raises(VectorContractError):
        _hybrid(vector_model="e5-small+p1",
                card_vector_models={**{cid: "e5-small+p1" for cid in VECTORS},
                                    "coffee-1": "e5-small+p2"})
    with pytest.raises(VectorContractError):
        _hybrid(vector_model="e5-small+p1")  # 只给一半


@pytest.mark.parametrize("bad", [
    dict(min_cosine=None), dict(min_cosine=float("nan")), dict(min_cosine=1.5),
    dict(vector_weight=-1.0), dict(lexical_weight=float("inf")), dict(rrf_k=-1),
])
def test_bad_knobs_raise_instead_of_degrading(bad):
    with pytest.raises(ValueError):
        _hybrid(**bad)


def test_nan_and_dimension_mismatch_are_contract_errors():
    with pytest.raises(VectorContractError):
        _hybrid(card_vectors={**VECTORS, "coffee-1": [float("nan"), 0.0]})
    with pytest.raises(VectorContractError):
        _hybrid(card_vectors={**VECTORS, "coffee-1": [1.0, 0.0, 0.0]})


def test_trace_is_json_encodable_and_content_free():
    selected, trace = _hybrid()
    encoded = json.dumps(trace, allow_nan=False, ensure_ascii=False)
    for card in _garden():
        assert card["summary"] not in encoded
    assert all(math.isfinite(c["selection"]["score"]) for c in selected)
