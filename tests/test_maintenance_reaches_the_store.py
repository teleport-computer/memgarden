"""整理的产出必须能被官方 Store 真正执行 —— 纵向链路，不是模块内部行为。

## 为什么单独一个文件

2026-09-02 sevenfloor 复核时实测：

    GardenComponent.run_maintenance()  → op = merge
    SqliteStore.apply()                → ValueError: unknown op: merge

`run_maintenance` 当时把**模型的原始建议**原样当 ``mutations`` 返回（只加了个
mount 字段）。建议侧的 op 是 merge/thicken/supersede，存储侧是 add/update/
supersede/… —— 两套词汇表，撞在一起就是未知 op。

用户那头的症状是：**io 说整理好了，但记忆没变。**

**为什么一直没被发现**：在此之前，整个测试套里 maintenance 的用例
**全部使用 ``{"consolidations": []}``**，也就是模型返回空结果的 noop 分支。
空结果不产出 mutation，永远走不到下游。所以这里的每条用例都**必须是非空的**。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from memgarden import GardenComponent
from memgarden.contracts import MaintenanceRequest
from memgarden.stores.sqlite import SqliteStore


class _Model:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self.reply


def _cards(n: int) -> list[dict]:
    return [{"id": f"m_{i}", "summary": f"第 {i} 条", "content": f"正文 {i}"}
            for i in range(1, n + 1)]


def _reply(op: str, card_ids: list[str], summary: str) -> str:
    return json.dumps({"consolidations": [{
        "op": op,
        "card_ids": card_ids,
        "rationale": "这几条讲的是同一件事，收敛成一条更清楚。",
        "result": {"bucket": "未分类", "threads": [],
                   "summary": summary, "content": f"{summary}的正文。"},
    }]}, ensure_ascii=False)


@pytest.fixture()
def store() -> SqliteStore:
    return SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db"))


@pytest.mark.parametrize("op", ["merge", "thicken", "supersede"])
def test_a_non_empty_consolidation_reaches_the_store(store, op):
    """三种整理动作都要能落库。

    它们在存储上是同一个形状（N 张旧卡收敛成 1 张新卡），但都得真的走通 ——
    只测其中一种，另外两种坏了没人知道。
    """
    cards = _cards(12)
    store.apply("t1", [{"op": "add", "card": c} for c in cards],
                idempotency_key="seed")

    result = GardenComponent(
        model=_Model(_reply(op, ["m_1", "m_2"], "收敛后的说法")),
        min_new_cards_for_maintenance=1,
    ).run_maintenance(MaintenanceRequest(
        cards=cards, all_cards=cards,
        known_ids=tuple(c["id"] for c in cards), locale="zh-Hans",
    ))

    assert result.needed and not result.error
    assert result.mutations, "非空建议却没产出 mutation —— 转换那一步丢了东西"

    # 这一行就是当初断掉的地方：官方 Store 必须认识它。
    store.apply("t1", result.mutations, idempotency_key=f"tidy-{op}")


def test_the_new_card_is_active_and_the_old_ones_are_traceable(store):
    """收敛之后：新卡可召回，旧卡仍可追溯，且不存在两张同时 active。"""
    cards = _cards(12)
    store.apply("t1", [{"op": "add", "card": c} for c in cards],
                idempotency_key="seed")

    result = GardenComponent(
        model=_Model(_reply("merge", ["m_1", "m_2"], "合并后的说法")),
        min_new_cards_for_maintenance=1,
    ).run_maintenance(MaintenanceRequest(
        cards=cards, all_cards=cards,
        known_ids=tuple(c["id"] for c in cards), locale="zh-Hans",
    ))
    store.apply("t1", result.mutations, idempotency_key="tidy")

    active = {c["id"]: c for c in store.load("t1").cards}
    everything = {c["id"]: c
                  for c in store.load("t1", include_archived=True,
                                      include_superseded=True).cards}

    # 旧的两张不再 active
    assert "m_1" not in active and "m_2" not in active, (
        f"旧卡还活着 —— 会和新卡同时被召回：{sorted(active)}"
    )
    # 但历史里还在，而且指得出被谁取代
    for old_id in ("m_1", "m_2"):
        assert old_id in everything, f"{old_id} 被删了 —— 整理只归档不删除"
        assert everything[old_id].get("superseded_by"), (
            f"{old_id} 归档了却没说被谁取代，用户查历史时是断的"
        )

    # 新卡活着，且两张旧卡都指向同一张
    new_ids = {everything[o]["superseded_by"] for o in ("m_1", "m_2")}
    assert len(new_ids) == 1, f"两张旧卡指向了不同的新卡：{new_ids}"
    new_id = new_ids.pop()
    assert new_id in active, "合并出来的新卡不在 active 里 —— 等于记忆凭空少了"
    assert active[new_id]["summary"] == "合并后的说法"

    # 总数：12 - 2 + 1
    assert len(active) == 11, f"active 卡数不对：{len(active)}"


def test_a_missing_target_fails_the_whole_batch(store):
    """有一张目标卡找不到时，整条必须失败。

    半成功会留下「旧卡还活着、新卡也活着」的双活状态 —— 那是整理最不该产生的
    结果：用户会看到两条自相矛盾的记忆，而且没人知道哪条是新的。
    """
    cards = _cards(12)
    store.apply("t1", [{"op": "add", "card": c} for c in cards[:1]],
                idempotency_key="seed")  # 只写进 m_1

    result = GardenComponent(
        model=_Model(_reply("merge", ["m_1", "m_2"], "合并后的说法")),
        min_new_cards_for_maintenance=1,
    ).run_maintenance(MaintenanceRequest(
        cards=cards, all_cards=cards,
        known_ids=tuple(c["id"] for c in cards), locale="zh-Hans",
    ))

    with pytest.raises(KeyError):
        store.apply("t1", result.mutations, idempotency_key="tidy")

    # 失败之后 m_1 必须原样活着，不能被改成「已被取代」
    active = {c["id"]: c for c in store.load("t1").cards}
    assert "m_1" in active, "批次失败了却把 m_1 归档了 —— 没有回滚干净"
    assert not active["m_1"].get("superseded_by")


def test_the_ledger_fields_come_back_for_the_host_to_persist(store):
    """整理完宿主要把账本存回去，否则下次判断不出增量、会反复整理同一批。"""
    cards = _cards(12)
    result = GardenComponent(
        model=_Model(_reply("merge", ["m_1", "m_2"], "合并后的说法")),
        min_new_cards_for_maintenance=1,
    ).run_maintenance(MaintenanceRequest(
        cards=cards, all_cards=cards,
        known_ids=tuple(c["id"] for c in cards), locale="zh-Hans",
    ))
    assert result.trace.get("signature"), "没有 signature，宿主存不回账本"
    assert result.trace.get("seed_card_count") is not None


def test_the_raw_proposal_is_still_available_for_hosts_with_their_own_format(store):
    """转换之后，**原始建议仍要给出去**。

    宿主可能有自己的写入格式（io 的 action 带加密信封和通话溯源，mutations
    表达不了），它需要从原始建议构造。转换不能把这条路堵死。
    """
    cards = _cards(12)
    result = GardenComponent(
        model=_Model(_reply("merge", ["m_1", "m_2"], "合并后的说法")),
        min_new_cards_for_maintenance=1,
    ).run_maintenance(MaintenanceRequest(
        cards=cards, all_cards=cards,
        known_ids=tuple(c["id"] for c in cards), locale="zh-Hans",
    ))
    assert result.consolidations, "原始建议被转换吃掉了"
    assert result.consolidations[0]["op"] == "merge"
    # 而 mutations 里记着它本来是哪一种整理
    assert result.mutations[0]["consolidation_op"] == "merge"


def test_both_reference_stores_behave_the_same(store):
    """两个参考 Store 对同一批 mutation 必须给出同样的结果。

    「存储可替换」是这个包的卖点之一。只在 SQLite 上验证，接入方换成
    InMemory（或照着它写自己的 adapter）就会断 —— 而且断在整理这条最难
    察觉的路上。
    """
    from memgarden.stores.memory import InMemoryStore

    cards = _cards(12)
    result = GardenComponent(
        model=_Model(_reply("merge", ["m_1", "m_2"], "合并后的说法")),
        min_new_cards_for_maintenance=1,
    ).run_maintenance(MaintenanceRequest(
        cards=cards, all_cards=cards,
        known_ids=tuple(c["id"] for c in cards), locale="zh-Hans",
    ))

    def _apply_and_read(target):
        target.apply("t1", [{"op": "add", "card": c} for c in cards],
                     idempotency_key="seed")
        target.apply("t1", result.mutations, idempotency_key="tidy")
        active = {c["id"]: c.get("summary") for c in target.load("t1").cards}
        archived = {
            c["id"]: c.get("superseded_by")
            for c in target.load("t1", include_archived=True,
                                 include_superseded=True).cards
            if c.get("superseded_by")
        }
        return sorted(active.values()), sorted(archived)

    sqlite_active, sqlite_archived = _apply_and_read(store)
    memory_active, memory_archived = _apply_and_read(InMemoryStore())

    assert sqlite_active == memory_active, "两个 Store 的 active 卡不一致"
    assert sqlite_archived == memory_archived, "两个 Store 的归档卡不一致"
