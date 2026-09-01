"""SQLite 存储 —— 让陌生人开箱即用，数据在他自己机器上。

只用标准库 `sqlite3`，没有第三方依赖。

⚠️ 它**不做加密**。io 的加密（信封、enclave 解密、AAD 绑定）是宿主的事，
不在这个包里 —— 内核连密文长什么样都不知道。谁要在别处用，
自己决定要不要加密，以及在哪一层加。
"""
from __future__ import annotations

import json
import sqlite3
import re
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

#: schema 版本。**加字段/加表就要 +1**,并在 _migrate 里补上对应的升级动作 ——
#: 只改 _SCHEMA 里的 CREATE TABLE IF NOT EXISTS 对旧库一点作用都没有。
_SCHEMA_VERSION = 1

#: 这个 store 自己分配的 id 形状。宿主塞进来的 id 不长这样,也不该被计数器管。
_NUMERIC_ID = re.compile(r"m_(\d+)")

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
            self._migrate(conn)

    # -- 升级旧库 -------------------------------------------------------- #

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把早期版本建的库补齐到当前 schema。

        ## 为什么 ``CREATE TABLE IF NOT EXISTS`` 不够

        它只在表**不存在**时建表。表已经存在但**少一列**、或者某张表是后来
        才加的，它一概不管 —— 于是旧库能打开、能读，一写就出问题。

        v0.2.0 的库升上来实测有两个坑，**第二个会静默丢数据**：

            ① applied 表没有 digest 列
               → 一写就 sqlite3.OperationalError: no such column: digest
               崩得很响，至少不会悄悄错

            ② id_counters 是后加的表，旧库里是空的 → 计数从 1 开始
               → 生成 m_1，而旧库里已经有 m_1 → _put 是 upsert
               → **静默覆盖掉那张旧卡，不报错，总数还不变**

               实测:
                   升级前  {m_1: 不吃辣, m_2: 周末看医生}
                   写一张  {m_1: 新加的一张, m_2: 周末看医生}   ← 「不吃辣」没了

        ②正是 :meth:`_next_id` 文档里警告的那个「计数回退撞上已有 id」——
        只是这次让计数回退的不是删除，而是**升级**。

        ## 为什么用 PRAGMA user_version 而不是自己建张表

        它是 sqlite 内建的、每个库一个整数，读写都不需要额外的表，也不会
        和用户自己的表撞名。
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= _SCHEMA_VERSION:
            return

        # ① 补 applied.digest。旧行的 digest 留 NULL —— 幂等检查会把 NULL
        #    当作「没记过摘要」，退回到「同键即命中」的旧行为,而不是误报冲突。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(applied)")}
        if "digest" not in cols:
            conn.execute("ALTER TABLE applied ADD COLUMN digest TEXT")

        # ② 给每个已有卡片、但还没有计数行的 tenant 播种计数器。
        #    从**已有 id 的最大编号**往后接,而不是从 1 开始。
        for (tenant,) in conn.execute(
            "SELECT DISTINCT tenant FROM cards WHERE tenant NOT IN "
            "(SELECT tenant FROM id_counters)"
        ).fetchall():
            conn.execute(
                "INSERT INTO id_counters(tenant, next_id) VALUES(?,?)",
                (tenant, self._seed_next_id(conn, tenant)),
            )

        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @staticmethod
    def _seed_next_id(conn: sqlite3.Connection, tenant: str) -> int:
        """从已有卡片 id 推出「下一个安全编号」。

        只认 ``m_<数字>`` 这一种形状 —— 宿主自己塞的 id(ULID/UUID/业务号)
        本来就不由这个计数器分配,把它们算进来毫无意义。一张都认不出来时
        返回 1,这和空库是同一个状态。
        """
        biggest = 0
        for (card_id,) in conn.execute(
            "SELECT id FROM cards WHERE tenant=?", (tenant,)
        ):
            m = _NUMERIC_ID.fullmatch(str(card_id))
            if m:
                biggest = max(biggest, int(m.group(1)))
        return biggest + 1

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
        # 没有计数行时**不能默认从 1 开始** —— 这个 tenant 可能已经有卡了
        # (旧库升上来、或者别处直接写过库)。从已有 id 往后接,别撞上去。
        n = int(row[0]) if row else self._seed_next_id(conn, tenant)
        conn.execute(
            "INSERT INTO id_counters(tenant, next_id) VALUES(?,?) "
            "ON CONFLICT(tenant) DO UPDATE SET next_id=excluded.next_id",
            (tenant, n + 1),
        )
        return f"m_{n}"

    def _rev(self, conn: sqlite3.Connection, tenant: str) -> str:
        row = conn.execute("SELECT revision FROM revisions WHERE tenant=?", (tenant,)).fetchone()
        return str(row[0] if row else 0)
