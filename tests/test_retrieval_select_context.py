"""retrieval.select_context —— 自动想起：同一把尺子 + 软配额。

要守的三件事：

1. **和 rank 是同一把尺子**：合格的卡、分数、版本号都来自同一次打分；配额不绑定时
   选中的集合和顺序与 ``rank(limit=cap)`` 完全相同。
2. **配额是软的，且不绕过门槛**：不相关的转折点/最近卡不会为了凑数混进来；
   配额没用满的座位给其余合格的卡。
3. **trace 能接上宿主现有的观测**（``observability.injection_record``），且内容无关。
"""
from __future__ import annotations

import pathlib
import sys

_SRC_PATH = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

import pytest  # noqa: E402

from memgarden import observability  # noqa: E402
from memgarden.retrieval import rank, select_context  # noqa: E402
from memgarden.selection import Chain, RecentStage, RelevanceStage  # noqa: E402


def _filler(n: int = 20) -> list[dict]:
    return [{"id": f"f{i:02d}", "summary": f"周{i}吃了一碗面", "content": "味道一般，排队很久。",
             "occurred_at": "2026-01-01", "created_at": "2026-01-01"} for i in range(n)]


def _garden() -> list[dict]:
    return [
        # 转折点，和查询相关
        {"id": "tp-coffee", "summary": "决定戒掉咖啡", "content": "心悸之后决定不再喝咖啡。",
         "roles": ["turning_point"], "occurred_at": "2026-03-01", "created_at": "2026-03-01"},
        # 转折点，和查询无关 —— 不许为了凑配额混进来
        {"id": "tp-move", "summary": "搬到上海", "content": "离开杭州去上海工作。",
         "roles": ["turning_point"], "occurred_at": "2026-09-01", "created_at": "2026-09-01"},
        # 最近写的，和查询无关
        {"id": "new-unrelated", "summary": "买了一盆绿萝", "content": "放在窗台上。",
         "occurred_at": "2026-09-14", "created_at": "2026-09-14"},
        # 相关的普通卡
        {"id": "coffee-1", "summary": "咖啡豆换成浅烘", "content": "浅烘咖啡豆酸度高一点。",
         "occurred_at": "2026-02-01", "created_at": "2026-02-01"},
        {"id": "coffee-2", "summary": "公司楼下新开咖啡店", "content": "咖啡店的拿铁不错。",
         "occurred_at": "2026-02-10", "created_at": "2026-09-10"},
    ] + _filler()


def test_unrelated_turning_points_and_recent_cards_do_not_fill_quotas():
    picked, trace = select_context("咖啡", _garden())
    ids = [c["id"] for c in picked]
    assert "tp-move" not in ids and "new-unrelated" not in ids
    assert set(ids) == {"tp-coffee", "coffee-1", "coffee-2"}
    by_bucket = {s["id"]: s["bucket"] for s in trace["selected"]}
    assert by_bucket["tp-coffee"] == "turning"
    assert by_bucket["coffee-2"] == "recent"   # 合格卡里 created_at 最新
    assert by_bucket["coffee-1"] in {"recent", "query"}


def test_no_hit_selects_nothing():
    picked, trace = select_context("我有没有去过冰岛", _garden())
    assert picked == [] and trace["selected"] == []


def test_same_ruler_as_rank_when_quotas_do_not_bind():
    garden = _garden()
    for query in ("咖啡", "上海 工作", "浅烘咖啡豆", "面"):
        picked, trace = select_context(query, garden, cap=5)
        ranked = rank(query, garden, limit=5)
        assert [c["id"] for c in picked] == ranked.ids, query
        assert trace["version"] == ranked.version
        assert [c["selection"]["score"] for c in picked] == [h.score for h in ranked.hits]


def test_quotas_decide_seats_and_output_is_ordered_by_score():
    """cap 小于合格卡数时，配额决定谁有座位；座位上的顺序按分数。"""
    garden = _garden() + [
        {"id": f"coffee-strong-{i}", "summary": "咖啡 咖啡 咖啡", "content": "咖啡",
         "occurred_at": "2025-01-01", "created_at": "2025-01-01"} for i in range(4)]
    picked, trace = select_context("咖啡", garden, cap=3, quotas=(("turning_point", 1),))
    ids = [c["id"] for c in picked]
    assert "tp-coffee" in ids, "转折点配额没有生效"
    scores = [c["selection"]["score"] for c in picked]
    assert scores == sorted(scores, reverse=True), "输出不是按分数排的"
    # 没有配额时同样 cap 下，转折点卡分数不够、拿不到座位 —— 证明上面是配额起的作用
    plain, _ = select_context("咖啡", garden, cap=3, quotas=())
    assert "tp-coffee" not in [c["id"] for c in plain]
    assert trace["rejected_sample"] and all(r["reason"] in {"over_cap", "below_gate"}
                                           for r in trace["rejected_sample"])


