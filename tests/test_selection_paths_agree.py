"""包里有两条挑卡路，它们必须产出同一个结果。

## 为什么会有两条

    scoring/relevance.select_context_memories   一个函数 + mode 开关（较早）
    selection.Chain + 各段 stage                可组合的插口（后来的重构）

宿主 io 的生产流量走第一条；``GardenComponent`` 和 evals 走第二条。
两条调同一个打分器，但组装方式不同 —— **所以必须有测试盯着它们没分家**，
否则「换成组件之后召回结果变了」这种事只有用户能发现。

## 已经踩过一次

2026-08-29 实测：10 条查询里差 2 条。根因是 ``select_context_memories``
的相关性段**没有显式 tie-break** —— 分数并列时靠 Python 稳定排序保留输入顺序，
也就是选中哪张取决于宿主碰巧怎么排候选列表。
同一个查询、同一批卡，换个读取顺序结果就变，而且不报错。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from memgarden.scoring.relevance import select_context_memories
from memgarden.selection import Chain, RecentStage, RelevanceStage, RoleStage

CORPUS = pathlib.Path(__file__).resolve().parent.parent / "evals" / "corpus"


def _cards() -> list[dict]:
    rows = [json.loads(l) for l in (CORPUS / "cards.jsonl").read_text("utf-8").splitlines() if l.strip()]
    for c in rows:
        # relevance 那条路读 title/description，selection 那条读 summary/content。
        # 补齐两套字段名，让比较是公平的。
        c.setdefault("title", c.get("summary", ""))
        c.setdefault("description", c.get("content", ""))
        c.setdefault("search_text", f"{c.get('summary','')} {c.get('content','')}")
    return rows


def _queries() -> list[str]:
    return [json.loads(l)["query"]
            for l in (CORPUS / "queries.jsonl").read_text("utf-8").splitlines() if l.strip()]


def _chain() -> Chain:
    """和 ``select_context_memories`` 默认模式等价的组装：
    转折点 3 + 最近 2 + 相关 3，相关段宽松（score > 0 就要）。"""
    return Chain(stages=(
        RoleStage("turning_point", limit=3, order_by="occurred_at"),
        RecentStage(limit=2, order_by="created_at"),
        RelevanceStage(limit=3, any_score=True),
    ))


@pytest.mark.parametrize("query", _queries())
def test_both_selection_paths_pick_the_same_cards(query: str) -> None:
    cards = _cards()
    legacy = [c["id"] for c in select_context_memories(cards, query, cap=8)]
    chained = list(_chain().select(cards, query, limit=8).card_ids)
    assert legacy == chained, (
        f"两条挑卡路对「{query}」给出了不同结果。\n"
        f"  relevance: {legacy}\n  Chain:     {chained}\n"
        f"换成 GardenComponent 之后宿主的召回会变 —— 先弄清哪条对，别直接改语料。"
    )


def test_selection_does_not_depend_on_candidate_order() -> None:
    """候选列表的顺序**不该影响结果**。

    这是上面那次分歧的根因：并列分数靠输入顺序决定谁进来。
    宿主换个读取顺序（改了 SQL 的 ORDER BY、加了缓存）召回就变，而且不报错。
    """
    cards = _cards()
    for query in _queries():
        forward = [c["id"] for c in select_context_memories(cards, query, cap=8)]
        backward = [c["id"] for c in select_context_memories(list(reversed(cards)), query, cap=8)]
        assert sorted(forward) == sorted(backward), (
            f"「{query}」：候选顺序反过来，选中的卡就变了 —— 排序不确定"
        )


# --------------------------------------------------------------------------- #
# 并列用例必须自带，不能靠语料碰巧有
# --------------------------------------------------------------------------- #
#
# 一开始这个守卫是拿共享语料测的 —— 那份语料当时 17 张卡全都没有 created_at，
# 所以「最近」那段完全并列，顺序依赖一测就露。
#
# 后来给语料补上了 created_at（真实卡本来就有），并列消失，**守卫也跟着失去了牙**：
# 把 tie-break 删掉，测试照样全绿。
#
# 教训：守卫要自带它要守的那个形状，不能指望共享数据碰巧提供。

_TIED = [
    # 三张卡的时间**完全相同** —— 真实数据里很常见（同一批导入、同一秒写入）
    {"id": "t_a", "summary": "他不吃辣", "content": "一吃辣就胃疼",
     "occurred_at": "2026-08-01", "created_at": "2026-08-01"},
    {"id": "t_b", "summary": "养了只柯基", "content": "叫崽崽，三岁",
     "occurred_at": "2026-08-01", "created_at": "2026-08-01"},
    {"id": "t_c", "summary": "换了个手机壳", "content": "蓝色的",
     "occurred_at": "2026-08-01", "created_at": "2026-08-01"},
]


def _with_both_field_names(cards: list[dict]) -> list[dict]:
    out = []
    for c in cards:
        c = dict(c)
        c.setdefault("title", c.get("summary", ""))
        c.setdefault("description", c.get("content", ""))
        c.setdefault("search_text", f"{c.get('summary','')} {c.get('content','')}")
        out.append(c)
    return out


@pytest.mark.parametrize("query", ["狗", "吃什么", "手机"])
def test_tied_timestamps_still_give_a_stable_answer(query: str) -> None:
    """时间完全相同的卡，选中哪张必须**与输入顺序无关**。

    真实数据里同时间很常见：同一批历史导入、同一秒落的几张卡。
    没有最终 tie-break 的话，宿主改一下 SQL 的 ORDER BY，召回就变。
    """
    cards = _with_both_field_names(_TIED)
    orders = [cards, list(reversed(cards)), [cards[1], cards[2], cards[0]]]
    results = [
        tuple(c["id"] for c in select_context_memories(o, query, cap=2)) for o in orders
    ]
    assert len(set(results)) == 1, (
        f"「{query}」：候选顺序不同，选中的卡就不同 —— {results}"
    )


@pytest.mark.parametrize("query", ["狗", "吃什么", "手机"])
def test_both_paths_agree_on_tied_timestamps(query: str) -> None:
    """并列时两条路也要给同一个答案 —— 这正是它们最容易分家的地方。"""
    cards = _with_both_field_names(_TIED)
    legacy = [c["id"] for c in select_context_memories(cards, query, cap=2)]
    chained = list(Chain(stages=(
        RecentStage(limit=2, order_by="created_at"),
    )).select(cards, query, limit=2).card_ids)
    assert legacy == chained, f"「{query}」并列时分家了：{legacy} vs {chained}"
