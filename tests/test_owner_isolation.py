"""同一个租户、同一个库、两个 memory owner —— 互相读不到。

## 为什么单独一个文件

这是 sevenfloor 2026-09-06 **实际复现出来的越权**，不是设想的风险：

    同一个 tenant
    Agent A 写 mount=agent-private 的卡
    Agent B 用同一个 tenant、不同 agent_id 读
    → B 读得到 A 的卡

而当时号称「多 agent 隔离通过」的那组测试用的是 ``tenant-a`` / ``tenant-b``
两个不同租户。跨租户隔离靠的是表分离，本来就成立 —— 它证明的是另一件事，
**恰好绕开了真正会漏的那条路**。

所以这里的每一条都必须：同一个 tenant、同一个库文件、只有 owner 不同。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from memgarden.contracts import CaptureRequest
from memgarden.mounted import MissingMemoryOwner, MountedGarden, Scope
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore

TENANT = "acme"
CARD = {"action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
        "summary": "不吃辣，一吃就胃疼",
        "content": "对方不吃辣，一吃就胃疼，点菜需要避开辣味。"}


class _Model:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return json.dumps({"cards": [CARD]}, ensure_ascii=False)


def _stores():
    db = str(pathlib.Path(tempfile.mkdtemp()) / "shared.db")
    return [("memory", InMemoryStore()), ("sqlite", SqliteStore(db))]


@pytest.fixture(params=[s[0] for s in _stores()])
def store(request):
    db = str(pathlib.Path(tempfile.mkdtemp()) / "shared.db")
    return InMemoryStore() if request.param == "memory" else SqliteStore(db)


def _scope(owner: str, **kw) -> Scope:
    return Scope(tenant_id=TENANT, memory_owner_id=owner, **kw)


def test_one_owner_cannot_read_another_owners_cards(store):
    """A 写，B 读不到。**同一个租户、同一个库。**"""
    a = MountedGarden(model=_Model(), store=store)
    a.capture_and_store(_scope("agent-a"),
                        CaptureRequest(window="我不吃辣", locale="zh-Hans"))

    b = MountedGarden(model=_Model(), store=store)
    assert b.browse(_scope("agent-b")) == []
    assert a.browse(_scope("agent-a")), "A 自己得看得到，否则这条测试没意义"


def test_one_owner_cannot_search_another_owners_cards(store):
    """召回面同样隔离 —— 读不到不等于搜不到，两个面都要挡。"""
    garden = MountedGarden(model=_Model(), store=store)
    garden.capture_and_store(_scope("agent-a"),
                             CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    found = garden.context_for_turn(_scope("agent-b"), "吃什么")
    assert not found.record_ids


def test_one_owner_cannot_export_another_owners_cards(store):
    garden = MountedGarden(model=_Model(), store=store)
    garden.capture_and_store(_scope("agent-a"),
                             CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    exported = garden.export(_scope("agent-b"))
    assert not (exported.records if hasattr(exported, "records") else exported)


def test_one_owner_cannot_delete_another_owners_card(store):
    """拿到别人的 id 也删不掉 —— 删除只在自己看得见的范围内生效。"""
    garden = MountedGarden(model=_Model(), store=store)
    receipt = garden.capture_and_store(
        _scope("agent-a"), CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    victim = receipt.record_ids[0]

    out = garden.delete_record(_scope("agent-b"), victim, requested_by="attacker")
    assert out.error == "record_not_found"
    # 真的还在
    assert [c["id"] for c in store.load(TENANT, owner="agent-a").cards] == [victim]


def test_the_model_cannot_widen_its_own_scope_through_tool_arguments(store):
    """模型在工具参数里写别人的 tenant/owner —— 一律无效。

    工具参数是**模型生成的**。读它等于让模型自己决定能看谁的记忆。
    """
    from memgarden.contracts import ToolCall

    garden = MountedGarden(model=_Model(), store=store)
    garden.capture_and_store(_scope("agent-a"),
                             CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    out = garden.invoke_tool(_scope("agent-b"), ToolCall(
        name="memory_search",
        arguments={"query": "辣", "tenant_id": TENANT,
                   "memory_owner_id": "agent-a", "mounts": ["agent-private"]},
    ))
    assert out.ok
    assert not (out.content or "").strip(), "参数里的 owner 不该被采信"


def test_a_scope_without_an_owner_fails_closed(store):
    """缺稳定 owner → 抛，**不回退成默认花园**。

    回退的后果是所有没显式给 owner 的调用共用一座花园 ——
    隔离在最常见的那条路径上直接失效，而且一切「正常」。
    """
    garden = MountedGarden(model=_Model(), store=store)
    with pytest.raises(MissingMemoryOwner):
        garden.browse(Scope(tenant_id=TENANT))


def test_owner_is_pushed_into_the_store_query_not_filtered_afterwards():
    """owner 必须进**查询条件**，不是读回整租户再在 Python 里过滤。

    两者在正常情况下结果一样，差别只在出错时才看得见：漏一处过滤，
    前者读不到、后者读得到。所以要直接断言存储收到了 owner。
    """
    seen: list[str] = []

    class _Spy(InMemoryStore):
        def load(self, tenant, *, owner, **filters):
            seen.append(owner)
            return super().load(tenant, owner=owner, **filters)

    MountedGarden(model=_Model(), store=_Spy()).browse(_scope("agent-a"))
    assert seen == ["agent-a"]


def test_a_store_that_ignores_owner_is_caught(store):
    """存储把 owner 过滤漏了 —— 必须当场发现，而不是把别人的卡端上来。

    快照带回它自己的 owner，对不上就抛。没有这一层的话，一个实现有 bug 的
    适配器造成的越权读**没有任何症状**。
    """
    from memgarden.mounted import MountPermissionError
    from memgarden.storage import Snapshot

    class _Sloppy(InMemoryStore):
        def load(self, tenant, *, owner, **filters):
            snap = super().load(tenant, owner=owner, **filters)
            return Snapshot(cards=snap.cards, revision=snap.revision,
                            owner="somebody-else")

    garden = MountedGarden(model=_Model(), store=_Sloppy())
    with pytest.raises(MountPermissionError):
        garden.browse(_scope("agent-a"))
