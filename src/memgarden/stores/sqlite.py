"""SQLite 存储 —— 让陌生人开箱即用，数据在他自己机器上。

只用标准库 `sqlite3`，没有第三方依赖。

## ⚠️ 规模上限（有意的取舍，hx 2026-09-06 拍板保持现状）

``load`` 会把该 owner 的卡**整批读进内存**，而每轮对话都会 load 一次。
实测（每张卡约 300 字正文）：

    1000 张     5ms 每轮，峰值 1.4MB
    10000 张    40ms 每轮，峰值 14.3MB
    50000 张   270ms 每轮，峰值 73.4MB   ← 用户能感觉到回复变慢

一万张以上就该换后端 —— 实现自己的 ``StoragePort``，那个抽象就是为这件事
留的。把这个参考实现做成能扛量的，等于把那层抽象的意义抵消掉。

**打算大批做历史导入的要特别注意**：导入三年聊天记录一次就可能几千张，
几个用户下来就逼近这条线。

⚠️ 它**不做加密**。全链路按明文设计；传输和磁盘层面的安全由部署环境决定，
不在这个包里 —— 内核连密文长什么样都不知道。谁要在别处用，
自己决定要不要加密，以及在哪一层加。
"""
from __future__ import annotations

import json
import sqlite3
import re
import threading
from pathlib import Path

from ._ops import apply_ops, new_seed_mounts
from ..storage import (
    FULL_CAPABILITIES,
    ApplyResult,
    Capabilities,
    IdempotencyConflict,
    RevisionConflict,
    Snapshot,
    apply_digest,
)

#: schema 版本。**加字段/加表就要 +1**,并在 _migrate 里补上对应的升级动作 ——
#: 只改 _SCHEMA 里的 CREATE TABLE IF NOT EXISTS 对旧库一点作用都没有。
_SCHEMA_VERSION = 3

