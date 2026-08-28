"""SQLite 存储 —— 让陌生人开箱即用，数据在他自己机器上。

只用标准库 `sqlite3`，没有第三方依赖。

⚠️ 它**不做加密**。io 的加密（信封、enclave 解密、AAD 绑定）是宿主的事，
不在这个包里 —— 内核连密文长什么样都不知道。谁要在别处用，
自己决定要不要加密，以及在哪一层加。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..storage import (
    FULL_CAPABILITIES,
    ApplyResult,
    Capabilities,
    IdempotencyConflict,
    RevisionConflict,
    Snapshot,
    mutations_digest,
)

_SCHEMA = """
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
    digest   TEXT,
    PRIMARY KEY (tenant, key)
);
-- 只增不减的 id 计数器。**不要**改回「数 cards 的行数」——
-- 删除会让计数回退、撞上已有 id，而写入是 upsert，结果是静默覆盖数据。
CREATE TABLE IF NOT EXISTS id_counters (
    tenant   TEXT PRIMARY KEY,
    next_id  INTEGER NOT NULL DEFAULT 1
);
"""


class SqliteStore:
    """单文件存储。并发写用 sqlite 自己的事务 + 一把进程内的锁。"""

    def __init__(self, path: str | Path = "memgarden.db") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- 能力声明 -------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        return FULL_CAPABILITIES

    # -- 读 -------------------------------------------------------------- #

    def load(self, tenant: str, **filters) -> Snapshot:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT doc FROM cards WHERE tenant=?", (tenant,)
            ).fetchall()
            cards = [json.loads(r[0]) for r in rows]
            if not filters.get("include_archived"):
                cards = [c for c in cards if not c.get("archived")]
            if not filters.get("include_superseded"):
                cards = [c for c in cards if not c.get("superseded_by")]
            return Snapshot(cards=cards, revision=self._rev(conn, tenant))

    # -- 写 -------------------------------------------------------------- #

    def apply(
        self,
        tenant: str,
        mutations: list[dict],
        *,
        idempotency_key: str,
        expected_revision: str | None = None,
    ) -> ApplyResult:
        with self._lock, self._connect() as conn:
            digest = mutations_digest(mutations)
            cached = conn.execute(
                "SELECT result, digest FROM applied WHERE tenant=? AND key=?",
                (tenant, idempotency_key),
            ).fetchone()
            if cached:
                # 同 key 必须同内容。不同内容不是重放，是两个不同请求撞了 key ——
                # 静默返回旧结果会让第二批改动凭空消失，而调用方以为写成功了。
                if cached[1] and cached[1] != digest:
                    raise IdempotencyConflict(idempotency_key)
                payload = json.loads(cached[0])
                return ApplyResult(results=payload["results"], revision=payload["revision"])

            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._rev(conn, tenant)
                if expected_revision is not None and expected_revision != current:
                    raise RevisionConflict(expected_revision, current)
                results = [self._one(conn, tenant, m) for m in mutations]
                new_rev = str(int(current) + 1)
                conn.execute(
                    "INSERT INTO revisions(tenant, revision) VALUES(?,?) "
                    "ON CONFLICT(tenant) DO UPDATE SET revision=excluded.revision",
                    (tenant, int(new_rev)),
                )
                conn.execute(
                    "INSERT INTO applied(tenant, key, result, digest) VALUES(?,?,?,?)",
                    (tenant, idempotency_key,
                     json.dumps({"results": results, "revision": new_rev}, ensure_ascii=False),
                     digest),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return ApplyResult(results=results, revision=new_rev)

    # -- 内部 ------------------------------------------------------------ #

    def _one(self, conn: sqlite3.Connection, tenant: str, m: dict) -> dict:
        op = str(m.get("op") or "add")
        if op == "add":
            card = dict(m.get("card") or {})
            card.setdefault("id", self._next_id(conn, tenant))
            self._put(conn, tenant, card)
            return {"id": card["id"], "status": "written"}
        if op == "supersede":
            old_id = str(m.get("target_id") or "")
            row = conn.execute(
                "SELECT doc FROM cards WHERE tenant=? AND id=?", (tenant, old_id)
            ).fetchone()
            if not row:
                raise KeyError(f"supersede target not found: {old_id}")
            new_card = dict(m.get("card") or {})
            new_card.setdefault("id", self._next_id(conn, tenant))
            old = {**json.loads(row[0]), "superseded_by": new_card["id"], "archived": True}
            self._put(conn, tenant, old)
            self._put(conn, tenant, new_card)
            return {"id": new_card["id"], "status": "superseded", "replaced": old_id}
        if op == "delete":
            target = str(m.get("target_id") or "")
            conn.execute("DELETE FROM cards WHERE tenant=? AND id=?", (tenant, target))
            return {"id": target, "status": "deleted"}
        raise ValueError(f"unknown op: {op}")

    def _put(self, conn: sqlite3.Connection, tenant: str, card: dict) -> None:
        conn.execute(
            "INSERT INTO cards(tenant, id, doc) VALUES(?,?,?) "
            "ON CONFLICT(tenant, id) DO UPDATE SET doc=excluded.doc",
            (tenant, card["id"], json.dumps(card, ensure_ascii=False)),
        )

    def _next_id(self, conn: sqlite3.Connection, tenant: str) -> str:
        """下一个卡片 id。

        ⚠️ **绝不能用「当前总数 + 1」。** 那样删掉一条之后计数会回退，
        算出来的 id 撞上已存在的卡，而 ``_put`` 是 upsert（有则覆盖）——
        结果是**静默覆盖别人的数据**：

            初始      {m_1: first, m_2: second}
            删掉 m_1  {m_2: second}
            新增      COUNT=1 → 算出 m_2 → 覆盖掉 second，不报错

        这里用一张只增不减的计数表。真实宿主更应该直接用 ULID / UUID /
        数据库序列 —— 任何**不会因删除而回退**的东西都行，别自己数数。
        """
        row = conn.execute(
            "SELECT next_id FROM id_counters WHERE tenant=?", (tenant,)
        ).fetchone()
        n = int(row[0]) if row else 1
        conn.execute(
            "INSERT INTO id_counters(tenant, next_id) VALUES(?,?) "
            "ON CONFLICT(tenant) DO UPDATE SET next_id=excluded.next_id",
            (tenant, n + 1),
        )
        return f"m_{n}"

    def _rev(self, conn: sqlite3.Connection, tenant: str) -> str:
        row = conn.execute("SELECT revision FROM revisions WHERE tenant=?", (tenant,)).fetchone()
        return str(row[0] if row else 0)
