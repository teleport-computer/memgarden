"""关联读取：纯函数 ``one_hop`` 与挂了 Store 的 ``MountedGarden.related``。

## 黄金用例从哪来

``fixtures/related_one_hop_golden.json`` 的每条 ``expected`` 都是把同一份输入
喂给宿主 io 的 ``recall_metadata.one_hop``（release/memory-overhaul 分支，
只读运行）得到的输出。本仓库不 import io —— 输入和输出一起抄进来，
行为以它们为准：v1 承诺和 io 逐项一致，改语义要先改这份黄金用例并说明原因。

用例覆盖：io 自己的回归（历史卡 × 三种关系、跨源显式链接胜出、cap 稳定）、
cap 0/1/7/50、摘要空白与长度、链接字段的各种坏形状、生命周期变体、源卡自身
被排除、非法 id、重复候选、源 id 为空，以及 120 组带种子的随机花园。
"""
from __future__ import annotations

import copy
import json
import pathlib
import tempfile

import pytest

from memgarden.mounted import MountedGarden, Scope
from memgarden.related import one_hop
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore

GOLDEN = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "related_one_hop_golden.json")
    .read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", GOLDEN, ids=[c["name"] for c in GOLDEN])
def test_one_hop_matches_the_io_golden_output(case):
    kwargs = {"cap": case["cap"]} if "cap" in case else {}
    sources, candidates = copy.deepcopy(case["sources"]), copy.deepcopy(case["candidates"])
    assert one_hop(sources, candidates, **kwargs) == case["expected"]
    # 纯函数：不改宿主传进来的东西。
    assert sources == case["sources"] and candidates == case["candidates"]


def test_the_golden_set_actually_exercises_every_relation_and_history():
    """黄金用例要是全是空结果，上面那条测试就什么也证明不了。"""
    seen = {(item["relation"], item["status"]) for c in GOLDEN for item in c["expected"]}
    assert {("anchor", "active"), ("supersedes", "active"), ("thread", "active"),
            ("anchor", "superseded"), ("supersedes", "superseded")} <= seen
    assert not any(item["relation"] == "thread" and item["status"] == "superseded"
                   for c in GOLDEN for item in c["expected"])
    assert sum(1 for c in GOLDEN if c["expected"]) >= 50


@pytest.mark.parametrize("status", ["archived", "superseded", "deleted"])
@pytest.mark.parametrize("relation", ["thread", "anchor", "supersedes"])
def test_retired_cards_need_an_explicit_link_and_must_be_superseded(status, relation):
    source = {"id": "source", "threads": ["t"]}
    if relation != "thread":
        source["anchor_memory_ids" if relation == "anchor" else "supersedes"] = ["old"]
    candidates = [{"id": "old", "summary": "历史摘要", "status": status, "threads": ["t"]},
                  {"id": "now", "summary": "当前摘要", "threads": ["t"]}]
    expected = [{"id": "now", "summary": "当前摘要", "source_id": "source",
                 "relation": "thread", "status": "active"}]
    if status == "superseded" and relation != "thread":
        expected.insert(0, {"id": "old", "summary": "历史摘要", "source_id": "source",
                            "relation": relation, "status": "superseded"})
    assert one_hop([source], candidates) == expected


def test_explicit_link_wins_across_sources_and_order_does_not_matter():
    sources = [{"id": "s1", "threads": ["t"]}, {"id": "s2", "anchor_memory_ids": ["z"]}]
    cards = [{"id": mid, "summary": mid, "threads": ["t"]} for mid in "abcdefghz"]
    picked = one_hop(sources, cards)
    assert len(picked) == 6
    assert picked[0] == {"id": "z", "summary": "z", "source_id": "s2",
                         "relation": "anchor", "status": "active"}
    assert one_hop(list(reversed(sources)), list(reversed(cards))) == picked


