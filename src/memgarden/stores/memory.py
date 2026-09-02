"""内存存储 —— 给测试和试玩用，进程退出即丢。

它同时是 `StoragePort` 的**活文档**：这份实现不到 100 行，说明接口没有
把复杂度推给实现方。如果哪天加一个方法让这个文件写不下去了，
那多半是接口设计出了问题，不是实现方偷懒。
"""
from __future__ import annotations

import copy
import itertools
import threading

from ..storage import (
    ApplyResult,
    Capabilities,
    FULL_CAPABILITIES,
    IdempotencyConflict,
    RevisionConflict,
    Snapshot,
    mutations_digest,
)


class InMemoryStore:
    """线程安全的最小实现。CAS 用一个单调递增的整数当版本号。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cards: dict[str, dict[str, dict]] = {}   # tenant -> id -> card
        self._revision: dict[str, int] = {}
        self._applied: dict[str, dict[str, ApplyResult]] = {}  # tenant -> key -> 结果
        self._ids = itertools.count(1)

    # -- 能力声明 -------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        return FULL_CAPABILITIES

    # -- 读 -------------------------------------------------------------- #

    def load(self, tenant: str, **filters) -> Snapshot:
        with self._lock:
            cards = list(self._cards.get(tenant, {}).values())
            if not filters.get("include_archived"):
                cards = [c for c in cards if not c.get("archived")]
            if not filters.get("include_superseded"):
                cards = [c for c in cards if not c.get("superseded_by")]
            return Snapshot(cards=copy.deepcopy(cards), revision=self._rev(tenant))

    # -- 写 -------------------------------------------------------------- #

    def apply(
        self,
        tenant: str,
        mutations: list[dict],
        *,
        idempotency_key: str,
        expected_revision: str | None = None,
    ) -> ApplyResult:
        with self._lock:
            # 幂等：同一个 key 重放，原样返回上次的结果，不重复写。
            # 但**必须是同一批内容** —— 同 key 不同内容不是重放，是撞了 key，
            # 静默返回旧结果会让第二批改动凭空消失。
            digest = mutations_digest(mutations)
            cached = self._applied.get(tenant, {}).get(idempotency_key)
            if cached is not None:
                prev_digest, prev_result = cached
                if prev_digest != digest:
                    raise IdempotencyConflict(idempotency_key)
                return prev_result

            if expected_revision is not None and expected_revision != self._rev(tenant):
                raise RevisionConflict(expected_revision, self._rev(tenant))

            bucket = self._cards.setdefault(tenant, {})
            results: list[dict] = []
            # 原子：先在副本上做完，全部成功才落回去。
            staged = dict(bucket)
            for m in mutations:
                op = str(m.get("op") or "add")
                if op == "add":
                    card = dict(m.get("card") or {})
                    card.setdefault("id", f"m_{next(self._ids)}")
                    staged[card["id"]] = card
                    results.append({"id": card["id"], "status": "written"})
                elif op == "supersede":
                    # 一张新卡可以取代**多张**旧卡 —— 整理(merge/thicken)就是这个
                    # 形状。两个参考 Store 必须表现一致,否则接入方换一个 store
                    # 就断,而这正是「可替换存储」要保证的东西。
                    targets: list[str] = []
                    for candidate in ([m.get("target_id")]
                                      + list(m.get("target_ids") or ())):
                        value = str(candidate or "").strip()
                        if value and value not in targets:
                            targets.append(value)
                    if not targets:
                        raise KeyError("supersede without a target")
                    for old_id in targets:
                        # 一张找不到就整条失败 —— 半成功会留下「旧卡还活着、
                        # 新卡也活着」的双活状态。
                        if old_id not in staged:
                            raise KeyError(f"supersede target not found: {old_id}")
                    new_card = dict(m.get("card") or {})
                    new_card.setdefault("id", f"m_{next(self._ids)}")
                    for old_id in targets:
                        staged[old_id] = {**staged[old_id],
                                          "superseded_by": new_card["id"],
                                          "archived": True}
                    staged[new_card["id"]] = new_card
                    results.append({
                        "id": new_card["id"], "status": "superseded",
                        "replaced": targets[0] if len(targets) == 1 else list(targets),
                    })
                elif op == "delete":
                    target = str(m.get("target_id") or "")
                    staged.pop(target, None)
                    results.append({"id": target, "status": "deleted"})
                else:
                    raise ValueError(f"unknown op: {op}")

            self._cards[tenant] = staged
            self._revision[tenant] = int(self._rev(tenant)) + 1
            out = ApplyResult(results=results, revision=self._rev(tenant))
            self._applied.setdefault(tenant, {})[idempotency_key] = (digest, out)
            return out

    # -- 内部 ------------------------------------------------------------ #

    def _rev(self, tenant: str) -> str:
        return str(self._revision.setdefault(tenant, 0))