def test_spare_quota_seats_go_to_other_eligible_cards():
    picked, _ = select_context("咖啡", _garden(), cap=8)
    assert len(picked) == 3, "空位没有补给其余合格的卡，或者塞进了不合格的卡"


def test_cap_zero_cards_without_id_and_candidate_order():
    garden = _garden()
    assert select_context("咖啡", garden, cap=0)[0] == []
    no_id = [{"summary": "咖啡"}] + garden
    assert [c["id"] for c in select_context("咖啡", no_id)[0]] == \
        [c["id"] for c in select_context("咖啡", garden)[0]]
    forward = select_context("咖啡", garden)[0]
    backward = select_context("咖啡", list(reversed(garden)))[0]
    assert [c["id"] for c in forward] == [c["id"] for c in backward]


def test_returned_cards_are_copies_with_selection_and_input_is_untouched():
    garden = _garden()
    picked, _ = select_context("咖啡", garden)
    assert all("selection" not in c for c in garden)
    assert {"score", "coverage", "bucket", "reason", "version"} <= set(picked[0]["selection"])


def test_trace_feeds_injection_record_and_is_content_free():
    garden = _garden()
    picked, trace = select_context("戒掉咖啡的事", garden)
    record = observability.injection_record(
        mode=f"relevant:{trace['version']}", query="戒掉咖啡的事", candidate_pool=len(garden),
        selection_trace=trace, injected_ids=[c["id"] for c in picked], cap=8)
    observability.assert_content_free(record)
    assert record["counts"]["index_count"] == len(garden)
    assert record["by_bucket"]
    assert set(record["rejected_reasons"]) <= {"below_gate", "over_cap", "below_min_score"}
    flat = repr({k: v for k, v in trace.items() if k != "version"})
    for fragment in ("咖啡", "戒掉", "心悸"):
        assert fragment not in flat


# --------------------------------------------------------------------------- #
# selection.RelevanceStage 的 bm25 默认
# --------------------------------------------------------------------------- #

def test_relevance_stage_defaults_to_the_same_ruler():
    garden = _garden()
    chained = Chain(stages=(RelevanceStage(limit=5),)).select(garden, "咖啡", limit=5)
    assert chained.card_ids == rank("咖啡", garden, limit=5).ids
    assert all(p.stage == "relevance" and p.reason == "bm25_match" for p in chained.picks)


def test_relevance_stage_no_hit_leaves_only_what_other_stages_add():
    """Chain 里有 RecentStage 时，最近卡照样进来 —— 那是 RecentStage 的设计；
    相关性这一段自己不许贡献任何卡。"""
    garden = _garden()
    result = Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=1))).select(
        garden, "我有没有去过冰岛", limit=8)
    assert [p.stage for p in result.picks] == ["recent"]


def test_relevance_stage_legacy_scorer_still_available_and_unknown_is_rejected():
    garden = _garden()
    legacy = Chain(stages=(RelevanceStage(limit=5, scorer="legacy", any_score=True),)).select(
        garden, "咖啡", limit=5)
    assert legacy.card_ids and all(p.reason != "bm25_match" for p in legacy.picks)
    with pytest.raises(ValueError):
        RelevanceStage(scorer="vector").pick(garden, "咖啡", budget=3)


@pytest.mark.parametrize("knob", [{"any_score": True}, {"strong_min": 0.5}, {"medium_min": 0.2},
                                  {"excluded_reasons": ("generic",)}])
def test_legacy_thresholds_with_bm25_are_rejected_not_silently_ignored(knob):
    with pytest.raises(ValueError, match="scorer='legacy'"):
        Chain(stages=(RelevanceStage(limit=3, **knob),))
    # 显式 legacy 照常可用；bm25 不带旧旋钮照常可用
    Chain(stages=(RelevanceStage(limit=3, scorer="legacy", **knob),)).select(_garden(), "咖啡", limit=3)
    Chain(stages=(RelevanceStage(limit=3),)).select(_garden(), "咖啡", limit=3)