def test_no_multi_hop_and_no_reverse_lookup():
    """B 锚定 C 不会让 A 看到 C；旧卡的 superseded_by 不会把新卡带出来。"""
    a = {"id": "a", "anchor_memory_ids": ["b"]}
    b = {"id": "b", "summary": "B", "anchor_memory_ids": ["c"]}
    c = {"id": "c", "summary": "C"}
    assert [r["id"] for r in one_hop([a], [b, c])] == ["b"]
    old = {"id": "old", "summary": "旧", "superseded_by": "new", "status": "superseded"}
    new = {"id": "new", "summary": "新", "supersedes": ["old"]}
    assert one_hop([old], [new]) == []


@pytest.mark.parametrize("cap", [-1, 1.5, "6", True])
def test_cap_must_be_a_non_negative_int(cap):
    with pytest.raises(ValueError):
        one_hop([], [], cap=cap)


# ------------------------------------------------------ MountedGarden.related

TENANT = "acme"


class _NoModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:  # pragma: no cover
        raise AssertionError("related() must not call the model")


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    if request.param == "memory":
        return InMemoryStore()
    return SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "related.db"))


def _card(mid, summary, **extra):
    return {"id": mid, "summary": summary, "content": f"{summary}的正文", **extra}


def _apply(store, owner, mutations, key):
    store.apply(TENANT, mutations, owner=owner, idempotency_key=key)


def _scope(owner="alice", **kw):
    return Scope(tenant_id=TENANT, memory_owner_id=owner, **kw)


def _seed(store):
    _apply(store, "alice", [
        {"op": "add", "card": _card("trip", "搬家计划", threads=["搬家"],
                                    anchor_memory_ids=["house"])},
        {"op": "add", "card": _card("house", "看中了一套两居", threads=["看房"])},
        {"op": "add", "card": _card("budget_v1", "预算三百万", threads=["搬家"])},
        {"op": "add", "card": _card("boxes", "打包纸箱买好了", threads=["搬家"])},
        {"op": "add", "card": _card("filed", "旧的搬家清单", threads=["搬家"])},
        {"op": "add", "card": _card("gone", "被用户删掉的搬家卡", threads=["搬家"])},
        {"op": "add", "card": _card("shared", "家庭群里的搬家卡", threads=["搬家"],
                                    mount="family-shared")},
    ], "seed")
    _apply(store, "alice", [
        {"op": "supersede", "target_id": "budget_v1",
         "card": _card("budget_v2", "预算改成三百五十万", threads=["预算"])},
        {"op": "archive", "record_id": "filed", "reason": "整理"},
        {"op": "delete", "record_id": "gone", "requested_by": "user"},
    ], "lifecycle")
    _apply(store, "bob", [
        {"op": "add", "card": _card("bob_card", "bob 的搬家卡", threads=["搬家"])},
    ], "bob")


def test_related_reads_neighbours_through_the_store(store):
    _seed(store)
    garden = MountedGarden(model=_NoModel(), store=store)
    got = garden.related(_scope(), ["trip"])
    assert got == [
        {"id": "house", "summary": "看中了一套两居", "source_id": "trip",
         "relation": "anchor", "status": "active"},
        {"id": "boxes", "summary": "打包纸箱买好了", "source_id": "trip",
         "relation": "thread", "status": "active"},
    ]
    # 归档卡（filed）、硬删卡（gone）、别的挂载点（shared）、别的 owner（bob_card）、
    # 被取代但没有显式链接的历史卡（budget_v1）都不在里面。


def test_the_store_supersede_chain_counts_as_an_explicit_link(store):
    """参考 Store 只在旧卡上写 superseded_by；新卡取回时要能看到它取代了谁。"""
    _seed(store)
    garden = MountedGarden(model=_NoModel(), store=store)
    assert garden.related(_scope(), ["budget_v2"]) == [
        {"id": "budget_v1", "summary": "预算三百万", "source_id": "budget_v2",
         "relation": "supersedes", "status": "superseded"},
    ]
    # 反方向不做：从旧卡出发不会找到取代它的新卡，旧卡默认也不能当源。
    assert garden.related(_scope(), ["budget_v1"]) == []
    from_old = garden.related(_scope(), ["budget_v1"], include_superseded=True)
    assert "budget_v2" not in {r["id"] for r in from_old}


