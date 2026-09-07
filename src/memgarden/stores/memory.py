"""内存存储 —— 给测试和试玩用，进程退出即丢。

它同时是 `StoragePort` 的**活文档**：接口如果让这份实现写不下去了，
那多半是接口设计出了问题，不是实现方偷懒。

## 归属边界：(tenant, owner)

``tenant`` 是账户 / 组织 / 部署的安全边界；``owner`` 是**一座长期花园的稳定
所有者**。两者都进 key，不是只进 tenant：

    同一个 tenant，两个 agent 各自的 agent-private
    → 必须互相读不到

以前只按 tenant 分桶，上面这句话不成立 —— 同租户的另一个 agent 能读到全部。
"""
from __future__ import annotations

import copy
import re
import threading

from ..storage import (
    ApplyResult,
    Capabilities,
    FULL_CAPABILITIES,
    IdempotencyConflict,
    RevisionConflict,
    Snapshot,
    apply_digest,
)
from ._ops import apply_ops, new_seed_mounts


class InMemoryStore:
    """线程安全的最小实现。CAS 用一个单调递增的整数当版本号。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 🔴 key 是 (tenant, owner) —— 只按 tenant 分桶就是同租户越权的根因。
        self._cards: dict[tuple[str, str], dict[str, dict]] = {}
        self._revision: dict[tuple[str, str], int] = {}
        self._applied: dict[tuple[str, str], dict[str, tuple]] = {}
        self._ledger: dict[tuple[str, str, str], dict] = {}   # +mount
        self._seed_generation: dict[tuple[str, str, str], int] = {}
        self._next_ids: dict[tuple[str, str], int] = {}

    # -- 能力声明 -------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        return FULL_CAPABILITIES

    # -- 读 -------------------------------------------------------------- #

    def load(self, tenant: str, *, owner: str, **filters) -> Snapshot:
        key = _key(tenant, owner)
        with self._lock:
            cards = list(self._cards.get(key, {}).values())
            if not filters.get("include_archived"):
                cards = [c for c in cards if not c.get("archived")]
            if not filters.get("include_superseded"):
                cards = [c for c in cards if not c.get("superseded_by")]
            return Snapshot(cards=copy.deepcopy(cards),
                            revision=self._rev(key), owner=key[1],
                            seed_generations={
                                mount: value
                                for (tenant, owner, mount), value
                                in self._seed_generation.items()
                                if (tenant, owner) == key
                            })

    def maintenance_state(self, tenant: str, *, owner: str, mount: str) -> dict:
        """上一次整理留下的账本。没有就返回空 dict。"""
        with self._lock:
            return dict(self._ledger.get((*_key(tenant, owner), mount)) or {})

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
        key = _key(tenant, owner)
        with self._lock:
            # 幂等：同一个 key 重放，原样返回上次的结果，不重复写。
            # 但**必须是同一批内容** —— 同 key 不同内容不是重放，是撞了 key，
            # 静默返回旧结果会让第二批改动凭空消失。
            digest = apply_digest(mutations, maintenance_state)
            cached = self._applied.get(key, {}).get(idempotency_key)
            if cached is not None:
                prev_digest, prev_result = cached
                if prev_digest != digest:
                    raise IdempotencyConflict(idempotency_key)
                return prev_result

            if expected_revision is not None and expected_revision != self._rev(key):
                raise RevisionConflict(expected_revision, self._rev(key))

            bucket = self._cards.setdefault(key, {})
            # 原子：先在副本上做完，全部成功才落回去。
            staged = dict(bucket)
            next_id = self._reserved_next_id(key, mutations)

            def allocate() -> str:
                nonlocal next_id
                value = next_id
                next_id += 1
                return f"m_{value}"

            results = apply_ops(staged, mutations, new_id=allocate)

            seed_mounts = new_seed_mounts(
                mutations, before=bucket, staged=staged)
            self._cards[key] = staged
            # ID 水位也只在整批成功后提交；失败批次不能留下半个可见状态。
            self._next_ids[key] = next_id
            for mount in seed_mounts:
                generation_key = (*key, mount)
                self._seed_generation[generation_key] = (
                    self._seed_generation.get(generation_key, 0) + 1)
            changed = (staged != bucket or bool(seed_mounts)
                       or maintenance_state is not None)
            if changed:
                self._revision[key] = int(self._rev(key)) + 1
            # 🔴 账本和卡改动在同一个临界区里落地 —— 任一半单独推进都会
            # 造成「整理丢了没人知道」或「同一批反复整理」。
            if maintenance_state is not None:
                mount = str(maintenance_state.get("mount") or "agent-private")
                self._ledger[(*key, mount)] = {
                    **dict(maintenance_state), "revision": self._rev(key)}
            out = ApplyResult(results=results, revision=self._rev(key))
            self._applied.setdefault(key, {})[idempotency_key] = (digest, out)
            return out

    # -- 内部 ------------------------------------------------------------ #

    def _rev(self, key: tuple[str, str]) -> str:
        return str(self._revision.setdefault(key, 0))

    def _reserved_next_id(
        self, key: tuple[str, str], mutations: list[dict],
    ) -> int:
        watermark = self._next_ids.get(key, 1)
        for mutation in mutations:
            card = mutation.get("card")
            supplied = str(card.get("id") or "") if isinstance(card, dict) else ""
            match = re.fullmatch(r"m_(\d+)", supplied)
            if match:
                watermark = max(watermark, int(match.group(1)) + 1)
        return watermark


def _key(tenant: str, owner: str) -> tuple[str, str]:
    """归属键。**owner 为空直接拒绝** —— 不回退成全局默认值。

    回退的后果很具体：所有没显式给 owner 的调用共用同一座花园，
    于是「同租户两个 agent 互不可见」这条在最常见的路径上根本不成立。
    """
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
