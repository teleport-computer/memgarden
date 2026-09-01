"""用**早期版本建的库**跑当前代码 —— 升级路径的回归测试。

## 为什么单独一个文件

`CREATE TABLE IF NOT EXISTS` 只管「表不存在时建表」。表已存在但少一列、
或者某张表是后来才加的，它一概不管 —— 旧库能打开、能读，**一写才出事**。
所有普通用例都在空库上跑，这条路一次都走不到。

v0.2.0 的库升上来实测有两个坑，第二个会**静默丢数据**：

    ① applied 表没有 digest 列 → 一写就 OperationalError（崩得响，不会错得静默）
    ② id_counters 是后加的表，旧库里空 → 计数从 1 开始 → 生成已存在的 m_1
       → _put 是 upsert → 覆盖掉那张旧卡，不报错，总数还不变

实测过的症状：

    升级前  {m_1: 不吃辣, m_2: 周末看医生}
    写一张  {m_1: 新加的一张, m_2: 周末看医生}   ← 「不吃辣」没了
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from memgarden.stores.sqlite import SqliteStore

# v0.2.0 的原样 schema：没有 digest 列，也没有 id_counters 表。
_V0_2_0_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    tenant   TEXT NOT NULL,
    id       TEXT NOT NULL,
    doc      TEXT NOT NULL,
    PRIMARY KEY (tenant, id)
);
CREATE TABLE IF NOT EXISTS revisions (
    tenant   TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS applied (
    tenant   TEXT NOT NULL,
    key      TEXT NOT NULL,
    result   TEXT NOT NULL,
    PRIMARY KEY (tenant, key)
);
"""


def _old_db(path, cards: dict[str, str], tenant: str = "t1") -> str:
    conn = sqlite3.connect(str(path))
    conn.executescript(_V0_2_0_SCHEMA)
    for card_id, summary in cards.items():
        conn.execute(
            "INSERT INTO cards(tenant,id,doc) VALUES(?,?,?)",
            (tenant, card_id,
             json.dumps({"id": card_id, "summary": summary}, ensure_ascii=False)),
        )
    conn.execute(
        "INSERT INTO revisions(tenant,revision) VALUES(?,?)", (tenant, len(cards))
    )
    conn.commit()
    conn.close()
    return str(path)


def test_writing_to_an_upgraded_db_does_not_overwrite_old_cards(tmp_path):
    """升级后写新卡，**一张旧卡都不能被覆盖**，总数要真的涨。

    这条盯的是实测到的静默数据丢失：断言只看 id 会漏掉它 —— id 列表看着
    没变（还是 m_1/m_2），变的是 m_1 的**内容**。所以这里比对的是内容。
    """
    db = _old_db(tmp_path / "old.db", {"m_1": "不吃辣", "m_2": "周末看医生"})

    before = {c["id"]: c["summary"] for c in SqliteStore(db).load("t1").cards}
    assert before == {"m_1": "不吃辣", "m_2": "周末看医生"}

    SqliteStore(db).apply(
        "t1", [{"op": "add", "card": {"summary": "新加的一张"}}],
        idempotency_key="k1",
    )

    after = {c["id"]: c["summary"] for c in SqliteStore(db).load("t1").cards}
    clobbered = {k: (before[k], after.get(k)) for k in before if after.get(k) != before[k]}
    assert not clobbered, f"旧卡内容被覆盖了：{clobbered}"
    assert len(after) == len(before) + 1, f"新卡没真的加进去：{after}"


def test_the_old_applied_table_gets_its_digest_column(tmp_path):
    """旧库的 applied 表少 digest 列 —— 不补的话第一次写就 OperationalError。"""
    db = _old_db(tmp_path / "old.db", {"m_1": "不吃辣"})
    SqliteStore(db)  # 构造即升级
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(applied)")}
    assert "digest" in cols


def test_idempotency_still_works_across_the_upgrade(tmp_path):
    """升级后幂等键照样生效：同键重放不重复写。

    旧行的 digest 是 NULL（旧库里根本没记过摘要）。那种情况要退回「同键即
    命中」的旧行为，而不是把 NULL 当成「摘要不一致」误报冲突。
    """
    db = _old_db(tmp_path / "old.db", {"m_1": "不吃辣"})
    mutations = [{"op": "add", "card": {"summary": "只该进去一次"}}]
    SqliteStore(db).apply("t1", mutations, idempotency_key="same-key")
    SqliteStore(db).apply("t1", mutations, idempotency_key="same-key")
    summaries = [c["summary"] for c in SqliteStore(db).load("t1").cards]
    assert summaries.count("只该进去一次") == 1, summaries


def test_host_supplied_ids_do_not_confuse_the_counter(tmp_path):
    """宿主自己塞的 id（ULID/UUID/业务号）不该被计数器算进去。

    播种只认 ``m_<数字>`` 这一种形状 —— 那是这个 store 自己分配的。把别的
    形状算进来毫无意义，还可能把计数器推到一个荒谬的数。
    """
    db = _old_db(tmp_path / "old.db", {
        "01J8XYZABCDEFGHJKMNPQRSTVW": "宿主自己给的 ULID",
        "m_7": "store 自己发的号",
    })
    SqliteStore(db).apply(
        "t1", [{"op": "add", "card": {"summary": "新的"}}], idempotency_key="k1")
    ids = {c["id"] for c in SqliteStore(db).load("t1").cards}
    assert "m_8" in ids, f"应当从 m_7 往后接，实际：{ids}"
    assert len(ids) == 3


def test_an_empty_old_db_still_starts_from_one(tmp_path):
    """空的旧库升上来仍然从 m_1 开始 —— 播种逻辑别把空库也推高。"""
    db = _old_db(tmp_path / "old.db", {})
    SqliteStore(db).apply(
        "t1", [{"op": "add", "card": {"summary": "第一张"}}], idempotency_key="k1")
    assert [c["id"] for c in SqliteStore(db).load("t1").cards] == ["m_1"]