def test_another_owner_and_unknown_ids_get_nothing(store):
    _seed(store)
    garden = MountedGarden(model=_NoModel(), store=store)
    assert garden.related(_scope("bob"), ["trip"]) == []
    assert garden.related(_scope("bob"), ["bob_card"]) == []  # alice 的卡不是 bob 的候选
    assert garden.related(_scope(), ["gone"]) == []
    assert garden.related(_scope(), ["nope", ""]) == []
    assert garden.related(_scope(), []) == []


def test_hard_deleted_cards_never_come_back_even_when_linked(store):
    _apply(store, "alice", [
        {"op": "add", "card": _card("src", "源卡", anchor_memory_ids=["dead"],
                                    supersedes=["dead2"])},
        {"op": "add", "card": _card("dead", "要删的卡")},
        {"op": "add", "card": _card("dead2", "也要删", status="superseded")},
    ], "seed")
    _apply(store, "alice", [
        {"op": "delete", "record_id": "dead", "requested_by": "user"},
        {"op": "delete", "record_id": "dead2", "requested_by": "user"},
    ], "del")
    garden = MountedGarden(model=_NoModel(), store=store)
    assert garden.related(_scope(), ["src"]) == []


def test_mount_scope_is_respected(store):
    _seed(store)
    garden = MountedGarden(model=_NoModel(), store=store)
    both = _scope(allowed_mounts=("agent-private", "family-shared"))
    assert "shared" in {r["id"] for r in garden.related(both, ["trip"], cap=10)}
    assert garden.related(_scope(allowed_mounts=("family-shared",)), ["trip"]) == []


def test_unknown_lifecycle_values_fail_closed(store):
    _apply(store, "alice", [
        {"op": "add", "card": _card("src", "源卡", threads=["t"], anchor_memory_ids=["odd"])},
        {"op": "add", "card": _card("odd", "状态没见过", status="pending", threads=["t"])},
        {"op": "add", "card": _card("ok", "正常", threads=["t"])},
    ], "seed")
    garden = MountedGarden(model=_NoModel(), store=store)
    assert [r["id"] for r in garden.related(_scope(), ["src"])] == ["ok"]


def test_ids_must_be_a_list(store):
    garden = MountedGarden(model=_NoModel(), store=store)
    with pytest.raises(ValueError):
        garden.related(_scope(), "trip")


def test_recall_search_and_related_share_one_lifecycle_filter(store):
    """想起、搜索、关联读取走同一个 _scoped_cards：外部 Store 直接写的 status 也算数。

    以前想起和搜索只靠 Store 的 archived / superseded_by 过滤，一张写着
    ``status="archived"``（或没见过的 ``pending``）的卡能被搜到、被想起，关联读取却认它是归档卡。
    """
    _apply(store, "alice", [
        {"op": "add", "card": _card("live", "搬家纸箱", threads=["搬家"])},
        {"op": "add", "card": _card("filed", "搬家纸箱旧清单", status="archived", threads=["搬家"])},
        {"op": "add", "card": _card("odd", "搬家纸箱待定", status="pending", threads=["搬家"])},
        {"op": "add", "card": _card("src", "搬家", threads=["搬家"])},
    ], "seed")
    garden = MountedGarden(model=_NoModel(), store=store)
    scope = _scope()
    found = set(garden.search(scope, "搬家纸箱").record_ids)
    recalled = set(garden.context_for_turn(scope, "搬家纸箱").record_ids)
    related = {r["id"] for r in garden.related(scope, ["src"], cap=10)}
    assert "live" in found and "live" in related
    for gone in ("filed", "odd"):
        assert gone not in found and gone not in recalled and gone not in related
