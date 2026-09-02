"""挂载好的花园：纵向链路测试（sevenfloor 2026-09-02 §8.1 的 A–E）。

这个文件和其它测试的区别是**它不测模块内部行为**，只问一件事：

    Capture → Store → Recall → Context → Maintenance → Store → Tool

这条链路真的能跑通吗。

上一轮翻车正是因为「各块分别存在」被当成了「链路已经闭合」——
`run_maintenance` 能产出 merge，官方 Store 不认识 merge，而两边各自的单测都是绿的。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from memgarden.contracts import (
    Actor, CaptureRequest, MaintenanceRequest, ToolCall,
)
from memgarden.mounted import MountPermissionError, MountedGarden, Scope
from memgarden.selection import Chain, RelevanceStage, RecentStage
from memgarden.stores.sqlite import SqliteStore


class _Model:
    """按顺序吐预设回复；用完一直吐最后一条。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["{}"]
        self.calls = 0

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def _card_reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


SPICY = {"action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
         "summary": "不吃辣，一吃就胃疼",
         "content": "对方不吃辣，一吃就胃疼，点菜需要避开辣味。"}


def _db() -> str:
    return str(pathlib.Path(tempfile.mkdtemp()) / "g.db")


def _garden(*replies: str, store=None) -> MountedGarden:
    return MountedGarden(
        model=_Model(*replies),
        store=store if store is not None else SqliteStore(_db()),
        selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))),
    )


ALICE = Scope(tenant_id="alice", actor=Actor(user_id="alice", agent_id="a1"))


# --------------------------------------------------------------------------- #
# A. Capture → Store → Context
# --------------------------------------------------------------------------- #

def test_A_capture_then_recall_in_a_new_turn():
    """记下来的东西，下一轮要能自己被想起来 —— 不靠调用方准备候选。"""
    garden = _garden(_card_reply(SPICY))

    receipt = garden.capture_and_store(ALICE, CaptureRequest(
        window="用户：我不吃辣，一吃就胃疼", locale="zh-Hans"))
    assert receipt.written, f"没写进去：{receipt.error or receipt.reason}"
    assert receipt.record_ids

    # 新的一轮：只给一句话，候选由 MountedGarden 自己从库里取
    ctx = garden.context_for_turn(ALICE, "晚饭吃什么")
    assert ctx.record_ids, f"召回不到刚记的东西：{ctx.trace}"
    assert any("不吃辣" in b["text"] for b in ctx.blocks), ctx.blocks


def test_A_nothing_worth_keeping_is_not_an_error():
    """模型觉得没什么可记 —— 这是**正常结果**，不是失败。

    两者混在一起的后果很实在：报成失败会让宿主重试同一批对话，
    报成成功又会让真正的解析失败被吞掉、游标带着没记住的窗口往前走。
    """
    garden = _garden(_card_reply())
    receipt = garden.capture_and_store(ALICE, CaptureRequest(
        window="用户：嗯", locale="zh-Hans"))
    assert not receipt.written
    assert receipt.error is None
    assert receipt.reason == "nothing_worth_keeping"


# --------------------------------------------------------------------------- #
# B. 非空 Maintenance → Store
# --------------------------------------------------------------------------- #

def _consolidation_reply(card_ids: list[str]) -> str:
    return json.dumps({"consolidations": [{
        "op": "merge",
        "card_ids": card_ids,
        "rationale": "这两条讲的是同一件事，合起来更完整。",
        "result": {"bucket": "偏好与边界", "threads": ["饮食"],
                   "summary": "不吃辣，点菜要避开",
                   "content": "对方不吃辣，一吃就胃疼，点菜需避开辣味。"},
    }]}, ensure_ascii=False)


def test_B_a_non_empty_merge_is_persisted_and_the_old_cards_stay_traceable():
    """整理必须真的改动库 —— 而不是「说整理好了，记忆没变」。"""
    store = SqliteStore(_db())
    seed = [{"id": f"m_{i}", "summary": f"第 {i} 条", "content": f"正文 {i}"}
            for i in range(1, 13)]
    store.apply("alice", [{"op": "add", "card": c} for c in seed],
                idempotency_key="seed")

    garden = MountedGarden(
        model=_Model(_consolidation_reply(["m_1", "m_2"])),
        store=store,
        selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))),
        min_new_cards_for_maintenance=1,
    )

    check = garden.check_maintenance(ALICE)
    assert check.needed, f"该整理却说不用：{check.trace}"

    receipt = garden.run_and_store_maintenance(
        ALICE, MaintenanceRequest(locale="zh-Hans"))
    assert receipt.written, f"整理没落库：{receipt.error or receipt.reason}"

    active = {c["id"] for c in store.load("alice").cards}
    everything = {c["id"]: c for c in store.load(
        "alice", include_archived=True, include_superseded=True).cards}

    assert "m_1" not in active and "m_2" not in active, "旧卡还活着，会和新卡同时被召回"
    for old in ("m_1", "m_2"):
        assert everything[old].get("superseded_by"), f"{old} 归档了却查不到被谁取代"
    assert len(active) == 11, f"12 - 2 + 1 应当是 11，实际 {len(active)}"


