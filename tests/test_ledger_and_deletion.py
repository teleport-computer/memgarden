"""整理账本的持久化，和用户主动删除的真删。

## 账本：为什么不能丢给宿主存

以前 signature / seed_card_count 只出现在返回的 ``trace`` 里，注释写着
「宿主要存回去」。宿主重启一次就忘了 —— 表现是**同一批卡被反复合并**。
而且宿主分两步存时，两种坏法都很隐蔽：

    账本先走 → 这批整理再也不会跑，改动丢了没人知道
    卡先走   → 下次照样整理同一批，重复合并

## 删除：archive 不是 delete

产品已经拍板：用户明确删除是**真删**；整理时的收敛用 archive/supersede。
混在一起的后果是界面说「已删除」而库里原封不动 —— 当着用户的面撒谎。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from memgarden.contracts import MaintenanceRequest
from memgarden.mounted import MountedGarden, Scope
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore

ME = Scope(tenant_id="t1", memory_owner_id="owner-1")


def _db() -> str:
    return str(pathlib.Path(tempfile.mkdtemp()) / "g.db")


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    return InMemoryStore() if request.param == "memory" else SqliteStore(_db())


class _Tidy:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return json.dumps({"consolidations": [{
            "op": "merge", "card_ids": ["m_1", "m_2"],
            "rationale": "这两条讲的是同一件事。",
            "result": {"bucket": "未分类", "threads": [],
                       "summary": "合并后的", "content": "合并后的正文。"},
        }]}, ensure_ascii=False)


def _seed(store, n: int = 12) -> None:
    store.apply("t1", [{"op": "add",
                        "card": {"id": f"m_{i}", "summary": f"第 {i} 条",
                                 "content": f"正文 {i}"}}
                       for i in range(1, n + 1)],
                owner="owner-1", idempotency_key="seed", expected_revision=None)


def _garden(store):
    return MountedGarden(model=_Tidy(), store=store,
                         min_new_cards_for_maintenance=1)


def test_the_ledger_lands_in_the_store(store):
    _seed(store)
    assert _garden(store).run_and_store_maintenance(
        ME, MaintenanceRequest(locale="zh-Hans")).written
    ledger = store.maintenance_state("t1", owner="owner-1", mount="agent-private")
    assert ledger.get("signature"), "整理跑完了但账本是空的"


def test_a_second_check_right_after_says_not_needed(store):
    """整理成功之后立刻再问一次 —— 不该再整理同一批。"""
    _seed(store)
    garden = _garden(store)
    assert garden.run_and_store_maintenance(
        ME, MaintenanceRequest(locale="zh-Hans")).written
    again = garden.run_and_store_maintenance(ME, MaintenanceRequest(locale="zh-Hans"))
    assert again.reason == "not_needed", f"又整理了一遍: {again}"


def test_the_ledger_survives_closing_and_reopening_the_database():
    """**关掉再打开** SQLite，仍然不重复整理。

    这一条是账本必须落库的直接理由：进程内存里的状态活不过重启。
    """
    db = _db()
    _seed(SqliteStore(db))
    assert _garden(SqliteStore(db)).run_and_store_maintenance(
        ME, MaintenanceRequest(locale="zh-Hans")).written

    reopened = SqliteStore(db)          # 全新的连接，模拟进程重启
    again = _garden(reopened).run_and_store_maintenance(
        ME, MaintenanceRequest(locale="zh-Hans"))
    assert again.reason == "not_needed", f"重启后又整理了一遍: {again}"


def test_the_ledger_does_not_advance_when_the_mutations_fail(store):
    """卡改动失败 → 账本不能推进。

    推进了的话，这批整理再也不会跑，而它其实一次都没成功过。
    """
    _seed(store)

    class _Broken(type(store)):
        pass

    def _boom(*a, **k):
        raise RuntimeError("写库炸了")

    original = store.apply
    store.apply = _boom          # type: ignore[method-assign]
    out = _garden(store).run_and_store_maintenance(
        ME, MaintenanceRequest(locale="zh-Hans"))
    store.apply = original       # type: ignore[method-assign]

    assert not out.written
    assert not store.maintenance_state(
        "t1", owner="owner-1", mount="agent-private").get("signature")


def test_deleting_a_record_really_removes_it(store):
    """用户删除之后：召回看不到、浏览看不到、导出里也没有正文。"""
    _seed(store, 3)
    garden = MountedGarden(model=_Tidy(), store=store)
    out = garden.delete_record(ME, "m_1", requested_by="user:u1", reason="不想留")
    assert out.written, out.error

    assert "m_1" not in {c["id"] for c in store.load("t1", owner="owner-1").cards}
    everything = store.load("t1", owner="owner-1", include_archived=True,
                            include_superseded=True).cards
    assert "m_1" not in {c["id"] for c in everything}, "只是归档了，没真删"
    assert "m_1" not in {getattr(r, "id", None) for r in garden.browse(ME)}


def test_a_deleted_record_stays_gone_after_reopening_the_database():
    db = _db()
    _seed(SqliteStore(db), 3)
    MountedGarden(model=_Tidy(), store=SqliteStore(db)).delete_record(
        ME, "m_1", requested_by="user:u1")
    survivors = {c["id"] for c in SqliteStore(db).load(
        "t1", owner="owner-1", include_archived=True,
        include_superseded=True).cards}
    assert "m_1" not in survivors


def test_deletion_requires_saying_who_asked_for_it(store):
    """删除必须能追溯到是谁要求的 —— 审计和合规都指着这个字段。"""
    _seed(store, 3)
    out = MountedGarden(model=_Tidy(), store=store).delete_record(
        ME, "m_1", requested_by="")
    assert out.error == "requested_by_required"


def test_a_store_that_cannot_hard_delete_refuses_instead_of_archiving(store):
    """后端做不到真删 → **拒绝**，不降级成归档。

    降级的后果是界面说「已删除」而内容还在。这不是体验退化，是撒谎。
    """
    from memgarden.storage import Capabilities

    _seed(store, 3)

    class _NoDelete(type(store)):
        pass

    store.capabilities = lambda: Capabilities(     # type: ignore[method-assign]
        supports_supersede=True, supports_atomic_batch=True,
        supports_custom_fields=True, supports_metadata_sort=True,
        supports_hard_delete=False, supports_owner_scoping=True)

    out = MountedGarden(model=_Tidy(), store=store).delete_record(
        ME, "m_1", requested_by="user:u1")
    assert not out.written
    assert "hard_delete" in (out.error or "")
    # 卡还在 —— 但我们**如实说了做不到**，没假装删掉。
    assert "m_1" in {c["id"] for c in store.load("t1", owner="owner-1").cards}
