"""写入路径的共用验收场景 —— 任何宿主的存储接法都跑同一套。

## 为什么有这个模块

Garden 有两种接法：``MountedGarden`` + 合规 Store（内置 SqliteStore 是参考实现），
或者像 io 那样用 ``GardenComponent`` 做判断、写入走宿主自己的执行器。两条路各有
测试，但「内置 Store 通过了幂等 / 删除 / CAS」**不能**当成「宿主那条路也满足」
—— 宿主的执行器、事务边界和读路径都是自己的。这里把共同的业务语义写成**可运行的场景**，宿主实现一个
小适配器（:class:`Host`）就能在自己真实的写读路径上跑一遍。

## 场景只断言业务语义，不断言实现

- 身份用适配器分配的 id，不假设 id 形状；
- 读路径按「能不能读到」断言（fetch / index / search / recall / related / history），
  不断言排序分数、字段顺序、响应格式；
- 真相用 :meth:`Host.inspect` 读 —— 它是存储层视图，**不能**走会产生副作用的产品读路径
  （否则「读不刷新 updated_at」这类条款没法验）。

## 宿主差异：显式声明，不许静默跳过

每个场景由若干**条款**（clause id）组成，失败的条款逐条记录、不在第一条就停。
宿主有意不同的地方用 :class:`Deviation` 按条款声明（``by_design`` 或已知 ``bug``）：

    声明了、也确实失败      → deviation / bug（记录证据）
    失败了、但没声明        → fail
    声明了、但其实通过了    → fail（过期声明必须删掉，清单只许变短）

所以一份绿色报告的含义是：**除了写明理由的那几条，其余语义逐条成立**。

## 不在这里

- 不含加解密；宿主在适配器里自己封装/解封。
- 不跑模型：Capture 场景提交的是「判断已经做完」的卡，验的是写库与进度语义。
- Dream / Maintenance 账本与卡改动的原子性由 ``MountedGarden`` 自己的测试覆盖，
  宿主侧 Dream 写回留给宿主自己的测试（本模块 v1 不含 Dream 场景）。

只依赖标准库与 :mod:`memgarden.timestamps`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from .timestamps import parse_ts

#: 场景集版本。场景语义有变化（加条款、改判据）就 +1，宿主报告里带上它。
SCENARIO_VERSION = 1

#: 写操作失败时的规范错误类别。适配器把宿主自己的错误码映射进来。
ERROR_KINDS = frozenset({
    "not_found",             # 目标不存在，或不属于这个 owner（不证明存在）
    "conflict",              # 基于过期观察的写：目标已被别的写入改变
    "idempotency_conflict",  # 同一个请求身份送来了不同内容
    "invalid",               # 请求本身不合法（缺字段、超限被拒）
    "storage_failed",        # 存储执行失败，什么都没写
    "unsupported",           # 宿主不支持这个操作
})

#: 场景会检查的产品读路径。
READ_PATHS = ("fetch", "index", "search", "recall", "related", "history")

#: 规范生命周期（见 ``records.Lifecycle``；真删的卡不在任何视图里）。
STATUSES = frozenset({"active", "archived", "superseded"})


@dataclass(frozen=True)
class Outcome:
    """一次写操作的规范化回执。

    ``record_ids``：``add`` / ``supersede`` 是新卡 id；``patch`` 是**当前版本**的 id
    （就地修改时等于原 id，按取代实现时是新卡 id）；``delete`` / ``archive`` 是目标 id。
    ``reason``：成功但没写任何卡时的原因（如 Capture 判断没什么可记）。
    """

    ok: bool
    record_ids: tuple[str, ...] = ()
    error: str = ""
    reason: str = ""
    #: 宿主原始错误码，只用于报告里的证据。**不许包含卡片正文**。
    detail: str = ""


@runtime_checkable
class Host(Protocol):
    """宿主适配器要实现的全部接口。每个场景拿一个**全新**的宿主实例（空花园）。

    ``owner`` 是场景里的逻辑名（``"alice"`` / ``"bob"``），适配器负责映射成宿主的
    可信身份（tenant / memory owner / 用户 id）。
    """

    #: 宿主报告里的名字。
    name: str
    #: 两个宿主接受的 ``source`` 取值（有些宿主是封闭枚举）。场景只用这两个。
    sources: tuple[str, str]

    # -- 写 -------------------------------------------------------------- #
    def add(self, owner: str, card: Mapping[str, Any], *, request_id: str = "",
            record_id: str = "") -> Outcome:
        """新增一张卡。``request_id`` 是请求身份（重放不重复写）；``record_id``
        是调用方自带的 id（可信恢复/导入），宿主不支持自带 id 时返回 unsupported。"""
        ...

    def patch(self, owner: str, record_id: str, changes: Mapping[str, Any], *,
              based_on: Any = None) -> Outcome:
        """修改一张卡的内容字段（summary / content）。``based_on`` 来自 :meth:`observe`。"""
        ...

    def supersede(self, owner: str, target_ids: Sequence[str], card: Mapping[str, Any], *,
                  based_on: Any = None) -> Outcome:
        """一张新卡取代一张或多张旧卡。"""
        ...

    def archive(self, owner: str, record_id: str, *, reason: str = "") -> Outcome:
        """收起一张卡：不再参与召回，内容还在、历史里可见。"""
        ...

    def delete(self, owner: str, record_id: str, *, requested_by: str) -> Outcome:
        """用户要求的真删。"""
        ...

    def observe(self, owner: str) -> Any:
        """拿一个并发控制凭据（revision 等）。宿主不用凭据做冲突检测时返回 ``None``。

        返回 ``None`` 是允许的，但下面这几条条款**要求**基于凭据的冲突检测，这样的宿主
        会在这几条上失败，需要按条款声明 :class:`Deviation`（通常 ``by_design``）：
        ``supersede.concurrent_same_target/second_conflict``、
        ``supersede.concurrent_same_target/chain_points_to_winner``、
        ``supersede.concurrent_same_target/one_active_successor``、
        ``conflict.stale_patch/stale_edit_refused``、
        ``conflict.stale_patch/first_edit_survives``。
        拒绝时回 ``conflict`` 或 ``not_found``（目标已不是当前卡）都算拒绝，与
        ``conflict.supersede_after_delete`` 一致；改内容的旧凭据只认 ``conflict``（目标还在）。
        """
        ...

    def tick(self) -> None:
        """让宿主的写入时钟前进（至少 1 秒）。"""
        ...

    # -- Capture：判断已经做完，提交结果 --------------------------------- #
    def commit_capture(self, owner: str, cards: Sequence[Mapping[str, Any]], *,
                       request_id: str, fail_storage: bool = False) -> Outcome:
        """提交一批已判断好的新卡，并按宿主规则推进「素材已处理」进度。

        ``fail_storage=True``：让这一次提交在存储执行阶段失败（真实事务里抛错）。
        ``cards`` 为空 = 判断结论是没什么可记。
        """
        ...

    def capture_progress(self, owner: str) -> Any:
        """「这段素材算处理过了没有」的进度值。只比较相等/不等。"""
        ...

    # -- 读 -------------------------------------------------------------- #
    def inspect(self, owner: str, record_id: str) -> dict | None:
        """存储层真相（不经产品读路径、不产生副作用）。真删的卡返回 ``None``。

        视图字段：``id summary content bucket threads source occurred_at created_at
        updated_at status superseded_by``；``status`` ∈ :data:`STATUSES`。
        """
        ...

    def fetch(self, owner: str, ids: Sequence[str], *, include_history: bool = False) -> list[dict]:
        """按 id 取卡（产品读路径）。返回能读到的卡视图（至少含 ``id``、``content``）。"""
        ...

    def index(self, owner: str) -> list[dict]:
        """当前有效卡列表（产品读路径）。"""
        ...

    def search(self, owner: str, query: str) -> list[str]:
        """主动搜索（产品读路径），按宿主顺序返回 id。"""
        ...

    def recall(self, owner: str, query: str) -> list[str]:
        """自动上下文召回（产品读路径）。"""
        ...

    def related(self, owner: str, ids: Sequence[str]) -> list[str]:
        """关联读取（产品读路径）。"""
        ...

    def history(self, owner: str) -> list[dict]:
        """含归档 / 被取代的历史视图（产品读路径）。"""
        ...


@dataclass(frozen=True)
class Deviation:
    """宿主对某一条款的显式差异声明。"""

    kind: Literal["by_design", "bug"]
    reason: str


@dataclass(frozen=True)
class ClauseFailure:
    clause: str
    evidence: str


class _Stop(Exception):
    """致命条款失败：后续步骤没有意义，停止这个场景。"""


class Checks:
    """场景里用的断言收集器。条款失败不立即停止，除非 ``fatal``。"""

    def __init__(self) -> None:
        self.failures: list[ClauseFailure] = []
        #: 真正判过的条款（通过或失败）。致命失败或适配器抛错之后的条款没跑过。
        self.ran: set[str] = set()

    def check(self, ok: bool, clause: str, evidence: Any = "", *, fatal: bool = False) -> bool:
        self.ran.add(clause)
        if not ok:
            self.failures.append(ClauseFailure(clause, _short(evidence)))
            if fatal:
                raise _Stop(clause)
        return bool(ok)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    #: 这个场景保证的业务语义（给人读的一句话）。
    guarantees: str
    run: Callable[[Host, Checks], None] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Result:
    scenario: str
    #: pass / deviation / bug / fail
    status: str
    failures: tuple[ClauseFailure, ...] = ()
    #: 未声明的失败 + 过期的声明，status == fail 时非空。
    problems: tuple[str, ...] = ()
    deviations: tuple[tuple[str, Deviation], ...] = ()


# --------------------------------------------------------------------------- #
# 合成素材
# --------------------------------------------------------------------------- #

def card(token: str, *, source: str, occurred_at: str = "2026-03-01T08:00:00Z",
         thread: str = "kit-thread", **extra: Any) -> dict:
    """一张合成卡。``token`` 同时出现在 summary 和 content 里，用来搜和查泄漏。"""
    return {
        "summary": f"{token} summary line",
        "content": f"{token} body: a synthetic conformance memory about {token}.",
        "bucket": "Conformance",
        "threads": [thread],
        "occurred_at": occurred_at,
        "source": source,
        **extra,
    }


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


def _ids(views: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(v.get("id") or "") for v in views]


def _instant(value: Any):
    return parse_ts(value)


def _explicit_offset(value: Any) -> bool:
    text = str(value or "").strip()
    if text.endswith("Z"):
        return True
    return len(text) > 19 and ("+" in text[19:] or "-" in text[19:])


def _all_reads(host: Host, owner: str, *, ids: Sequence[str], query: str,
               related_from: Sequence[str] = ()) -> dict[str, list[str]]:
    """把六条产品读路径都跑一遍，返回每条路读到的 id。"""
    return {
        "fetch": _ids(host.fetch(owner, list(ids))),
        "fetch_history": _ids(host.fetch(owner, list(ids), include_history=True)),
        "index": _ids(host.index(owner)),
        "search": list(host.search(owner, query)),
        "recall": list(host.recall(owner, query)),
        "related": list(host.related(owner, list(related_from))) if related_from else [],
        "history": _ids(host.history(owner)),
    }


def _time_fields(view: Mapping[str, Any] | None) -> tuple:
    view = view or {}
    return tuple(str(view.get(k) or "") for k in ("created_at", "updated_at", "occurred_at"))


def _added(host: Host, checks: Checks, owner: str, data: Mapping[str, Any], clause: str,
           **kw: Any) -> str:
    out = host.add(owner, data, **kw)
    checks.check(out.ok and len(out.record_ids) == 1, clause, out, fatal=True)
    return out.record_ids[0]


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #

def _add_roundtrip(host: Host, c: Checks) -> None:
    src = host.sources[0]
    data = card("kitaddone", source=src, occurred_at="2025-11-02T09:30:00Z")
    rid = _added(host, c, "alice", data, "add.roundtrip/ok")
    view = host.inspect("alice", rid)
    c.check(view is not None, "add.roundtrip/persisted", rid, fatal=True)
    for key in ("summary", "content", "bucket", "source"):
        c.check(view.get(key) == data[key], f"add.roundtrip/{key}",
                {"wrote": data[key], "stored": view.get(key)})
    c.check(list(view.get("threads") or []) == data["threads"], "add.roundtrip/threads",
            view.get("threads"))
    c.check(_instant(view.get("occurred_at")) == _instant(data["occurred_at"]),
            "add.roundtrip/occurred_at", view.get("occurred_at"))
    c.check(view.get("status") == "active", "add.roundtrip/status", view.get("status"))
    fetched = host.fetch("alice", [rid])
    c.check(_ids(fetched) == [rid] and fetched[0].get("content") == data["content"],
            "add.roundtrip/fetch", _ids(fetched))
    c.check(rid in _ids(host.index("alice")), "add.roundtrip/index")
    c.check(rid in host.search("alice", "kitaddone"), "add.roundtrip/search")


def _add_write_times(host: Host, c: Checks) -> None:
    rid = _added(host, c, "alice", card("kitclock", source=host.sources[0]), "add.write_times/ok")
    view = host.inspect("alice", rid) or {}
    created, updated = view.get("created_at"), view.get("updated_at")
    c.check(_instant(created) is not None, "add.write_times/created_at_present", created, fatal=True)
    c.check(_instant(updated) is not None, "add.write_times/updated_at_present", updated, fatal=True)
    c.check(_explicit_offset(created), "add.write_times/created_at_explicit_utc", created)
    c.check(_explicit_offset(updated), "add.write_times/updated_at_explicit_utc", updated)
    c.check(abs(_instant(updated) - _instant(created)) < timedelta(seconds=2),
            "add.write_times/same_write_instant", {"created_at": created, "updated_at": updated})


def _read_side_effects(host: Host, c: Checks) -> None:
    src = host.sources[0]
    a = _added(host, c, "alice", card("kitquiet", source=src), "reads.no_side_effects/add_a")
    b = _added(host, c, "alice", card("kitquietb", source=src), "reads.no_side_effects/add_b")
    before = {rid: _time_fields(host.inspect("alice", rid)) for rid in (a, b)}
    host.tick()
    _all_reads(host, "alice", ids=[a, b], query="kitquiet", related_from=[a])
    after = {rid: _time_fields(host.inspect("alice", rid)) for rid in (a, b)}
    c.check(after == before, "reads.no_side_effects/no_timestamp_change",
            {"before": before, "after": after})


def _patch_current_version(host: Host, c: Checks) -> None:
    src = host.sources[0]
    rid = _added(host, c, "alice", card("kitpatch", source=src), "patch.current_version/add")
    host.tick()
    out = host.patch("alice", rid, {"summary": "kitpatch summary corrected",
                                    "content": "kitpatch body: corrected spelling."})
    c.check(out.ok and len(out.record_ids) == 1, "patch.current_version/ok", out, fatal=True)
    current = out.record_ids[0]
    view = host.inspect("alice", current) or {}
    c.check(view.get("content") == "kitpatch body: corrected spelling.",
            "patch.current_version/new_content", view.get("content"))
    c.check(view.get("status") == "active", "patch.current_version/active", view.get("status"))
    index = _ids(host.index("alice"))
    c.check(index.count(current) == 1 and (current == rid or rid not in index),
            "patch.current_version/single_current_version", index)
    hits = host.search("alice", "corrected")
    c.check(current in hits, "patch.current_version/searchable", hits)


def _patch_in_place(host: Host, c: Checks) -> None:
    src = host.sources[0]
    rid = _added(host, c, "alice", card("kitinplace", source=src), "patch.in_place/add")
    before = host.inspect("alice", rid) or {}
    host.tick()
    out = host.patch("alice", rid, {"content": "kitinplace body: fixed typo."})
    c.check(out.ok, "patch.in_place/ok", out, fatal=True)
    c.check(tuple(out.record_ids) == (rid,), "patch.in_place/same_id",
            {"patched": rid, "current": out.record_ids})
    after = host.inspect("alice", rid) or {}
    c.check(after.get("created_at") == before.get("created_at"),
            "patch.in_place/created_at_kept", (before.get("created_at"), after.get("created_at")))
    c.check((_instant(after.get("updated_at")) or _instant("1970-01-01"))
            > (_instant(before.get("updated_at")) or _instant("1970-01-01")),
            "patch.in_place/updated_at_advanced",
            (before.get("updated_at"), after.get("updated_at")))


def _patch_provenance(host: Host, c: Checks) -> None:
    """修正内容不改变「这件事什么时候发生、从哪来、归哪个桶」。"""
    src = host.sources[1]
    data = card("kitprov", source=src, occurred_at="2024-06-15T12:00:00Z", thread="prov-thread")
    rid = _added(host, c, "alice", data, "patch.preserves_provenance/add")
    host.tick()
    out = host.patch("alice", rid, {"content": "kitprov body: clarified wording."})
    c.check(out.ok and out.record_ids, "patch.preserves_provenance/ok", out, fatal=True)
    view = host.inspect("alice", out.record_ids[0]) or {}
    c.check(_instant(view.get("occurred_at")) == _instant(data["occurred_at"]),
            "patch.preserves_provenance/occurred_at", {"wrote": data["occurred_at"],
                                             "after_patch": view.get("occurred_at")})
    c.check(view.get("source") == src, "patch.preserves_provenance/source",
            {"wrote": src, "after_patch": view.get("source")})
    c.check(view.get("bucket") == data["bucket"], "patch.preserves_provenance/bucket", view.get("bucket"))
    c.check(list(view.get("threads") or []) == data["threads"], "patch.preserves_provenance/threads",
            view.get("threads"))


def _supersede_links(host: Host, c: Checks) -> None:
    src = host.sources[0]
    a = _added(host, c, "alice", card("kitolda", source=src), "supersede.links_history/add_a")
    b = _added(host, c, "alice", card("kitoldb", source=src), "supersede.links_history/add_b")
    before_a = host.inspect("alice", a) or {}
    host.tick()
    out = host.supersede("alice", [a, b], card("kitmerged", source=src))
    c.check(out.ok and len(out.record_ids) == 1, "supersede.links_history/ok", out, fatal=True)
    new = out.record_ids[0]
    for old in (a, b):
        view = host.inspect("alice", old) or {}
        c.check(view.get("status") == "superseded", f"supersede.links_history/status",
                view.get("status"))
        c.check(view.get("superseded_by") == new, "supersede.links_history/superseded_by",
                view.get("superseded_by"))
        c.check(bool(view.get("content")), "supersede.links_history/history_content_kept")
    after_a = host.inspect("alice", a) or {}
    c.check(after_a.get("created_at") == before_a.get("created_at"),
            "supersede.links_history/created_at_kept")
    c.check((_instant(after_a.get("updated_at")) or _instant("1970-01-01"))
            > (_instant(before_a.get("updated_at")) or _instant("1970-01-01")),
            "supersede.links_history/updated_at_advanced",
            (before_a.get("updated_at"), after_a.get("updated_at")))
    c.check((host.inspect("alice", new) or {}).get("status") == "active",
            "supersede.links_history/successor_active")
    reads = _all_reads(host, "alice", ids=[a, b, new], query="kitolda")
    c.check(a not in reads["index"] and b not in reads["index"] and new in reads["index"],
            "supersede.links_history/index_current_only", reads["index"])
    c.check(a not in reads["search"], "supersede.links_history/search_excludes_retired", reads["search"])
    c.check(a not in reads["recall"], "supersede.links_history/recall_excludes_retired", reads["recall"])
    c.check(a not in reads["fetch"], "supersede.links_history/fetch_default_current", reads["fetch"])
    c.check({a, b} <= set(reads["history"]) and {a, b} <= set(reads["fetch_history"]),
            "supersede.links_history/history_visible", reads)


def _supersede_concurrent(host: Host, c: Checks) -> None:
    src = host.sources[0]
    t = _added(host, c, "alice", card("kitrace", source=src), "supersede.concurrent_same_target/add")
    seen = host.observe("alice")
    first = host.supersede("alice", [t], card("kitracewin", source=src), based_on=seen)
    second = host.supersede("alice", [t], card("kitracelose", source=src), based_on=seen)
    c.check(first.ok, "supersede.concurrent_same_target/first_ok", first, fatal=True)
    # 第二次取代的目标已经被取代：回 conflict（凭据过期）或 not_found（目标已不是当前卡）
    # 都是拒绝，和 supersede_after_delete 同一个口径。
    c.check(not second.ok and second.error in {"conflict", "not_found"},
            "supersede.concurrent_same_target/second_conflict", second)
    view = host.inspect("alice", t) or {}
    c.check(view.get("superseded_by") == first.record_ids[0],
            "supersede.concurrent_same_target/chain_points_to_winner", view.get("superseded_by"))
    index = host.index("alice")
    successors = [v for v in index if "kitrace" in str(v.get("summary") or "")]
    c.check(_ids(successors) == list(first.record_ids), "supersede.concurrent_same_target/one_active_successor",
            _ids(successors))


def _archive_retires(host: Host, c: Checks) -> None:
    src = host.sources[0]
    a = _added(host, c, "alice", card("kitshelf", source=src, thread="shelf"), "archive.retires/add")
    neighbour = _added(host, c, "alice", card("kitneighbour", source=src, thread="shelf"),
                       "archive.retires/add_neighbour")
    before = host.inspect("alice", a) or {}
    host.tick()
    out = host.archive("alice", a, reason="kit")
    c.check(out.ok, "archive.retires/ok", out, fatal=True)
    view = host.inspect("alice", a) or {}
    c.check(view.get("status") == "archived", "archive.retires/status", view.get("status"))
    c.check(view.get("content") == before.get("content"), "archive.retires/content_kept")
    c.check(view.get("created_at") == before.get("created_at"), "archive.retires/created_at_kept")
    c.check((_instant(view.get("updated_at")) or _instant("1970-01-01"))
            > (_instant(before.get("updated_at")) or _instant("1970-01-01")),
            "archive.retires/updated_at_advanced", (before.get("updated_at"), view.get("updated_at")))
    reads = _all_reads(host, "alice", ids=[a], query="kitshelf", related_from=[neighbour])
    for path in ("fetch", "index", "search", "recall", "related"):
        c.check(a not in reads[path], f"archive.retires/{path}_excludes", reads[path])
    c.check(a in reads["history"] and a in reads["fetch_history"], "archive.retires/history_visible",
            reads)


def _delete_hard(host: Host, c: Checks) -> None:
    src = host.sources[0]
    gone = _added(host, c, "alice", card("kitforget", source=src, thread="forget"),
                  "delete.hard/add")
    sibling = _added(host, c, "alice", card("kitsibling", source=src, thread="forget"),
                     "delete.hard/add_sibling")
    c.check(gone in host.related("alice", [sibling]), "delete.hard/precondition_related",
            host.related("alice", [sibling]), fatal=True)
    out = host.delete("alice", gone, requested_by="user")
    c.check(out.ok, "delete.hard/ok", out, fatal=True)
    c.check(host.inspect("alice", gone) is None, "delete.hard/storage_gone",
            host.inspect("alice", gone))
    reads = _all_reads(host, "alice", ids=[gone], query="kitforget", related_from=[sibling])
    for path, ids in reads.items():
        c.check(gone not in ids, f"delete.hard/{path}_excludes", ids)
    blob = repr([host.history("alice"), host.index("alice"),
                 host.fetch("alice", [sibling], include_history=True)])
    c.check("kitforget" not in blob, "delete.hard/content_unreadable")
    again = host.delete("alice", gone, requested_by="user")
    c.check(again.ok or again.error == "not_found", "delete.hard/repeat_is_safe", again)
    c.check(host.inspect("alice", gone) is None, "delete.hard/repeat_does_not_resurrect")
    fresh = _added(host, c, "alice", card("kitafter", source=src), "delete.hard/add_after")
    c.check(fresh != gone, "delete.hard/id_not_reused", fresh)


def _delete_after_supersede(host: Host, c: Checks) -> None:
    src = host.sources[0]
    old = _added(host, c, "alice", card("kitprior", source=src), "delete.after_supersede/add")
    out = host.supersede("alice", [old], card("kitsuccessor", source=src))
    c.check(out.ok, "delete.after_supersede/supersede_ok", out, fatal=True)
    new = out.record_ids[0]
    c.check(old in host.related("alice", [new]), "delete.after_supersede/precondition_related",
            host.related("alice", [new]))
    gone = host.delete("alice", old, requested_by="user")
    c.check(gone.ok, "delete.after_supersede/user_delete_wins", gone, fatal=True)
    c.check(host.inspect("alice", old) is None, "delete.after_supersede/storage_gone")
    c.check((host.inspect("alice", new) or {}).get("status") == "active",
            "delete.after_supersede/successor_still_active")
    c.check(old not in host.related("alice", [new]), "delete.after_supersede/related_excludes",
            host.related("alice", [new]))
    c.check(old not in _ids(host.history("alice")), "delete.after_supersede/history_excludes")


def _supersede_after_delete(host: Host, c: Checks) -> None:
    src = host.sources[0]
    t = _added(host, c, "alice", card("kitvanish", source=src), "conflict.supersede_after_delete/add")
    seen = host.observe("alice")
    c.check(host.delete("alice", t, requested_by="user").ok, "conflict.supersede_after_delete/delete_ok",
            fatal=True)
    out = host.supersede("alice", [t], card("kitghost", source=src), based_on=seen)
    c.check(not out.ok and out.error in {"not_found", "conflict"},
            "conflict.supersede_after_delete/supersede_refused", out)
    c.check("kitghost" not in repr(host.index("alice")), "conflict.supersede_after_delete/no_successor_written")
    c.check(host.inspect("alice", t) is None, "conflict.supersede_after_delete/not_resurrected")


def _stale_patch(host: Host, c: Checks) -> None:
    src = host.sources[0]
    rid = _added(host, c, "alice", card("kitstale", source=src), "conflict.stale_patch/add")
    seen = host.observe("alice")
    fresh = host.patch("alice", rid, {"content": "kitstale body: first edit."})
    c.check(fresh.ok, "conflict.stale_patch/first_edit_ok", fresh, fatal=True)
    late = host.patch("alice", rid, {"content": "kitstale body: stale edit."}, based_on=seen)
    c.check(not late.ok and late.error == "conflict", "conflict.stale_patch/stale_edit_refused", late)
    current = [v for v in host.index("alice") if "kitstale" in str(v.get("summary") or "")]
    c.check(len(current) == 1, "conflict.stale_patch/one_current", _ids(current), fatal=True)
    view = host.inspect("alice", str(current[0].get("id"))) or {}
    c.check(view.get("content") == "kitstale body: first edit.",
            "conflict.stale_patch/first_edit_survives", view.get("content"))


def _owner_isolation(host: Host, c: Checks) -> None:
    """id 的作用域由宿主决定（可以按 owner 分区，所以 bob 可能有同名 id 的自己的卡）。
    因此读侧按**内容**断言：bob 的任何读路径都不出现 alice 那张卡的正文。"""
    src = host.sources[0]
    rid = _added(host, c, "alice", card("kitprivate", source=src, thread="private"),
                 "owner.isolation/add")
    before = host.inspect("alice", rid)
    writes = {
        "patch": host.patch("bob", rid, {"content": "kithijack body: overwritten."}),
        "supersede": host.supersede("bob", [rid], card("kithijack", source=src)),
        "archive": host.archive("bob", rid),
        "delete": host.delete("bob", rid, requested_by="bob"),
    }
    for op, out in writes.items():
        c.check(not out.ok and out.error == "not_found", f"owner.isolation/{op}_refused", out)
    c.check(host.inspect("alice", rid) == before, "owner.isolation/card_untouched",
            host.inspect("alice", rid))
    c.check("kithijack" not in repr(host.history("alice")) + repr(host.history("bob")),
            "owner.isolation/no_foreign_successor")
    # bob 有自己的一张同线程卡，关联读取才有东西可扩展。
    own = _added(host, c, "bob", card("kitbobown", source=src, thread="private"),
                 "owner.isolation/add_bob")
    bob_views = (host.fetch("bob", [rid, own], include_history=True) + host.index("bob")
                 + host.history("bob"))
    c.check(all("kitprivate" not in repr(v) for v in bob_views), "owner.isolation/views_exclude",
            _ids(bob_views))
    for path, ids in (("search", host.search("bob", "kitprivate")),
                      ("recall", host.recall("bob", "kitprivate")),
                      ("related", host.related("bob", [own]))):
        leaked = [i for i in ids if "kitprivate" in repr(host.inspect("bob", i))
                  or host.inspect("bob", i) is None]
        c.check(not leaked, f"owner.isolation/{path}_excludes", ids)
    c.check("kitprivate" not in repr(host.inspect("bob", rid)), "owner.isolation/inspect_scoped")


def _idempotent_replay(host: Host, c: Checks) -> None:
    src = host.sources[0]
    data = card("kitreplay", source=src)
    first = host.add("alice", data, request_id="req-kit-1")
    c.check(first.ok and len(first.record_ids) == 1, "idempotency.replay/first_ok", first, fatal=True)
    before = _time_fields(host.inspect("alice", first.record_ids[0]))
    host.tick()
    again = host.add("alice", data, request_id="req-kit-1")
    c.check(again.ok and tuple(again.record_ids) == tuple(first.record_ids),
            "idempotency.replay/same_receipt", {"first": first, "again": again})
    copies = [v for v in host.history("alice") if "kitreplay" in str(v.get("summary") or "")]
    c.check(len(copies) == 1, "idempotency.replay/single_record", _ids(copies))
    c.check(_time_fields(host.inspect("alice", first.record_ids[0])) == before,
            "idempotency.replay/no_rewrite", before)


def _idempotency_key_reuse(host: Host, c: Checks) -> None:
    src = host.sources[0]
    first = host.add("alice", card("kitkeyone", source=src), request_id="req-kit-2")
    c.check(first.ok, "idempotency.key_reuse/first_ok", first, fatal=True)
    other = host.add("alice", card("kitkeytwo", source=src), request_id="req-kit-2")
    c.check(not other.ok and other.error == "idempotency_conflict", "idempotency.key_reuse/conflict",
            other)
    history = repr(host.history("alice"))
    c.check("kitkeytwo" not in history, "idempotency.key_reuse/second_not_written")
    c.check("kitkeyone" in history, "idempotency.key_reuse/first_kept")


def _supplied_id_never_overwrites(host: Host, c: Checks) -> None:
    src = host.sources[0]
    rid = _added(host, c, "alice", card("kitoriginal", source=src), "add.supplied_id_never_overwrites/add")
    retired = host.supersede("alice", [rid], card("kitnewer", source=src))
    c.check(retired.ok, "add.supplied_id_never_overwrites/supersede_ok", retired, fatal=True)
    before = host.inspect("alice", rid)
    out = host.add("alice", card("kitclobber", source=src), record_id=rid)
    c.check(not out.ok and out.error in {"conflict", "invalid", "unsupported"},
            "add.supplied_id_never_overwrites/refused", out)
    c.check(host.inspect("alice", rid) == before, "add.supplied_id_never_overwrites/existing_untouched",
            {"before": before, "after": host.inspect("alice", rid)})


def _occurred_at_ordering(host: Host, c: Checks) -> None:
    """发生时间决定顺序，写入顺序不决定。日期-only 与带时区的值按同一时间轴比较。"""
    src = host.sources[0]
    written = [("2025-01-10T00:00:00Z", "mid"), ("2026-02-01T00:00:00+08:00", "new"),
               ("2024-05-01", "old")]
    ids: dict[str, str] = {}
    for occurred, label in written:
        data = card("kitchrono", source=src, occurred_at=occurred)
        data["summary"] = "kitchrono summary line"
        data["content"] = "kitchrono body: identical text so only time breaks the tie."
        ids[label] = _added(host, c, "alice", data, f"order.occurred_at/add_{label}")
    for label, occurred in (("new", written[1][0]), ("old", written[2][0])):
        c.check(_instant((host.inspect("alice", ids[label]) or {}).get("occurred_at"))
                == _instant(occurred), f"order.occurred_at/{label}_occurred_at_instant")
    hits = [h for h in host.search("alice", "kitchrono") if h in ids.values()]
    c.check(hits == [ids["new"], ids["mid"], ids["old"]], "order.occurred_at/search_tie_break_newest_first",
            {"got": hits, "expected": [ids["new"], ids["mid"], ids["old"]]})


def _error_receipts(host: Host, c: Checks) -> None:
    src = host.sources[0]
    missing = host.patch("alice", "kit-no-such-record", {"content": "x"})
    c.check(not missing.ok and missing.error == "not_found", "receipt.errors/patch_missing", missing)
    gone = host.delete("alice", "kit-no-such-record", requested_by="user")
    c.check(not gone.ok and gone.error == "not_found", "receipt.errors/delete_missing", gone)
    bad = host.add("alice", {"summary": "", "content": "", "bucket": "kitsecretbucket",
                             "threads": [], "source": src, "occurred_at": ""})
    c.check(not bad.ok and bad.error == "invalid", "receipt.errors/empty_card_invalid", bad)
    c.check("kitsecretbucket" not in repr(host.history("alice")), "receipt.errors/nothing_written")
    for out in (missing, gone, bad):
        c.check(out.error in ERROR_KINDS or out.ok, "receipt.errors/canonical_error", out)
    leaky = host.patch("alice", "kit-no-such-record", {"content": "kitleaksecret text"})
    c.check("kitleaksecret" not in repr(leaky), "receipt.errors/no_content_echo", leaky)


def _content_length(host: Host, c: Checks) -> None:
    """长正文要么完整保存，要么明确拒绝；不许成功回执配一张被截短的卡。"""
    src = host.sources[0]
    body = "kitlong " + ("abcdefghij" * 600)  # 6008 字
    data = card("kitlong", source=src)
    data["content"] = body
    out = host.add("alice", data)
    if not out.ok:
        c.check(out.error == "invalid", "content.length/rejected_explicitly", out)
        return
    view = host.inspect("alice", out.record_ids[0]) or {}
    c.check(view.get("content") == body, "content.length/stored_whole",
            {"wrote_chars": len(body), "stored_chars": len(str(view.get("content") or ""))})


def _capture_failure(host: Host, c: Checks) -> None:
    src = host.sources[0]
    batch = [card("kitcapone", source=src), card("kitcaptwo", source=src)]
    start = host.capture_progress("alice")
    failed = host.commit_capture("alice", batch, request_id="cap-kit-1", fail_storage=True)
    c.check(not failed.ok and failed.error == "storage_failed", "capture.write_failure/receipt_failed",
            failed)
    c.check(failed.reason != "nothing_to_keep", "capture.write_failure/not_reported_as_empty", failed)
    c.check(host.capture_progress("alice") == start, "capture.write_failure/progress_not_advanced",
            (start, host.capture_progress("alice")))
    c.check("kitcapone" not in repr(host.history("alice")), "capture.write_failure/nothing_partial")
    retry = host.commit_capture("alice", batch, request_id="cap-kit-1")
    c.check(retry.ok and len(retry.record_ids) == 2, "capture.write_failure/retry_writes", retry,
            fatal=True)
    c.check(host.capture_progress("alice") != start, "capture.write_failure/retry_advances")
    after_retry = host.capture_progress("alice")
    # 重放已提交的请求：可以回同一张回执，也可以因为进度已经走过而报 conflict ——
    # 这里要的是「不重复写、进度不动」，不规定回执形状。
    replay = host.commit_capture("alice", batch, request_id="cap-kit-1")
    c.check(replay.ok or replay.error == "conflict", "capture.write_failure/replay_ok", replay)
    copies = [v for v in host.history("alice") if "kitcapone" in str(v.get("summary") or "")]
    c.check(len(copies) == 1, "capture.write_failure/replay_single_record", _ids(copies))
    c.check(host.capture_progress("alice") == after_retry, "capture.write_failure/replay_progress_stable")


def _capture_nothing(host: Host, c: Checks) -> None:
    start = host.capture_progress("alice")
    out = host.commit_capture("alice", [], request_id="cap-kit-empty")
    c.check(out.ok and not out.record_ids, "capture.nothing_to_keep/ok_without_records", out)
    c.check(out.error == "", "capture.nothing_to_keep/not_an_error", out)
    c.check(host.capture_progress("alice") != start, "capture.nothing_to_keep/progress_advanced")


def _scenario(sid: str, title: str, guarantees: str, fn) -> Scenario:
    return Scenario(id=sid, title=title, guarantees=guarantees, run=fn)


#: 全部场景。宿主报告按这个顺序出。
SCENARIOS: tuple[Scenario, ...] = (
    _scenario("add.roundtrip", "新增后字段原样可读",
              "新增返回一个 id；存储层与 fetch/index/search 读回写入的正文、桶、线索、来源和发生时间",
              _add_roundtrip, ),
    _scenario("add.write_times", "写入时间由存储记录",
              "新卡有 created_at/updated_at，二者是带时区的同一写入时刻",
              _add_write_times, ),
    _scenario("reads.no_side_effects", "读不刷新时间",
              "fetch/index/search/recall/related/history 不改变 created_at/updated_at/occurred_at",
              _read_side_effects, ),
    _scenario("patch.current_version", "修改后只有一个当前版本",
              "修改后当前版本带新内容、可搜到，index 里只有这一个版本",
              _patch_current_version, ),
    _scenario("patch.in_place", "修改就地生效",
              "修改保留 id 与 created_at，推进 updated_at",
              _patch_in_place, ),
    _scenario("patch.preserves_provenance", "修改不改发生时间和来源",
              "只改正文时 occurred_at / source / bucket / threads 保持原值",
              _patch_provenance, ),
    _scenario("supersede.links_history", "取代保留历史链",
              "旧卡变 superseded 并指向新卡，正文保留；只在历史与显式历史读取中出现",
              _supersede_links, ),
    _scenario("supersede.concurrent_same_target", "并发取代同一张卡",
              "基于同一观察的两次取代只有一次成功，另一次报 conflict，只有一个当前继任卡",
              _supersede_concurrent, ),
    _scenario("archive.retires", "归档退出召回但可追溯",
              "归档卡不进 fetch/index/search/recall/related，历史可见，正文与 created_at 不变",
              _archive_retires, ),
    _scenario("delete.hard", "真删后任何标准读路径都读不到",
              "删除后存储与六条读路径都不再返回该卡或其正文；重复删除安全；id 不复用",
              _delete_hard, ),
    _scenario("delete.after_supersede", "用户删除历史版本优先",
              "删除一张已被取代的卡成功，继任卡不受影响，关联与历史读取不再出现它",
              _delete_after_supersede, ),
    _scenario("conflict.supersede_after_delete", "取代与删除冲突",
              "目标已被删除时，基于旧观察的取代被拒绝，不写继任卡、不复活目标",
              _supersede_after_delete, ),
    _scenario("conflict.stale_patch", "基于过期观察的修改被拒",
              "目标在观察之后被改过，再按旧观察修改报 conflict，第一次修改保留",
              _stale_patch, ),
    _scenario("owner.isolation", "另一 owner 读不到、改不了",
              "另一 owner 的六条读路径读不到；patch/supersede/archive/delete 一律 not_found 且原卡不变",
              _owner_isolation, ),
    _scenario("idempotency.replay", "同一请求重放不重复写",
              "同一请求身份、同样内容重放，返回同一 id，不产生第二张卡，也不改写时间",
              _idempotent_replay, ),
    _scenario("idempotency.key_reuse", "同一请求身份不同内容报冲突",
              "请求身份撞了但内容不同，报 idempotency_conflict，不写第二批",
              _idempotency_key_reuse, ),
    _scenario("add.supplied_id_never_overwrites", "自带 id 的新增不覆盖已有卡",
              "新增请求带着已存在的 id（包括历史卡）时被拒绝，已有卡逐字段不变",
              _supplied_id_never_overwrites, ),
    _scenario("order.occurred_at", "发生时间决定顺序",
              "同分搜索结果按 occurred_at 从新到旧，与写入顺序无关；日期与带时区值同轴比较",
              _occurred_at_ordering, ),
    _scenario("receipt.errors", "错误回执规范且不回显正文",
              "目标不存在报 not_found，空卡报 invalid 且不落库，错误回执不含卡片正文",
              _error_receipts, ),
    _scenario("content.length", "长正文不被悄悄截短",
              "超长正文要么完整保存，要么明确 invalid；不许成功回执配截短的卡",
              _content_length, ),
    _scenario("capture.write_failure", "判断成功但写库失败",
              "写库失败回执是 storage_failed（不是没什么可记），进度不动、不留半批；重试写一次，重放不重复",
              _capture_failure, ),
    _scenario("capture.nothing_to_keep", "没什么可记推进进度",
              "空结果成功、不是错误、推进进度 —— 与写库失败可区分",
              _capture_nothing, ),
)


def scenario_ids() -> tuple[str, ...]:
    return tuple(s.id for s in SCENARIOS)


def run_scenario(scenario: Scenario, host: Host,
                 deviations: Mapping[str, Deviation] | None = None) -> Result:
    """跑一个场景并按声明分类。``deviations`` 的键是条款 id（``"patch.in_place/same_id"``）。"""
    checks = Checks()
    try:
        scenario.run(host, checks)
    except _Stop:
        pass
    except Exception as exc:  # noqa: BLE001 —— 适配器或宿主抛错也是一条证据
        checks.failures.append(ClauseFailure(f"{scenario.id}/raised",
                                             f"{type(exc).__name__}: {_short(str(exc))}"))
    declared = {k: v for k, v in (deviations or {}).items()
                if _clause_scenario(k) == scenario.id}
    failed = {f.clause for f in checks.failures}
    unexpected = sorted(failed - set(declared))
    # 只有**跑过且通过**的条款才算过期声明。前面一条致命失败（或适配器抛错）之后没跑到的
    # 条款无从判断，不能因为「没失败」就判它过期 —— 那会让一份正确的声明清单在别的条款
    # 出问题时一起变红，逼宿主删掉仍然成立的声明。
    stale = sorted((set(declared) - failed) & checks.ran)
    problems = tuple([f"undeclared failure: {c}" for c in unexpected]
                     + [f"stale deviation (clause now passes): {c}" for c in stale])
    used = tuple(sorted((k, v) for k, v in declared.items() if k in failed))
    if problems:
        status = "fail"
    elif not used:
        status = "pass"
    elif any(d.kind == "bug" for _k, d in used):
        status = "bug"
    else:
        status = "deviation"
    return Result(scenario=scenario.id, status=status, failures=tuple(checks.failures),
                  problems=problems, deviations=used)


def _clause_scenario(clause: str) -> str:
    """条款 id 形如 ``<scenario id>/<条款名>``。"""
    return clause.split("/", 1)[0]


def run_all(host_factory: Callable[[], Host], *,
            deviations: Mapping[str, Deviation] | None = None,
            only: Iterable[str] | None = None) -> list[Result]:
    """每个场景一个新宿主实例。未知的声明键直接报错 —— 拼错的声明等于没声明。"""
    known = set(scenario_ids())
    bad = [k for k in (deviations or {}) if "/" not in k or _clause_scenario(k) not in known]
    if bad:
        raise ValueError(f"deviation keys do not name a clause: {bad}")
    wanted = set(only) if only is not None else None
    return [run_scenario(s, host_factory(), deviations)
            for s in SCENARIOS if wanted is None or s.id in wanted]


def assert_conformant(results: Sequence[Result]) -> None:
    """有未声明失败或过期声明就抛 AssertionError，信息里带证据。"""
    broken = [r for r in results if r.status == "fail"]
    if not broken:
        return
    lines = []
    for r in broken:
        lines.append(f"{r.scenario}: {'; '.join(r.problems)}")
        for f in r.failures:
            lines.append(f"    {f.clause}: {f.evidence}")
    raise AssertionError("conformance failures:\n" + "\n".join(lines))


def results_table(results: Sequence[Result], *, host: str = "") -> str:
    """Markdown 表格：场景 × 结果 × 声明理由。"""
    head = f"| scenario | {host or 'result'} | clauses / reason |\n|---|---|---|\n"
    rows = []
    for r in results:
        if r.status in {"deviation", "bug"}:
            note = "; ".join(f"`{k}` ({d.kind}): {d.reason}" for k, d in r.deviations)
        elif r.status == "fail":
            note = "; ".join(r.problems)
        else:
            note = ""
        rows.append(f"| {r.scenario} | {r.status} | {note} |")
    return head + "\n".join(rows)



# --------------------------------------------------------------------------- #
# 参考宿主：MountedGarden + 官方 Store
# --------------------------------------------------------------------------- #

class _StepClock:
    """可控时钟。``tick`` 前进一分钟，写入时间因此严格可比。"""

    def __init__(self, start: str = "2026-09-01T00:00:00Z") -> None:
        from datetime import datetime

        self._now = datetime.fromisoformat(start.replace("Z", "+00:00"))

    def now_iso(self) -> str:
        return self._now.isoformat().replace("+00:00", "Z")

    def advance(self) -> None:
        self._now += timedelta(minutes=1)


class _FailOnce:
    """包住一个 Store：``arm()`` 之后下一次 ``apply`` 在存储里抛错。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self.armed = False

    def arm(self) -> None:
        self.armed = True

    def apply(self, *args: Any, **kwargs: Any):
        if self.armed:
            self.armed = False
            raise RuntimeError("injected storage failure")
        return self._store.apply(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._store, name)


