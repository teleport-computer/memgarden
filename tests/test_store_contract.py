"""两个存储实现跑**同一套**契约测试。

这么写的用意：接口是不是真的可替换，不能靠文档声称，得靠同一组断言在两个
实现上都过。将来接第三个实现（Postgres / Notion / 别人的库）时，
把它加进 `STORES` 就能立刻知道缺什么。

覆盖的是 `storage.py` 里定的四件事：
  能力声明 / CAS 版本号 / 幂等键 / supersede 的原子性
"""
from __future__ import annotations

import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
PROTO = pathlib.Path(__file__).resolve().parents[2] / "agent-protocol-core" / "src"
for p in (str(SRC), str(PROTO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from memgarden.storage import FULL_CAPABILITIES, StoragePort  # noqa: E402
from memgarden.stores.memory import InMemoryStore  # noqa: E402
from memgarden.stores.sqlite import SqliteStore  # noqa: E402


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStore()
    return SqliteStore(tmp_path / "t.db")


T = "tenant_a"


def _add(summary: str) -> dict:
    return {"op": "add", "card": {"summary": summary, "content": f"{summary} 的正文"}}


# --------------------------------------------------------------------------- #
# 结构一致性
# --------------------------------------------------------------------------- #


def test_conforms_to_the_port(store):
    assert isinstance(store, StoragePort)


def test_declares_full_capabilities(store):
    assert store.capabilities() == FULL_CAPABILITIES


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #


def test_write_then_read_back(store):
    store.apply(T, [_add("我有一只狗")], idempotency_key="k1")
    cards = store.load(T).cards
    assert [c["summary"] for c in cards] == ["我有一只狗"]


def test_tenants_are_isolated(store):
    store.apply(T, [_add("A 的记忆")], idempotency_key="k1")
    store.apply("tenant_b", [_add("B 的记忆")], idempotency_key="k1")
    assert [c["summary"] for c in store.load(T).cards] == ["A 的记忆"]
    assert [c["summary"] for c in store.load("tenant_b").cards] == ["B 的记忆"]


# --------------------------------------------------------------------------- #
# CAS 版本号
# --------------------------------------------------------------------------- #


def test_revision_advances_after_each_write(store):
    r0 = store.load(T).revision
    r1 = store.apply(T, [_add("一")], idempotency_key="k1").revision
    r2 = store.apply(T, [_add("二")], idempotency_key="k2").revision
    assert r0 != r1 != r2


def test_stale_expected_revision_is_rejected(store):
    store.apply(T, [_add("一")], idempotency_key="k1")
    with pytest.raises(RuntimeError):
        store.apply(T, [_add("二")], idempotency_key="k2", expected_revision="0")


def test_fresh_expected_revision_is_accepted(store):
    rev = store.load(T).revision
    store.apply(T, [_add("一")], idempotency_key="k1", expected_revision=rev)
    assert len(store.load(T).cards) == 1


# --------------------------------------------------------------------------- #
# 幂等键
# --------------------------------------------------------------------------- #


def test_replaying_the_same_key_does_not_write_twice(store):
    first = store.apply(T, [_add("只该有一张")], idempotency_key="same")
    again = store.apply(T, [_add("只该有一张")], idempotency_key="same")
    assert len(store.load(T).cards) == 1, "同一个幂等键重放写了两次"
    assert first.results == again.results
    assert first.revision == again.revision


def test_different_keys_write_separately(store):
    store.apply(T, [_add("一")], idempotency_key="k1")
    store.apply(T, [_add("二")], idempotency_key="k2")
    assert len(store.load(T).cards) == 2


# --------------------------------------------------------------------------- #
# supersede：不硬删，且要原子
# --------------------------------------------------------------------------- #


def test_supersede_archives_the_old_card_instead_of_deleting(store):
    res = store.apply(T, [_add("旧的说法")], idempotency_key="k1")
    old_id = res.results[0]["id"]
    store.apply(
        T,
        [{"op": "supersede", "target_id": old_id, "card": {"summary": "新的说法"}}],
        idempotency_key="k2",
    )
    visible = store.load(T).cards
    assert [c["summary"] for c in visible] == ["新的说法"], "旧卡还露在外面"

    everything = store.load(T, include_archived=True, include_superseded=True).cards
    old = [c for c in everything if c["id"] == old_id]
    assert old, "旧卡被硬删了 —— 契约要求只归档不删除"
    assert old[0]["superseded_by"], "没记下它被谁取代"


def test_a_failed_mutation_rolls_back_the_whole_batch(store):
    store.apply(T, [_add("先有一张")], idempotency_key="k1")
    with pytest.raises(KeyError):
        store.apply(
            T,
            [_add("这张不该留下"),
             {"op": "supersede", "target_id": "不存在的 id", "card": {"summary": "x"}}],
            idempotency_key="k2",
        )
    assert len(store.load(T).cards) == 1, "批次里前半截被写进去了 —— 不是原子的"


def test_unknown_op_is_rejected(store):
    with pytest.raises(ValueError):
        store.apply(T, [{"op": "打个响指"}], idempotency_key="k1")


# --------------------------------------------------------------------------- #
# 2026-08-27 Seven 评审 §10.3 / §10.4：两个静默丢数据的 bug
# --------------------------------------------------------------------------- #

def test_deleting_a_card_never_lets_the_next_one_overwrite_a_survivor(store):
    """**删一条之后新增，绝不能覆盖幸存者。**

    原实现用「当前总数 + 1」生成 id：删掉一条后计数回退，算出的 id 撞上已存在
    的卡，而写入是 upsert（有则覆盖）——

        初始      {m_1: first, m_2: second}
        删掉 m_1  {m_2: second}
        新增      COUNT=1 → 算出 m_2 → **覆盖掉 second，不报错**

    这是最坏的一类 bug：静默丢数据。用户的一条记忆凭空消失，没有任何错误可查。
    """
    t = "tenant-overwrite"
    store.apply(t, [{"op": "add", "card": {"summary": "first"}}], idempotency_key="k1")
    store.apply(t, [{"op": "add", "card": {"summary": "second"}}], idempotency_key="k2")
    survivors_before = {c["summary"] for c in store.load(t).cards}

    store.apply(t, [{"op": "delete", "target_id": "m_1"}], idempotency_key="k3")
    store.apply(t, [{"op": "add", "card": {"summary": "third"}}], idempotency_key="k4")

    after = {c["summary"] for c in store.load(t).cards}
    assert "second" in after, (
        f"删除后新增覆盖了幸存的卡：删前 {survivors_before}，删后新增得到 {after}"
    )
    assert "third" in after


def test_the_same_idempotency_key_with_different_content_is_a_conflict(store):
    """**同一个幂等键送来不同内容 = 冲突，不能静默返回旧结果。**

    幂等键的语义是「同一个请求重放，别写第二遍」。同 key 不同内容不是重放，
    是两个不同请求撞了 key（多半是调用方的键生成漏了这批改动的标识）。

    静默返回第一次的结果，会让第二批改动凭空消失，而调用方以为写成功了 ——
    用户那边的表现是「说了话但没记住」，且没有任何错误可查。
    """
    from memgarden.storage import IdempotencyConflict

    t = "tenant-idem"
    store.apply(t, [{"op": "add", "card": {"summary": "A"}}], idempotency_key="same")

    with pytest.raises(IdempotencyConflict):
        store.apply(t, [{"op": "add", "card": {"summary": "B 完全不同"}}],
                    idempotency_key="same")


def test_a_genuine_replay_is_still_idempotent(store):
    """反向：**真正的重放必须还是幂等的。**

    上一条守「别静默吞掉不同内容」，这条守「别把正常重试也当成冲突」——
    只守一边的话，把幂等键改成「永远报冲突」也能让上一条变绿。
    """
    t = "tenant-replay"
    mutations = [{"op": "add", "card": {"summary": "A", "content": "x"}}]
    first = store.apply(t, mutations, idempotency_key="same")
    again = store.apply(t, list(mutations), idempotency_key="same")

    assert again.results == first.results
    assert again.revision == first.revision
    assert len(store.load(t).cards) == 1, "重放写进了第二份"
