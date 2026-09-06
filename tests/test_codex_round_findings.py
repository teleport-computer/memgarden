"""外部 review（Codex，2026-09-06）逐条核实后确认的问题，各配一条会红的测试。

没有这些测试的话，它们全是「改过一次、下次还会回来」的那类问题 ——
尤其是提权和静默覆盖，症状都不明显。
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from memgarden.mounted import MountedGarden, Scope
from memgarden.records import validate_mutations
from memgarden.schema import schemas
from memgarden.service import Service
from memgarden.storage import MutationRejected
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore

ME = Scope(tenant_id="t1", memory_owner_id="owner-1",
           allowed_mounts=("agent-private",))


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    if request.param == "memory":
        return InMemoryStore()
    return SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db"))


def _seed(store, n=3):
    store.apply("t1", [{"op": "add",
                        "card": {"id": f"m_{i}", "summary": f"第 {i} 条",
                                 "content": "正文", "mount": "agent-private"}}
                       for i in range(1, n + 1)],
                owner="owner-1", idempotency_key="seed", expected_revision=None)


def test_add_with_an_existing_id_is_rejected_not_silently_overwriting(store):
    """自带 id 的 add 撞上已有卡 → 拒绝，**不能静默覆盖**。

    写入是 upsert，撞号就是把那张旧卡抹掉：不报错、总数还不变。
    「我要新增一张」和「我要改那一张」是两个意思，后者该走 update。
    """
    _seed(store, 1)
    with pytest.raises(MutationRejected):
        store.apply("t1", [{"op": "add", "card": {"id": "m_1",
                                                  "summary": "冒名顶替"}}],
                    owner="owner-1", idempotency_key="k", expected_revision=None)
    assert [c["summary"] for c in store.load("t1", owner="owner-1").cards] == ["第 1 条"]


def test_two_adds_with_the_same_id_in_one_batch_are_rejected(store):
    """同一批里两个 add 用同一个 id —— 前一张会凭空消失。"""
    with pytest.raises(MutationRejected):
        store.apply("t1", [
            {"op": "add", "card": {"id": "dup", "summary": "第一张"}},
            {"op": "add", "card": {"id": "dup", "summary": "第二张"}},
        ], owner="owner-1", idempotency_key="k", expected_revision=None)


def test_supersede_cannot_reuse_an_existing_id_for_the_new_card(store):
    """supersede 的新卡 id 撞上 target → 拒绝。

    否则「旧卡归档并指向新卡」的链条会被新卡直接盖掉 ——
    而保住那条链正是 supersede 的全部意义。
    """
    _seed(store, 1)
    with pytest.raises(MutationRejected):
        store.apply("t1", [{"op": "supersede", "target_id": "m_1",
                            "card": {"id": "m_1", "summary": "新的",
                                     "content": "正文"}}],
                    owner="owner-1", idempotency_key="k", expected_revision=None)


def test_update_cannot_promote_a_card_to_another_mount(store):
    """🔴 提权：update 改 mount。

    ``_apply`` 只校验 mutation 顶层的 mount，而 ``changes={"mount": ...}``
    绕过那道检查直接写进卡 —— 只有 agent-private 权限的调用因此能把一张
    私密卡提升成共享的。挂载点的提升必须走宿主授权的专门路径。
    """
    _seed(store, 1)
    receipt = MountedGarden(model=None, store=store)._apply(
        ME, "agent-private",
        [{"op": "update", "record_id": "m_1",
          "changes": {"mount": "family-shared"}}],
        idempotency_key="k", trace={})
    assert not receipt.written
    assert "mount" in (receipt.error or "")
    card = store.load("t1", owner="owner-1").cards[0]
    assert card.get("mount") == "agent-private"


def test_the_legacy_target_id_spelling_really_works():
    """注释承诺兼容旧的 ``target_id``，那就必须真的能过。

    只在校验处「也认 target_id」是不够的：解析会先按 dataclass 字段名过滤，
    而 Archive/Delete 上没有这个字段 —— 值在到达校验之前就被丢掉了。
    于是「兼容」是一句空话，旧请求照样被拒。
    """
    typed = validate_mutations([{"op": "delete", "target_id": "m_1",
                                 "requested_by": "user:u1"}])
    assert typed[0].record_id == "m_1"
    assert validate_mutations([{"op": "archive", "target_id": "m_1"}])[0].record_id == "m_1"


def test_the_scope_schema_requires_what_the_service_requires():
    """schema 和运行时必须要求同一组字段。

    schema 存在的意义就是让对方**在本地**就知道自己传对没有。
    少一个必填字段，接入方本地校验通过、到服务端才失败。
    """
    scope = schemas()["Scope"]
    assert "memory_owner_id" in scope["required"]
    assert "memory_owner_id" in scope["properties"]


def test_handle_never_raises_even_on_a_malformed_params():
    """``handle`` 承诺永远返回 dict。一次坏请求不能把长驻进程带走。"""
    svc = Service(MountedGarden(model=None, store=InMemoryStore()))
    for bad in ("我是字符串", 42, ["a"], True):
        out = svc.handle({"id": "1", "method": "health.get", "params": bad})
        assert out["ok"] is False
        assert out["error"]["code"] == "invalid_request"


def test_browse_is_paginated_and_loses_nothing(store):
    """翻完所有页要不重不漏。

    游标用卡 id 而不是下标：下标游标在翻页途中有卡被删时会跳过一条，
    而那一条从此不出现在任何一页 —— 导出「成功」了，内容少一张。
    """
    _seed(store, 250)
    garden = MountedGarden(model=None, store=store)
    seen, cursor, pages = set(), "", 0
    while True:
        page = garden.browse(ME, limit=100, cursor=cursor)
        pages += 1
        seen.update(item.record_ref for item in page.items)
        if not page.next_cursor:
            break
        cursor = page.next_cursor
        assert pages < 10, "翻页停不下来"
    assert len(seen) == 250
    assert pages == 3


def test_page_size_is_capped(store):
    """limit 再大也要被夹住 —— 不然「有界」只是写在 schema 上。"""
    _seed(store, 250)
    page = MountedGarden(model=None, store=store).browse(ME, limit=99999)
    assert len(page.items) <= MountedGarden.MAX_PAGE


def test_export_honours_include_archived(store):
    """``include_archived`` 以前在服务层被丢掉了：声明了、调用方传了，实现从不读。"""
    _seed(store, 2)
    store.apply("t1", [{"op": "archive", "record_id": "m_1", "reason": "旧了"}],
                owner="owner-1", idempotency_key="a", expected_revision=None)
    svc = Service(MountedGarden(model=None, store=store))
    params = {"scope": {"tenant_id": "t1", "memory_owner_id": "owner-1"}}
    with_archived = svc.handle({"id": "1", "method": "records.export",
                                "params": {**params, "include_archived": True}})
    without = svc.handle({"id": "1", "method": "records.export",
                          "params": {**params, "include_archived": False}})
    assert with_archived["result"]["total"] > without["result"]["total"]


def test_browse_result_still_behaves_like_the_old_list(store):
    """分页是加法，不该逼所有现有调用方改代码。"""
    _seed(store, 3)
    page = MountedGarden(model=None, store=store).browse(ME)
    assert len(page) == 3
    assert len(list(page)) == 3
    assert page[0] is page.items[0]


def test_a_half_migrated_database_never_survives_a_crash(tmp_path):
    """迁移必须是一个事务。

    重建表要走「建新表 → 搬数据 → DROP 旧表 → 改名」，中间崩掉会留下一个
    半迁移的库 —— 可能旧表已经 DROP 而新表还没改名，数据看起来凭空消失。
    """
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE cards (tenant TEXT NOT NULL, id TEXT NOT NULL,
                            doc TEXT NOT NULL, PRIMARY KEY (tenant, id));
        CREATE TABLE revisions (tenant TEXT PRIMARY KEY,
                                revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE applied (tenant TEXT NOT NULL, key TEXT NOT NULL,
                              result TEXT NOT NULL, PRIMARY KEY (tenant, key));
        INSERT INTO cards VALUES ('t1', 'm_1', '{"id":"m_1","summary":"老卡"}');
    """)
    conn.commit()
    conn.close()

    # 迁移中途炸掉
    original = SqliteStore._migrate_steps

    def _boom(self, conn):
        original(self, conn)
        raise RuntimeError("升级到一半断电了")

    SqliteStore._migrate_steps = _boom
    try:
        with pytest.raises(RuntimeError):
            SqliteStore(db)
    finally:
        SqliteStore._migrate_steps = original

    # 老数据必须还在，且能被正常升级一次
    survivors = SqliteStore(db).load("t1", owner="t1").cards
    assert [c["summary"] for c in survivors] == ["老卡"]
