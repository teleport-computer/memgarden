"""召回排序的评测闸：默认 ``retrieval.rank`` 在 ``evals/retrieval`` 语料上不许退步。

单句查询集的阈值 = MG-3 校准时实测值减一点余量（2026-09-15，默认分词器）：

    实测   recall@5 0.932 · MRR@8 0.891 · 无命中返回空 4/5 · 有答案却返回空 1 · 陷阱排在答案前 0
    闸     recall@5 ≥ 0.91 · MRR@8 ≥ 0.87 · 无命中 ≥ 4/5 · 有答案却返回空 ≤ 1 · 陷阱在前 ≤ 1

红了先判断是**实现退步**还是**语料/产品定义变了**（改了 queries.jsonl 的标注）；
后者要在同一个提交里更新这里的实测值和原因，不许为了变绿只改阈值。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "evals" / "retrieval"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402


def _summary(ranker, query_set="single"):
    cards = harness.load_garden()
    queries = harness.load_queries(cards, query_set)
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


# ---------------------------------------------------------------- 多轮窗口（自动想起的真实查询形状）
#
# 查询照 io 的自动想起构造：最近 4 条 user/assistant 消息用换行拼起来（harness.chat_window_query）。
# 2026-09-15 实测（默认分词器，select_context cap 8）：
#
#                    recall@5  MRR@8  无命中返回空  有答案却返回空  平均带回
#   默认闸            0.962   0.923      0/3            0          7.88
#   strong_evidence_terms=8
#                     0.923   0.923      3/3            0          1.81


def test_default_select_context_finds_answers_in_multi_turn_windows():
    s = _summary(harness.mg_select, "multi_turn")
    assert s["recall@5"] >= 0.94, s["recall@5"]
    assert s["mrr@8"] >= 0.90, s["mrr@8"]
    assert s["false_empty"] == 0, s["false_empty"]
    assert s["trap_above_answer"] == 0, s["trap_above_answer"]


@pytest.mark.xfail(strict=True, reason=(
    "已知缺陷：默认闸在四条消息拼成的长查询上几乎全部放行 —— 杂卡靠泛词累加的分数越过固定的"
    "强证据闸，无命中窗口 3 条全部带回卡、平均每轮带回 7.9 张。放大闸（strong_evidence_terms）"
    "能修但会挡掉长粘贴里只靠一个编号命中的答案，所以默认没开；见 retrieval.DEFAULT_STRONG_EVIDENCE_TERMS。"
    "默认行为修好时这条会 XPASS 报红，提醒更新说明。"))
def test_default_select_context_returns_nothing_for_unrelated_chat_windows():
    s = _summary(harness.mg_select, "multi_turn")
    assert s["no_hit_correct"] == "3/3", s["no_hit_correct"]
    assert s["avg_returned"] <= 3, s["avg_returned"]


def test_scaled_strong_evidence_gates_unrelated_chat_windows():
    """宿主为自动想起打开放大闸时承诺的质量线：无命中全挡、平均带回不超过 2.5 张、答案仍在。"""
    s = _summary(harness.mg_select_scaled, "multi_turn")
    assert s["no_hit_correct"] == "3/3", s["no_hit_correct"]
    assert s["avg_returned"] <= 2.5, s["avg_returned"]
    assert s["recall@5"] >= 0.90, s["recall@5"]
    assert s["false_empty"] == 0, s["false_empty"]


def test_chat_window_query_matches_io_construction():
    """最近 4 条非空 user/assistant 消息按时间顺序换行拼接；system、空白消息不算。"""
    messages = [{"role": "user", "content": "一"}, {"role": "assistant", "content": "二"},
                {"role": "system", "content": "系统"}, {"role": "user", "content": "  "},
                {"role": "user", "content": "三"}, {"role": "openclaw", "content": "四"},
                {"role": "user", "content": "五"}]
    assert harness.chat_window_query(messages) == "二\n三\n四\n五"


# ---------------------------------------------------------------- 小花园（候选池 1–5 张卡）
#
# evals/retrieval/small_pool.py：一张答案卡 + 无关卡填到 N 张 / N 张无关卡。默认分词器、rank 默认参数、
# 2 个种子，2026-09-15 实测：
#
#   池子大小                 1      2      5
#   找到答案  下限关（F=1）  0.806  0.944  0.972
#             默认 F=20      0.917  0.972  1.000
#   无关卡被返回（两者相同） 0.014  0.087  0.188


def test_small_gardens_find_the_answer_without_letting_more_noise_in():
    import small_pool
    from memgarden import retrieval

    floor = retrieval.DEFAULT_COVERAGE_POOL_FLOOR
    table = small_pool.run(None, path="search", floors=[1, floor], sizes=[1, 2, 5],
                           seeds=2)["table"]
    for size, line in ((1, 0.90), (2, 0.96), (5, 0.99)):
        got = table[f"{floor}:{size}"]
        assert got["answer_found"] >= line, (size, got)
        assert got["noise_returned"] <= table[f"1:{size}"]["noise_returned"] + 0.01, (size, got)