#: 这个 store 自己分配的 id 形状。宿主塞进来的 id 不长这样,也不该被计数器管。
_NUMERIC_ID = re.compile(r"m_(\d+)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    tenant   TEXT NOT NULL,
    owner    TEXT NOT NULL DEFAULT '',
    id       TEXT NOT NULL,
    doc      TEXT NOT NULL,
    PRIMARY KEY (tenant, owner, id)
);
CREATE TABLE IF NOT EXISTS revisions (
    tenant   TEXT NOT NULL,
    owner    TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, owner)
);
CREATE TABLE IF NOT EXISTS applied (
    tenant   TEXT NOT NULL,
    owner    TEXT NOT NULL DEFAULT '',
    key      TEXT NOT NULL,
    result   TEXT NOT NULL,
    digest   TEXT,
    PRIMARY KEY (tenant, owner, key)
);
-- 只增不减的 id 计数器。**不要**改回「数 cards 的行数」——
-- 删除会让计数回退、撞上已有 id，而写入是 upsert，结果是静默覆盖数据。
CREATE TABLE IF NOT EXISTS id_counters (
    tenant   TEXT NOT NULL,
    owner    TEXT NOT NULL DEFAULT '',
    next_id  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant, owner)
);
-- 整理账本。作用域 (tenant, owner, mount)。
-- 🔴 它和卡改动**在同一个事务里**提交 —— 分开写的两种坏法都很隐蔽：
--    账本先走 → 这批整理再也不会跑，改动丢了没人知道
--    卡先走   → 下次照样整理同一批，重复合并
CREATE TABLE IF NOT EXISTS maintenance_state (
    tenant          TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '',
    mount           TEXT NOT NULL,
    signature       TEXT NOT NULL DEFAULT '',
    seed_card_count INTEGER NOT NULL DEFAULT 0,
    revision        TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    schema_version  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant, owner, mount)
);
-- 原始 seed 的只增水位。hard delete 只删卡和正文，不让计数回退；否则删除
-- 一张再新增一张会被净数量抵消，Maintenance 永远看不到那次新增。
CREATE TABLE IF NOT EXISTS seed_generations (
    tenant     TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    mount      TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, owner, mount)
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS cards_by_owner ON cards(tenant, owner)")

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

        # 🔴 整个迁移是一个事务。
        # 重建表要走「建新表 → 搬数据 → DROP 旧表 → 改名」四步，中间任何一步
        # 崩掉（断电、被 kill、磁盘满）都会留下一个半迁移的库：可能旧表已经
        # DROP 了而新表还没改名 —— 数据看起来凭空消失。
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._migrate_steps(conn)
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _migrate_steps(self, conn: sqlite3.Connection) -> None:
        """实际的升级动作。由 :meth:`_migrate` 包在事务里调用。"""

        # ① 补 applied.digest。旧行的 digest 留 NULL —— 幂等检查会把 NULL
        #    当作「没记过摘要」，退回到「同键即命中」的旧行为,而不是误报冲突。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(applied)")}
        if "digest" not in cols:
            conn.execute("ALTER TABLE applied ADD COLUMN digest TEXT")

        # ② 升到 v2：owner 进主键。
        #
        # 🔴 **必须重建表，不能只 ALTER 加一列。**
        #
        # 旧表的主键是 (tenant, id)。加一列 owner 之后主键**还是** (tenant, id) ——
        # sqlite 不会因为多了一列就改主键。后果很具体：同一个 tenant 下
        # ownerA 和 ownerB 的 id 计数器各自从 1 开始，两边都会生成 m_1，
        # 而写入是 upsert →「ownerB 写自己的第一张卡」会**静默覆盖掉
        # ownerA 的第一张卡**。跨 owner 的数据互相破坏，一声不响。
        #
        # 所以老实走「建新表 → 搬数据 → 换名」这条路。
        #
        # 旧行归给谁？答案是 **owner = tenant 自己**。owner 这个概念出现之前，
        # 一个 tenant 就是一座花园，那座花园天然的所有者就是这个 tenant：
        #     · 老宿主（一租户一花园）传 owner=tenant，数据原样看得见
        #     · 新宿主开始区分多个 owner，从此互相隔离
        # 不能归给空字符串 —— 空 owner 在新代码里是被拒绝的值，
        # 那等于把旧数据变成谁都读不到的孤儿。
        #
        #    🔴 旧行归给谁？答案是 **owner = tenant 自己**。
        #
        #    owner 这个概念出现之前，一个 tenant 就是一座花园 —— 那座花园
        #    天然的所有者就是这个 tenant。这样升上来的库：
        #        · 老宿主（一租户一花园）传 owner=tenant，数据原样看得见
        #        · 新宿主开始区分多个 owner，从此互相隔离
        #    不能归给空字符串：空 owner 在新代码里是被拒绝的值，那等于把
        #    旧数据变成谁都读不到的孤儿。
        _REBUILD = {
            "cards": ("tenant, owner, id, doc",
                      "tenant, tenant, id, doc",
                      "tenant TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', "
                      "id TEXT NOT NULL, doc TEXT NOT NULL, "
                      "PRIMARY KEY (tenant, owner, id)"),
            "revisions": ("tenant, owner, revision",
                          "tenant, tenant, revision",
                          "tenant TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', "
                          "revision INTEGER NOT NULL DEFAULT 0, "
                          "PRIMARY KEY (tenant, owner)"),
            "applied": ("tenant, owner, key, result, digest",
                        "tenant, tenant, key, result, digest",
                        "tenant TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', "
                        "key TEXT NOT NULL, result TEXT NOT NULL, digest TEXT, "
                        "PRIMARY KEY (tenant, owner, key)"),
            "id_counters": ("tenant, owner, next_id",
                            "tenant, tenant, next_id",
                            "tenant TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', "
                            "next_id INTEGER NOT NULL DEFAULT 1, "
                            "PRIMARY KEY (tenant, owner)"),
        }
        for table, (cols_new, cols_from_old, ddl) in _REBUILD.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not existing or "owner" in existing:
                continue          # 新库，或者已经升过了
            # digest 是 v1 才加的列；更老的库没有它，搬数据时补 NULL。
            select = cols_from_old
            if table == "applied" and "digest" not in existing:
                select = "tenant, tenant, key, result, NULL"
            conn.execute(f"CREATE TABLE {table}__v2 ({ddl})")
            conn.execute(
                f"INSERT INTO {table}__v2 ({cols_new}) SELECT {select} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}__v2 RENAME TO {table}")

        # ③ 给每个已有卡片、但还没有计数行的 (tenant, owner) 播种计数器。
        #    从**已有 id 的最大编号**往后接,而不是从 1 开始。
        for tenant, owner in conn.execute(
            "SELECT DISTINCT tenant, owner FROM cards"
        ).fetchall():
            row = conn.execute(
                "SELECT 1 FROM id_counters WHERE tenant=? AND owner=?",
                (tenant, owner),
            ).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO id_counters(tenant, owner, next_id) VALUES(?,?,?)",
                (tenant, owner, self._seed_next_id(conn, tenant, owner)),
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS cards_by_owner ON cards(tenant, owner)")

        # ④ v3：给只增 seed 水位播种。旧库无法恢复过去已经 hard-delete 的
        # 次数，只能从仍存在的非 Dream 卡开始；升级后的新增不再被删除抵消。
        seeded: dict[tuple[str, str, str], int] = {}
        for tenant, owner, doc in conn.execute(
            "SELECT tenant, owner, doc FROM cards"
        ).fetchall():
            card = json.loads(doc)
            if str(card.get("source") or "") == "memory_dream":
                continue
            key = (tenant, owner,
                   str(card.get("mount") or "agent-private"))
            seeded[key] = seeded.get(key, 0) + 1
        for (tenant, owner, mount), generation in seeded.items():
            conn.execute(
                "INSERT OR IGNORE INTO seed_generations"
                "(tenant, owner, mount, generation) VALUES(?,?,?,?)",
                (tenant, owner, mount, generation),
            )
        # v2 可能已有比当前存量更高的整理水位。若 ledger=10、hard delete 后
        # 只剩 1 张，仅按现存卡回填为 1 会让后续新增在很长一段时间内仍算 0。
        # 迁移后的只增水位至少要等于账本已经确认处理过的 seed 数。
        for tenant, owner, mount, ledger_count in conn.execute(
            "SELECT tenant, owner, mount, seed_card_count FROM maintenance_state"
        ).fetchall():
            conn.execute(
                "INSERT INTO seed_generations"
                "(tenant, owner, mount, generation) VALUES(?,?,?,?) "
                "ON CONFLICT(tenant,owner,mount) DO UPDATE SET generation="
                "MAX(seed_generations.generation, excluded.generation)",
                (tenant, owner, mount, int(ledger_count)),
            )

    @staticmethod
    def _seed_next_id(conn: sqlite3.Connection, tenant: str, owner: str) -> int:
        """从已有卡片 id 推出「下一个安全编号」。

        只认 ``m_<数字>`` 这一种形状 —— 宿主自己塞的 id(ULID/UUID/业务号)
        本来就不由这个计数器分配,把它们算进来毫无意义。一张都认不出来时
        返回 1,这和空库是同一个状态。
        """
        biggest = 0
        for (card_id,) in conn.execute(
            "SELECT id FROM cards WHERE tenant=? AND owner=?", (tenant, owner)
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

    def load(self, tenant: str, *, owner: str, **filters) -> Snapshot:
        tenant, owner = _scope(tenant, owner)
        with self._lock, self._connect() as conn:
            cards = list(self._cards_of(conn, tenant, owner).values())
            if not filters.get("include_archived"):
                cards = [c for c in cards if not c.get("archived")]
            if not filters.get("include_superseded"):
                cards = [c for c in cards if not c.get("superseded_by")]
            generations = {
                str(mount): int(value)
                for mount, value in conn.execute(
                    "SELECT mount, generation FROM seed_generations "
                    "WHERE tenant=? AND owner=?", (tenant, owner)
                ).fetchall()
            }
            return Snapshot(cards=cards, revision=self._rev(conn, tenant, owner),
                            owner=owner, seed_generations=generations)

    def maintenance_state(self, tenant: str, *, owner: str, mount: str) -> dict:
        """上一次整理留下的账本。没有就返回空 dict。

        没有它的话，「这批整理过没有」这个判断只能靠宿主自己存 —— 而宿主
        重启一次就忘了，表现是同一批卡被反复合并。
        """
        tenant, owner = _scope(tenant, owner)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT signature, seed_card_count, revision, updated_at, "
                "schema_version FROM maintenance_state "
                "WHERE tenant=? AND owner=? AND mount=?",
                (tenant, owner, mount),
            ).fetchone()
        if not row:
            return {}
        return {"signature": row[0], "seed_card_count": int(row[1]),
                "revision": row[2], "updated_at": row[3],
                "schema_version": int(row[4]), "mount": mount}

    # -- 写 -------------------------------------------------------------- #

    def apply(
        self,
        tenant: str,
        mutations: list[dict],
        *,
        owner: str,
        idempotency_key: str,
        expected_revision: str | None = None,
        maintenance_state: dict | None = None,
    ) -> ApplyResult:
        tenant, owner = _scope(tenant, owner)
        with self._lock, self._connect() as conn:
            digest = apply_digest(mutations, maintenance_state)
            # 幂等 lookup 必须在写事务里。两个独立进程若都在 BEGIN 之前 miss，
            # 第二个最后只会撞 applied 唯一键，而不是按契约返回第一次的回执。
            conn.execute("BEGIN IMMEDIATE")
            try:
                cached = conn.execute(
                    "SELECT result, digest FROM applied "
                    "WHERE tenant=? AND owner=? AND key=?",
                    (tenant, owner, idempotency_key),
                ).fetchone()
                if cached:
                    if cached[1] and cached[1] != digest:
                        raise IdempotencyConflict(idempotency_key)
                    payload = json.loads(cached[0])
                    conn.execute("COMMIT")
                    return ApplyResult(results=payload["results"],
                                       revision=payload["revision"])
                current = self._rev(conn, tenant, owner)
                if expected_revision is not None and expected_revision != current:
                    raise RevisionConflict(expected_revision, current)

                # 🔴 七个 op 走**和 InMemoryStore 完全同一份**执行器。
                # 各写一份的后果是行为漂移，而漂移不报错：接入方在一个 store
                # 上测通、换另一个上线，某个 op 静默变成了别的语义。
                # 代价是这一批要把该 owner 的卡读进内存 —— 这个参考实现面向
                # 「开箱即用」，不面向超大库；真要扛量应当自己写适配器。
                before = self._cards_of(conn, tenant, owner)
                staged = {k: dict(v) for k, v in before.items()}
                self._reserve_supplied_ids(conn, tenant, owner, mutations)
                results = apply_ops(
                    staged, mutations,
                    new_id=lambda: self._next_id(conn, tenant, owner))

                for gone in set(before) - set(staged):
                    conn.execute(
                        "DELETE FROM cards WHERE tenant=? AND owner=? AND id=?",
                        (tenant, owner, gone))
                for card_id, card in staged.items():
                    if before.get(card_id) != card:
                        self._put(conn, tenant, owner, card)

                seed_mounts = new_seed_mounts(
                    mutations, before=before, staged=staged)
                for mount in seed_mounts:
                    conn.execute(
                        "INSERT INTO seed_generations"
                        "(tenant, owner, mount, generation) VALUES(?,?,?,1) "
                        "ON CONFLICT(tenant, owner,mount) DO UPDATE SET "
                        "generation=seed_generations.generation+1",
                        (tenant, owner, mount),
                    )

                changed = (staged != before or bool(seed_mounts)
                           or maintenance_state is not None)
                new_rev = str(int(current) + 1) if changed else current
                if changed:
                    conn.execute(
                        "INSERT INTO revisions(tenant, owner, revision) VALUES(?,?,?) "
                        "ON CONFLICT(tenant, owner) DO UPDATE SET "
                        "revision=excluded.revision",
                        (tenant, owner, int(new_rev)),
                    )
                conn.execute(
                    "INSERT INTO applied(tenant, owner, key, result, digest) "
                    "VALUES(?,?,?,?,?)",
                    (tenant, owner, idempotency_key,
                     json.dumps({"results": results, "revision": new_rev},
                                ensure_ascii=False),
                     digest),
                )
                # 🔴 账本和卡改动同一个事务。见 maintenance_state 表上的注释。
                if maintenance_state is not None:
                    self._put_ledger(conn, tenant, owner, maintenance_state, new_rev)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return ApplyResult(results=results, revision=new_rev)

    # -- 内部 ------------------------------------------------------------ #

    def _cards_of(self, conn: sqlite3.Connection, tenant: str,
                  owner: str) -> dict[str, dict]:
        rows = conn.execute(
            "SELECT id, doc FROM cards WHERE tenant=? AND owner=?",
            (tenant, owner),
        ).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def _put_ledger(self, conn: sqlite3.Connection, tenant: str, owner: str,
                    state: dict, revision: str) -> None:
        from datetime import datetime, timezone

        conn.execute(
            "INSERT INTO maintenance_state(tenant, owner, mount, signature, "
            "seed_card_count, revision, updated_at, schema_version) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant, owner, mount) DO UPDATE SET "
            "signature=excluded.signature, "
            "seed_card_count=excluded.seed_card_count, "
            "revision=excluded.revision, updated_at=excluded.updated_at, "
            "schema_version=excluded.schema_version",
            (tenant, owner, str(state.get("mount") or "agent-private"),
             str(state.get("signature") or ""),
             int(state.get("seed_card_count") or 0),
             revision,
             str(state.get("updated_at")
                 or datetime.now(timezone.utc).isoformat(timespec="seconds")),
             int(state.get("schema_version") or 1)),
        )

    def _put(self, conn: sqlite3.Connection, tenant: str, owner: str,
             card: dict) -> None:
        conn.execute(
            "INSERT INTO cards(tenant, owner, id, doc) VALUES(?,?,?,?) "
            "ON CONFLICT(tenant, owner, id) DO UPDATE SET doc=excluded.doc",
            (tenant, owner, card["id"],
             json.dumps(card, ensure_ascii=False)),
        )

    def _next_id(self, conn: sqlite3.Connection, tenant: str,
                 owner: str) -> str:
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
            "SELECT next_id FROM id_counters WHERE tenant=? AND owner=?",
            (tenant, owner),
        ).fetchone()
        # 没有计数行时**不能默认从 1 开始** —— 这个 tenant 可能已经有卡了
        # (旧库升上来、或者别处直接写过库)。从已有 id 往后接,别撞上去。
        n = int(row[0]) if row else self._seed_next_id(conn, tenant, owner)
        conn.execute(
            "INSERT INTO id_counters(tenant, owner, next_id) VALUES(?,?,?) "
            "ON CONFLICT(tenant, owner) DO UPDATE SET next_id=excluded.next_id",
            (tenant, owner, n + 1),
        )
        return f"m_{n}"

    def _reserve_supplied_ids(
        self, conn: sqlite3.Connection, tenant: str, owner: str,
        mutations: list[dict],
    ) -> None:
        highest = 0
        for mutation in mutations:
            card = mutation.get("card")
            supplied = str(card.get("id") or "") if isinstance(card, dict) else ""
            match = _NUMERIC_ID.fullmatch(supplied)
            if match:
                highest = max(highest, int(match.group(1)))
        if not highest:
            return
        row = conn.execute(
            "SELECT next_id FROM id_counters WHERE tenant=? AND owner=?",
            (tenant, owner),
        ).fetchone()
        current = int(row[0]) if row else self._seed_next_id(conn, tenant, owner)
        next_id = max(current, highest + 1)
        conn.execute(
            "INSERT INTO id_counters(tenant, owner, next_id) VALUES(?,?,?) "
            "ON CONFLICT(tenant, owner) DO UPDATE SET next_id=excluded.next_id",
            (tenant, owner, next_id),
        )

    def _rev(self, conn: sqlite3.Connection, tenant: str, owner: str) -> str:
        """版本号的作用域也是 (tenant, owner)。

        用全租户一个版本号的话，两个互不相干的 owner 会互相踢掉对方的 CAS ——
        表现是「明明没人跟我抢，我的写入却一直冲突」，很难查。
        """
        row = conn.execute(
            "SELECT revision FROM revisions WHERE tenant=? AND owner=?",
            (tenant, owner),
        ).fetchone()
        return str(row[0] if row else 0)


def _scope(tenant: str, owner: str) -> tuple[str, str]:
    """归属键。**owner 为空直接拒绝** —— 不回退成全局默认值。"""
    t = str(tenant or "").strip()
    o = str(owner or "").strip()
    if not t:
        raise ValueError("tenant is required")
    if not o:
        raise ValueError(
            "memory owner is required —— 缺稳定 owner 时必须 fail closed，"
            "不能回退成全局默认花园"
        )
    return (t, o)
