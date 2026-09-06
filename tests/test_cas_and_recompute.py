"""基于旧状态做的判断，必须带着那份状态的版本号回来提交。

审查复现的问题是：``Store`` 支持 CAS，**主链路却从不使用** ——
``_apply`` 里写死 ``expected_revision=None``。于是并发下后写的那次会盖掉
先写的判断，而且不报错。
"""
from __future__ import annotations

import json

import pytest

from memgarden.contracts import CaptureRequest, MaintenanceRequest
from memgarden.mounted import MountedGarden, Scope
from memgarden.stores.memory import InMemoryStore

CARD = {"action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
        "summary": "不吃辣，一吃就胃疼", "content": "点菜需要避开辣味。"}
ME = Scope(tenant_id="t1", memory_owner_id="owner-1")


class _Model:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return json.dumps({"cards": [CARD]}, ensure_ascii=False)


def test_capture_submits_with_the_revision_it_read():
    """读到 R1 就必须拿 R1 提交。传 None 等于放弃并发保护。"""
    seen: list = []

    class _Spy(InMemoryStore):
        def apply(self, tenant, mutations, *, owner, idempotency_key,
                  expected_revision=None, maintenance_state=None):
            seen.append(expected_revision)
            return super().apply(tenant, mutations, owner=owner,
                                 idempotency_key=idempotency_key,
                                 expected_revision=expected_revision,
                                 maintenance_state=maintenance_state)

    MountedGarden(model=_Model(), store=_Spy()).capture_and_store(
        ME, CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    assert seen == ["0"], f"提交时带的版本号是 {seen!r}，不该是 None"


def test_a_concurrent_write_forces_a_fresh_judgement_not_a_replay():
    """冲突之后**重新判断**，不是把旧 mutation 重放一遍。

    重放是最诱人也最错的做法：那批 mutation 基于旧快照算出来，里面的
    target_id 可能已经被别人 supersede 掉了，去重结论也可能失效。
    重放的结果是「凭空多一张重复卡」或「supersede 一张不存在的卡」。
    """
    store = InMemoryStore()
    calls: list[str] = []
    bumped = {"done": False}

    class _Model2:
        def complete(self, prompt: str, *, purpose: str = "") -> str:
            calls.append(prompt)
            if not bumped["done"]:
                # 判断进行到一半，别人写了一张 —— 版本号往前走了。
                bumped["done"] = True
                store.apply("t1", [{"op": "add",
                                    "card": {"summary": "别人写的", "content": "x"}}],
                            owner="owner-1", idempotency_key="other",
                            expected_revision=None)
            return json.dumps({"cards": [CARD]}, ensure_ascii=False)

    receipt = MountedGarden(model=_Model2(), store=store).capture_and_store(
        ME, CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    assert receipt.written, receipt.error
    # 🔴 模型被**重新问了一次** —— 这正是「重读重算」和「重放」的分界线。
    assert len(calls) == 2, f"模型只被调了 {len(calls)} 次，说明是重放不是重算"
    # 而且第二次的 prompt 里能看到别人刚写的那张
    assert "别人写的" in calls[1]


def test_recompute_is_bounded_and_reports_conflict_honestly():
    """一直冲突就如实报 conflict，**绝不对用户说「记住了」**。

    无界重算在高并发下会把额度烧光，而且每次重算都要调一次模型。
    """
    class _AlwaysBusy(InMemoryStore):
        def apply(self, tenant, mutations, *, owner, idempotency_key,
                  expected_revision=None, maintenance_state=None):
            from memgarden.storage import RevisionConflict
            raise RevisionConflict(str(expected_revision), "999")

    receipt = MountedGarden(model=_Model(), store=_AlwaysBusy()).capture_and_store(
        ME, CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    assert receipt.written is False
    assert receipt.error == "revision_conflict"
    assert receipt.trace.get("recompute_attempt") == MountedGarden.MAX_RECOMPUTE


def test_maintenance_also_uses_cas():
    """整理同样依赖旧状态，同样必须走 CAS。"""
    seen: list = []

    class _Spy(InMemoryStore):
        def apply(self, tenant, mutations, *, owner, idempotency_key,
                  expected_revision=None, maintenance_state=None):
            seen.append(expected_revision)
            return super().apply(tenant, mutations, owner=owner,
                                 idempotency_key=idempotency_key,
                                 expected_revision=expected_revision,
                                 maintenance_state=maintenance_state)

    store = _Spy()
    for i in range(12):
        store.apply("t1", [{"op": "add", "card": {"id": f"m_{i}",
                                                  "summary": f"第 {i} 条",
                                                  "content": "正文"}}],
                    owner="owner-1", idempotency_key=f"seed-{i}",
                    expected_revision=None)
    seen.clear()

    class _Tidy:
        def complete(self, prompt: str, *, purpose: str = "") -> str:
            return json.dumps({"consolidations": [{
                "op": "merge", "card_ids": ["m_0", "m_1"],
                "rationale": "这两条讲的是同一件事。",
                "result": {"bucket": "未分类", "threads": [],
                           "summary": "合并后的", "content": "合并后的正文。"},
            }]}, ensure_ascii=False)

    out = MountedGarden(model=_Tidy(), store=store,
                        min_new_cards_for_maintenance=1
                        ).run_and_store_maintenance(
        ME, MaintenanceRequest(locale="zh-Hans"))
    assert out.written, f"整理没落库: {out.error or out.reason}"
    assert seen and seen[0] is not None, "整理提交时没带版本号"