def test_B_maintenance_ledger_comes_back_so_it_does_not_loop():
    """账本要回来 —— 否则宿主判断不出增量，会反复整理同一批。"""
    store = SqliteStore(_db())
    seed = [{"id": f"m_{i}", "summary": f"第 {i} 条"} for i in range(1, 13)]
    store.apply("alice", [{"op": "add", "card": c} for c in seed],
                idempotency_key="seed")
    garden = MountedGarden(
        model=_Model(_consolidation_reply(["m_1", "m_2"])), store=store,
        selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))),
        min_new_cards_for_maintenance=1)
    receipt = garden.run_and_store_maintenance(
        ALICE, MaintenanceRequest(locale="zh-Hans"))
    assert receipt.trace.get("signature")
    assert receipt.trace.get("seed_card_count") is not None


# --------------------------------------------------------------------------- #
# C. Tool Search / Write 真执行
# --------------------------------------------------------------------------- #

def test_C_memory_write_persists_and_a_fresh_process_can_search_it():
    """写进去的东西，**换一个进程**也要搜得到。

    这是「真落库」和「返回一条建议」的分界线：后者重启就没了。
    """
    db = _db()
    writer = _garden(store=SqliteStore(db))
    out = writer.invoke_tool(ALICE, ToolCall(
        name="memory_write",
        arguments={"summary": "周末要去看医生", "content": "答应自己这周末去看医生。"},
    ))
    assert out.ok, out.error
    assert out.mutations and out.mutations[0]["record_id"], "没有回执 id"

    # 换一个 MountedGarden + 新开的 Store 句柄，模拟另一个进程
    reader = _garden(store=SqliteStore(db))
    found = reader.invoke_tool(ALICE, ToolCall(
        name="memory_search", arguments={"query": "医生"}))
    assert found.ok, found.error
    assert "看医生" in found.content, f"搜不到刚写的：{found.content!r}"


def test_C_search_no_longer_refuses_for_want_of_a_store():
    """挂载之后，搜索不该再回「需要宿主提供存储」。"""
    garden = _garden()
    out = garden.invoke_tool(ALICE, ToolCall(
        name="memory_search", arguments={"query": "辣"}))
    assert out.error != "search_requires_host_store"


# --------------------------------------------------------------------------- #
# D. 重放与并发
# --------------------------------------------------------------------------- #

def test_D_replaying_the_same_capture_does_not_write_twice():
    """同一批重放不写第二遍 —— 崩溃后重试是常态，不是异常。"""
    store = SqliteStore(_db())
    garden = MountedGarden(model=_Model(_card_reply(SPICY)), store=store,
                           selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))))
    req = CaptureRequest(window="用户：我不吃辣", locale="zh-Hans",
                         idempotency_key="turn-42")
    first = garden.capture_and_store(ALICE, req)
    second = garden.capture_and_store(ALICE, req)
    assert first.written and second.written
    assert len(store.load("alice").cards) == 1, "同一个幂等键写了两遍"


def test_D_a_failed_batch_leaves_nothing_behind():
    """批次里有一条失败，整批都不能落地。

    半成功是最难查的一类损坏：库里既有新卡又有没归档的旧卡，用户看到两条
    自相矛盾的记忆，而且没有任何报错。
    """
    store = SqliteStore(_db())
    store.apply("alice", [{"op": "add", "card": {"id": "m_1", "summary": "在的"}}],
                idempotency_key="seed")
    before = {c["id"]: dict(c) for c in store.load("alice").cards}

    with pytest.raises(KeyError):
        store.apply("alice", [
            {"op": "add", "card": {"summary": "这条本来能成"}},
            {"op": "supersede", "target_id": "不存在的卡",
             "card": {"summary": "这条会失败"}},
        ], idempotency_key="mixed")

    after = {c["id"]: dict(c) for c in store.load("alice").cards}
    assert after == before, f"失败的批次留下了东西：{after}"


# --------------------------------------------------------------------------- #
# E. 权限隔离
# --------------------------------------------------------------------------- #

BOB = Scope(tenant_id="bob", actor=Actor(user_id="bob", agent_id="b1"))


