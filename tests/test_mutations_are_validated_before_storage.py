"""不合法的改动**根本不进 Store**（sevenfloor 2026-09-02 §3.2 第 4/5 条）。

以前是「送到 Store，Store 不认识就抛」。三个问题：

1. 每个 Store 各判一遍，松紧不一 —— 官方 SQLite 抛 ValueError，别人的适配器
   可能默默跳过那条，于是记忆悄悄少了一条而没有任何报错。
2. 错误从**存储层**冒出来，调用方看到的是和自己代码对不上号的话
   （实测那句是 `unknown op: merge`）。
3. 批次里第 3 条不合法时，前 2 条可能已经写进去了 —— 那取决于 Store 有没有
   事务，不该由「这条 mutation 合不合法」来决定。
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from memgarden import Actor, MountedGarden, Scope, SqliteStore
from memgarden.records import (
    InvalidMutation,
    UnknownMutation,
    required_capabilities,
    validate_mutations,
)

ME = Scope(tenant_id="t1", memory_owner_id="owner-1", actor=Actor(user_id="u1"))


class _NoModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        raise AssertionError("这些用例不该调模型")


def _garden(store=None) -> MountedGarden:
    return MountedGarden(
        model=_NoModel(),
        store=store or SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db")),
    )


# --------------------------------------------------------------------------- #
# 校验本身
# --------------------------------------------------------------------------- #

def test_an_unknown_op_is_rejected():
    with pytest.raises(UnknownMutation):
        validate_mutations([{"op": "merge", "card_ids": ["m_1"]}])


@pytest.mark.parametrize("bad,missing", [
    ({"op": "add"}, "card"),
    ({"op": "supersede", "target_id": "m_1"}, "card"),
    ({"op": "supersede", "card": {"summary": "s", "content": "c"}}, "target"),
    ({"op": "update", "record_id": "m_1"}, "changes"),
    ({"op": "update", "changes": {"summary": "s"}}, "record_id"),
    ({"op": "archive"}, "record_id"),
    ({"op": "delete"}, "record_id"),
    # 删除必须能追溯到是谁要求的 —— ``Delete`` 的文档这么承诺，
    # 校验就得真的查，否则那句承诺是空的。
    ({"op": "delete", "record_id": "m_1"}, "requested_by"),
])
def test_missing_required_fields_are_rejected(bad, missing):
    """每个 op 的必填字段都要守住。

    ``supersede`` 少了 card 尤其致命：旧卡被标成「已被取代」，而取代它的新卡
    根本不存在 —— 那条记忆就凭空消失了，且不报错。
    """
    with pytest.raises(InvalidMutation) as exc:
        validate_mutations([bad])
    assert missing in str(exc.value)


def test_capabilities_are_derived_from_the_type_not_declared_by_the_caller():
    """需要什么能力由**类型**推出来，不靠调用方记得声明。

    靠自觉的检查等于没有检查：漏声明的那次不会报错，只会在不支持的存储上
    写出半个结果。
    """
    typed = validate_mutations([
        {"op": "add", "card": {"summary": "a", "content": "aa"}},
        {"op": "supersede", "target_id": "m_1",
         "card": {"summary": "b", "content": "bb"}},
    ])
    needed = required_capabilities(typed)
    assert "supersede" in needed
    # 多条改动 → 必须原子。半成功会留下自相矛盾的状态。
    assert "atomic_batch" in needed


# --------------------------------------------------------------------------- #
# 挂载入口：不合法就不进 Store
# --------------------------------------------------------------------------- #

def test_nothing_is_written_when_one_row_in_the_batch_is_invalid():
    """批次里有一条不合法 —— 前面合法的那些也不能落地。"""
    store = SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db"))
    garden = _garden(store)

    receipt = garden._apply(ME, "agent-private", [
        {"op": "add", "card": {"summary": "这条本来合法", "content": "正文"}},
        {"op": "merge", "card_ids": ["m_1"]},          # 未知 op
    ], idempotency_key="k1", trace={})

    assert not receipt.written
    assert receipt.error and receipt.error.startswith("invalid_mutation:")
    assert store.load("t1", owner="owner-1").cards == [], "不合法的批次却写进去了东西"


def test_the_error_names_the_mutation_not_the_storage_layer():
    """报错要让调用方看得懂是自己哪条 mutation 的问题。"""
    receipt = _garden()._apply(ME, "agent-private",
                               [{"op": "merge", "card_ids": ["m_1"]}],
                               idempotency_key="k1", trace={})
    assert "merge" in (receipt.error or ""), receipt.error


def test_storage_that_cannot_supersede_is_refused_up_front():
    """存储声明做不到的能力 —— 提前拒绝，不要写一半再失败。

    半成品状态最难查：库里既有新卡又有没归档的旧卡，而且没有报错。
    """
    class _NoSupersede(SqliteStore):
        def capabilities(self):
            caps = super().capabilities()
            from dataclasses import replace
            return replace(caps, supports_supersede=False)

    store = _NoSupersede(str(pathlib.Path(tempfile.mkdtemp()) / "g.db"))
    store.apply("t1", [{"op": "add", "card": {"id": "m_1", "summary": "旧的"}}],
                owner="owner-1", idempotency_key="seed")

    receipt = _garden(store)._apply(ME, "agent-private", [
        {"op": "supersede", "target_id": "m_1",
         "card": {"summary": "新的", "content": "正文"}},
    ], idempotency_key="k1", trace={})

    assert not receipt.written
    assert "storage_lacks_capabilities" in (receipt.error or "")
    assert "supersede" in (receipt.error or "")
    # 旧卡必须原样活着
    assert [c["id"] for c in store.load("t1", owner="owner-1").cards] == ["m_1"]


def test_a_store_that_does_not_declare_its_capabilities_is_refused():
    """存储不声明 ``capabilities()`` 时 —— **一律当作做不到（fail closed）**。

    这条以前是反的：没有声明就当成「全部支持」。那正好错在最危险的方向 ——
    一个不声明能力的适配器会被当成什么都能做，于是 supersede 被下发给一个
    只会覆盖的后端，「永远不硬删」在没有任何报错的情况下破掉。

    而 :mod:`memgarden.storage` 从一开始就写着「``Capabilities`` 没有默认值，
    外部适配器要逐项写清楚」。那条约束会被这一个 fallback 全部抵消。

    代价是简单适配器要多写一个方法。这个代价是对的：写一个 ``capabilities()``
    要五分钟，查一次「记忆被静默覆盖了」要两天。
    """
    class _Bare:
        def __init__(self): self.written = []
        def load(self, tenant, *, owner, **f):
            from memgarden.storage import Snapshot
            return Snapshot(cards=[], revision="0", owner=owner)
        def apply(self, tenant, mutations, *, owner, idempotency_key,
                  expected_revision, maintenance_state=None):
            from memgarden.storage import ApplyResult
            self.written.extend(mutations)
            return ApplyResult(results=[{"id": "m_1"}], revision="1")

    bare = _Bare()
    receipt = _garden(bare)._apply(ME, "agent-private", [
        # supersede 需要 supports_supersede —— 没声明就不该放行。
        {"op": "supersede", "target_id": "m_0",
         "card": {"summary": "新的", "content": "正文"}},
    ], idempotency_key="k1", trace={})
    assert not receipt.written
    assert receipt.error.startswith("storage_lacks_capabilities"), receipt.error
    # 🔴 最要紧的一句：**一条都没送到存储**。
    # 「先写一半再失败」留下的半成品状态最难查：库里既有新卡又有没归档的旧卡。
    assert bare.written == []


def test_a_store_that_declares_everything_is_allowed_through():
    """反过来：如实声明了全部能力的存储照常放行。

    有这一条，上面那条才不会被误读成「fail closed 就是全都拒绝」。
    """
    from memgarden.storage import FULL_CAPABILITIES

    class _Declared:
        def __init__(self): self.written = []
        def capabilities(self): return FULL_CAPABILITIES
        def load(self, tenant, *, owner, **f):
            from memgarden.storage import Snapshot
            return Snapshot(cards=[], revision="0", owner=owner)
        def apply(self, tenant, mutations, *, owner, idempotency_key,
                  expected_revision, maintenance_state=None):
            from memgarden.storage import ApplyResult
            self.written.extend(mutations)
            return ApplyResult(results=[{"id": "m_1"}], revision="1")

    store = _Declared()
    receipt = _garden(store)._apply(ME, "agent-private", [
        {"op": "add", "card": {"summary": "写得进去", "content": "正文"}},
    ], idempotency_key="k1", trace={})
    assert receipt.written, receipt.error
    assert store.written
