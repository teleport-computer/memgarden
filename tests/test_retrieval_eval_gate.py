"""召回排序的评测闸：默认 ``retrieval.rank`` 在 ``evals/retrieval`` 语料上不许退步。

阈值 = MG-3 校准时实测值减一点余量（2026-09-15，默认分词器）：

    实测   recall@5 0.932 · MRR@8 0.891 · 无命中返回空 4/5 · 有答案却返回空 1 · 陷阱排在答案前 0
    闸     recall@5 ≥ 0.91 · MRR@8 ≥ 0.87 · 无命中 ≥ 4/5 · 有答案却返回空 ≤ 1 · 陷阱在前 ≤ 1

红了先判断是**实现退步**还是**语料/产品定义变了**（改了 queries.jsonl 的标注）；
后者要在同一个提交里更新这里的实测值和原因，不许为了变绿只改阈值。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "evals" / "retrieval"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402


def _summary(ranker):
    cards = harness.load_garden()
    queries = harness.load_queries(cards)
    return harness.evaluate(ranker, cards, queries, repeats=1)["summary"]


def test_default_rank_holds_the_calibrated_quality_line():
    s = _summary(harness.mg_bm25)
    no_hit, total = (int(x) for x in s["no_hit_correct"].split("/"))
    assert s["recall@5"] >= 0.91, s["recall@5"]
    assert s["mrr@8"] >= 0.87, s["mrr@8"]
    assert total == 5 and no_hit >= 4, s["no_hit_correct"]
    assert s["false_empty"] <= 1, s["false_empty"]
    assert s["trap_above_answer"] <= 1, s["trap_above_answer"]


def test_the_gate_is_what_buys_the_empty_answers():
    """同一个排序器关掉停用词和门槛，无命中查询几乎全部返回东西 —— 证明上一条的
    「无命中 4/5」来自门槛，而不是语料碰巧没有重叠。"""
    from memgarden import retrieval

    def open_gate(query, cards, k):
        return retrieval.rank(query, cards, limit=k, stopwords=frozenset(), min_coverage=0.0).ids

    s = _summary(open_gate)
    assert int(s["no_hit_correct"].split("/")[0]) <= 2, s["no_hit_correct"]