def test_E_one_tenant_cannot_read_another():
    """A 记的东西，B 读不到。"""
    store = SqliteStore(_db())
    garden = MountedGarden(model=_Model(_card_reply(SPICY)), store=store,
                           selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))))
    garden.capture_and_store(ALICE, CaptureRequest(
        window="用户：我不吃辣", locale="zh-Hans"))

    assert garden.context_for_turn(ALICE, "晚饭").record_ids
    assert not garden.context_for_turn(BOB, "晚饭").record_ids, "B 读到了 A 的记忆"

    found = garden.invoke_tool(BOB, ToolCall(
        name="memory_search", arguments={"query": "辣"}))
    assert found.content == "", f"工具让 B 搜到了 A 的东西：{found.content!r}"


def test_E_the_model_cannot_widen_its_own_scope_through_tool_arguments():
    """🔴 模型在工具参数里写别人的 actor / mount —— 必须完全无效。

    这是权限系统最直接的攻击面：工具参数是模型生成的文本，它想写什么写什么。
    唯一安全的做法是**从头到尾不读它**。
    """
    store = SqliteStore(_db())
    garden = MountedGarden(model=_Model(_card_reply(SPICY)), store=store,
                           selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))))
    garden.capture_and_store(ALICE, CaptureRequest(
        window="用户：我不吃辣", locale="zh-Hans"))

    # 模型伪造：我是 alice，允许所有 mount
    forged = ToolCall(
        name="memory_search",
        arguments={"query": "辣"},
        actor=Actor(user_id="alice", agent_id="a1"),
        mounts=("agent-private", "user-private", "family-shared"),
    )
    out = garden.invoke_tool(BOB, forged)   # 可信作用域仍是 BOB
    assert out.content == "", f"工具参数伪造生效了：{out.content!r}"


def test_E_writing_to_a_mount_outside_the_scope_is_refused():
    """越权写入必须拒绝，而不是悄悄落到默认 mount 上。"""
    garden = _garden(_card_reply(SPICY))
    with pytest.raises(MountPermissionError):
        garden.capture_and_store(
            Scope(tenant_id="alice", allowed_mounts=("agent-private",)),
            CaptureRequest(window="x", locale="zh-Hans", mount="family-shared"),
        )


def test_E_an_empty_allowed_mounts_does_not_mean_everything():
    """允许列表为空 → 退化成只有默认 mount，**不是「都可以」**。

    把空理解成通配是权限系统最经典的翻车方式。
    """
    scope = Scope(tenant_id="alice", allowed_mounts=())
    assert scope.mounts() == ("agent-private",)
    with pytest.raises(MountPermissionError):
        scope.check("family-shared")


def test_E_export_only_covers_the_callers_own_data():
    store = SqliteStore(_db())
    garden = MountedGarden(model=_Model(_card_reply(SPICY)), store=store,
                           selection_policy=Chain(stages=(RelevanceStage(limit=8), RecentStage(limit=4))))
    garden.capture_and_store(ALICE, CaptureRequest(
        window="用户：我不吃辣", locale="zh-Hans"))

    assert garden.export(ALICE).counts.get("total", 0) >= 1
    assert garden.export(BOB).counts.get("total", 0) == 0, "B 导出到了 A 的数据"


def test_B_maintenance_refuses_to_guess_the_garden_language():
    """不给 locale 就报错，**不猜**。

    猜错的表现是整理完之后整个花园换了语言，而且不报错 —— 2026-08-24
    那次线上事故就是这个形状。宁可让调用方明确给。
    """
    garden = _garden()
    with pytest.raises(ValueError, match="locale"):
        garden.run_and_store_maintenance(ALICE, MaintenanceRequest())


def test_E_a_card_in_an_unauthorised_mount_is_invisible():
    """同一个租户下，**没授权的 mount 里的卡也读不到**。

    这条是补上来的：先前只测了跨租户，而跨租户靠 `store.load(tenant)` 分表
    就挡住了 —— 把 mount 过滤整段删掉，那些用例照样绿。真正需要 mount 过滤的
    是这个场景：同一个人的 family-shared 记忆，不该被只授权 agent-private 的
    agent 读到。
    """
    store = SqliteStore(_db())
    store.apply("alice", [
        {"op": "add", "card": {"id": "p_1", "summary": "私有的事",
                               "mount": "agent-private"}},
        {"op": "add", "card": {"id": "f_1", "summary": "家庭共享的事",
                               "mount": "family-shared"}},
    ], idempotency_key="seed")

    garden = MountedGarden(model=_Model(), store=store,
                           selection_policy=Chain(stages=(RecentStage(limit=8),)))

    private_only = Scope(tenant_id="alice", allowed_mounts=("agent-private",))
    ctx = garden.context_for_turn(private_only, "有什么事")
    assert "f_1" not in ctx.record_ids, "读到了没授权 mount 里的卡"
    assert "p_1" in ctx.record_ids, "自己 mount 里的卡反而读不到"

    found = garden.invoke_tool(private_only, ToolCall(
        name="memory_search", arguments={"query": "事"}))
    assert "家庭共享" not in found.content, f"工具漏了没授权的 mount：{found.content!r}"
