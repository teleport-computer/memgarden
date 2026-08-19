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

from memory_garden.storage import FULL_CAPABILITIES, StoragePort  # noqa: E402
from memory_garden.stores.memory import InMemoryStore  # noqa: E402
from memory_garden.stores.sqlite import SqliteStore  # noqa: E402


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