_CARD_FIELDS = ("summary", "content", "bucket", "threads", "occurred_at", "source")


class ReferenceHost:
    """用 :class:`memgarden.MountedGarden` + 官方 Store 实现 :class:`Host`。

    它就是第三方宿主「不自己写执行器」时的接法，也是写宿主适配器的范例：

    - 写入走 ``MountedGarden.store_capture_result``（host-driven 的正门：typed mutation
      校验、mount 权限、能力检查、幂等键、CAS）与 ``delete_record``。
    - 卡片正文闸用公开的 ``card_text_rejection`` —— ``GardenComponent`` 在产出
      mutation 前跑的是同一个闸；Store 的 typed mutation 关口刻意只管结构。
    - ``commit_capture`` 的进度规则就是 ``OperationReceipt`` 的文档约定：
      ``written`` 或 ``reason == "nothing_worth_keeping"`` 才推进；``error`` 不推进。
    - 读路径：fetch / history 用 ``export``，index 用 ``browse``，search / recall /
      related 用同名方法。``inspect`` 直接读 Store 快照（不经产品读路径）。
    """

    name = "memgarden-reference"
    sources = ("history_import", "chat")

    def __init__(self, store: str = "sqlite", *, path: Any = None) -> None:
        from .mounted import MountedGarden
        from .selection import Chain, RecentStage, RelevanceStage
        from .stores.memory import InMemoryStore
        from .stores.sqlite import SqliteStore

        self.clock = _StepClock()
        if store == "sqlite":
            if path is None:
                raise ValueError("SqliteStore needs a path")
            raw = SqliteStore(path, clock=self.clock)
        elif store == "memory":
            raw = InMemoryStore(clock=self.clock)
        else:
            raise ValueError(f"unknown store {store!r}")
        self.store = _FailOnce(raw)
        self.garden = MountedGarden(
            model=None, store=self.store,
            selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=2))))
        self.tenant = "conformance-tenant"
        self._progress: dict[str, frozenset[str]] = {}

    # -- 作用域与回执 ---------------------------------------------------- #

    def _scope(self, owner: str):
        from .contracts import Actor
        from .mounted import Scope

        return Scope(tenant_id=self.tenant, memory_owner_id=owner,
                     actor=Actor(agent_id="conformance", session_id=owner))

    @staticmethod
    def _outcome(receipt: Any) -> Outcome:
        error = str(getattr(receipt, "error", "") or "")
        ids = tuple(str(i) for i in getattr(receipt, "record_ids", ()) or ())
        if not error:
            return Outcome(ok=True, record_ids=ids, reason=str(receipt.reason or ""))
        head = error.split(":", 1)[0]
        if error in {"record_not_found", "target_mount_mismatch"} or (
                head == "mutation_rejected" and "not found" in error):
            kind = "not_found"
        elif head == "mutation_rejected" and "已存在" in error:
            kind = "conflict"
        elif head == "revision_conflict":
            kind = "conflict"
        elif head == "idempotency_conflict":
            kind = "idempotency_conflict"
        elif head in {"invalid_mutation", "invalid_card_content", "record_id_required",
                      "requested_by_required"}:
            kind = "invalid"
        elif head == "storage_failed" or head == "partial_failure":
            kind = "storage_failed"
        elif head == "storage_lacks_capabilities":
            kind = "unsupported"
        else:
            kind = head
        # 原始错误里可能带 Store 异常消息；只留错误头作证据，避免回显内容。
        return Outcome(ok=False, record_ids=ids, error=kind, detail=head)

    def _gate(self, data: Mapping[str, Any]) -> Outcome | None:
        from .text.card_text import card_text_rejection
        from .text.leak_signals import GENERIC_SIGNALS

        # 参考宿主没有自己的泄漏识别器，显式用通用集（宿主应传自己的组合）。
        rejection = card_text_rejection(summary=str(data.get("summary") or ""),
                                        content=str(data.get("content") or ""),
                                        signals=GENERIC_SIGNALS)
        if rejection:
            return Outcome(ok=False, error="invalid", detail=f"invalid_card_content:{rejection}")
        return None

    def _write(self, owner: str, mutations: list[dict], *, key: str = "",
               based_on: Any = None):
        from .contracts import CaptureRequest, CaptureResult

        return self.garden.store_capture_result(
            self._scope(owner), CaptureRequest(window="", locale="en", idempotency_key=key),
            CaptureResult(mutations=mutations), expected_revision=based_on)

    @staticmethod
    def _card(data: Mapping[str, Any], **extra: Any) -> dict:
        out = {k: data[k] for k in _CARD_FIELDS if k in data}
        out["threads"] = list(out.get("threads") or [])
        out.update(extra)
        return out

    # -- 写 -------------------------------------------------------------- #

    def add(self, owner, card, *, request_id="", record_id=""):
        rejected = self._gate(card)
        if rejected:
            return rejected
        extra = {"id": record_id} if record_id else {}
        return self._outcome(self._write(
            owner, [{"op": "add", "card": self._card(card, **extra)}], key=request_id))

    def patch(self, owner, record_id, changes, *, based_on=None):
        current = self.inspect(owner, record_id) or {}
        merged = {**current, **dict(changes)}
        if current and self._gate(merged):
            return self._gate(merged)
        return self._outcome(self._write(
            owner, [{"op": "update", "record_id": record_id, "changes": dict(changes)}],
            key=f"patch:{record_id}:{self.clock.now_iso()}:{sorted(dict(changes).items())}",
            based_on=based_on))

    def supersede(self, owner, target_ids, card, *, based_on=None):
        rejected = self._gate(card)
        if rejected:
            return rejected
        return self._outcome(self._write(
            owner, [{"op": "supersede", "target_ids": list(target_ids), "card": self._card(card)}],
            based_on=based_on))

    def archive(self, owner, record_id, *, reason=""):
        return self._outcome(self._write(
            owner, [{"op": "archive", "record_id": record_id, "reason": reason}],
            key=f"archive:{record_id}"))

    def delete(self, owner, record_id, *, requested_by):
        return self._outcome(self.garden.delete_record(
            self._scope(owner), record_id, requested_by=requested_by))

    def observe(self, owner):
        return self.store.load(self.tenant, owner=owner).revision

    def tick(self):
        self.clock.advance()

    # -- Capture ------------------------------------------------------------ #

    def commit_capture(self, owner, cards, *, request_id, fail_storage=False):
        for data in cards:
            rejected = self._gate(data)
            if rejected:
                return rejected
        if fail_storage:
            self.store.arm()
        try:
            receipt = self._write(owner, [{"op": "add", "card": self._card(c)} for c in cards],
                                  key=request_id)
        finally:
            self.store.armed = False
        out = self._outcome(receipt)
        if out.ok and (receipt.written or receipt.reason == "nothing_worth_keeping"):
            self._progress[owner] = self._progress.get(owner, frozenset()) | {request_id}
        if out.ok and receipt.reason == "nothing_worth_keeping":
            return Outcome(ok=True, reason="nothing_to_keep")
        return out

    def capture_progress(self, owner):
        return self._progress.get(owner, frozenset())

    # -- 读 -------------------------------------------------------------- #

    @staticmethod
    def _view(raw: Mapping[str, Any]) -> dict:
        if str(raw.get("superseded_by") or "").strip():
            status = "superseded"
        elif raw.get("archived"):
            status = "archived"
        else:
            status = "active"
        return {
            "id": str(raw.get("id") or ""),
            **{k: raw.get(k) for k in _CARD_FIELDS},
            "created_at": raw.get("created_at"), "updated_at": raw.get("updated_at"),
            "status": status, "superseded_by": str(raw.get("superseded_by") or ""),
        }

    def inspect(self, owner, record_id):
        snapshot = self.store.load(self.tenant, owner=owner, include_archived=True,
                                   include_superseded=True)
        for raw in snapshot.cards:
            if str(raw.get("id") or "") == record_id:
                return self._view(raw)
        return None

    def _export(self, owner: str, *, include_archived: bool) -> list[dict]:
        page = self.garden.export(self._scope(owner), include_archived=include_archived,
                                  limit=self.garden.MAX_PAGE)
        return [self._view(r) for r in page.records]

    def fetch(self, owner, ids, *, include_history=False):
        wanted = set(ids)
        return [v for v in self._export(owner, include_archived=include_history)
                if v["id"] in wanted]

    def index(self, owner):
        page = self.garden.browse(self._scope(owner), limit=self.garden.MAX_PAGE)
        return [{"id": item.record_ref, "summary": item.display_text} for item in page]

    def search(self, owner, query):
        return list(self.garden.search(self._scope(owner), query).record_ids)

    def recall(self, owner, query):
        return list(self.garden.context_for_turn(self._scope(owner), query).record_ids)

    def related(self, owner, ids):
        return [str(item.get("id") or "") for item in self.garden.related(self._scope(owner), list(ids))]

    def history(self, owner):
        return self._export(owner, include_archived=True)


__all__ = [
    "SCENARIO_VERSION", "ERROR_KINDS", "READ_PATHS", "STATUSES",
    "Outcome", "Host", "Deviation", "ClauseFailure", "Checks", "Scenario", "Result",
    "SCENARIOS", "card", "scenario_ids", "run_scenario", "run_all", "assert_conformant",
    "results_table", "ReferenceHost",
]
