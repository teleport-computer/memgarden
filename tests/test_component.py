"""GardenComponent —— 顶层接口的行为。

## 这些测试守什么

不是「函数按写的那样跑了吗」，是**接入方看到的行为对不对**：

  · 一次调用完成一件事，不需要知道内部有哪些零件
  · 「没什么可记」和「出错了」必须分得开 —— 混淆的代价是游标推进、
     那批对话永远不会再被看一眼
  · 判断在里面、执行在外面：返回的是改动指令，不是「已经写好了」
  · 缺能力时明确声明，不让宿主猜
"""
from __future__ import annotations

import json

import pytest

from memgarden import (
    CaptureRequest,
    CuratedWriteRequest,
    ExportRequest,
    ImportRequest,
    PromoteRequest,
    ContextRequest,
    GardenComponent,
    MaintenanceRequest,
    ToolCall,
)
from memgarden.selection import Chain, RecentStage


class FakeModel:
    """按顺序吐预设回复；用完之后一直吐最后一条。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["{}"]
        self.calls = 0
        self.purposes: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.calls += 1
        self.purposes.append(purpose)
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def _cards_reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


GOOD_CARD = {
    "action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
    "summary": "他不吃辣", "content": "一吃辣就胃疼，点菜要避开辣的。",
    "importance": 0.6, "pulse": 0.3,
}


# --------------------------------------------------------------- 接入体验

def test_capture_takes_one_call_and_no_internal_imports() -> None:
    """接入方只需要 GardenComponent 和一个请求对象。

    这条是整层存在的理由：在它之前，落一次卡要宿主自己知道
    build_capture_prompt → 调模型 → parse_capture_cards → 过闸 → 归一化
    这一串，而这份说明书从来没写下来过。
    """
    garden = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD)))
    result = garden.capture(CaptureRequest(window="用户：我不吃辣", locale="zh-Hans"))

    assert len(result.mutations) == 1
    assert result.mutations[0]["op"] == "add"
    assert result.error is None


def test_the_model_is_told_what_the_call_is_for() -> None:
    """宿主可能想给 capture 和 dream 分不同的模型（便宜的 / 强的）。"""
    model = FakeModel(_cards_reply(GOOD_CARD))
    GardenComponent(model=model).capture(CaptureRequest(window="x", locale="zh-Hans"))
    assert model.purposes == ["capture"]


# --------------------------------------------------------------- 判断 vs 执行

def test_capture_returns_instructions_not_writes() -> None:
    """内核不写库。它没有存储、没有密钥，也不该有。"""
    garden = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD)))
    result = garden.capture(CaptureRequest(window="x", locale="zh-Hans"))
    assert "card" in result.mutations[0], "返回的应该是「该这么改」，不是已写好的记录"


def test_tool_writes_are_also_only_instructions() -> None:
    garden = GardenComponent(model=FakeModel())
    out = garden.invoke_tool(ToolCall(
        name="memory_write",
        arguments={"summary": "他不吃辣", "content": "一吃辣就胃疼。"},
    ))
    assert out.ok and out.mutations and out.mutations[0]["op"] == "add"


def test_search_tool_says_it_needs_the_host_store() -> None:
    """搜索要读库，而库在宿主手里 —— 明说，别假装能搜。"""
    out = GardenComponent(model=FakeModel()).invoke_tool(ToolCall(name="memory_search"))
    assert not out.ok and out.error == "search_requires_host_store"


# --------------------------------------------------- 「没什么可记」≠「出错了」

def test_nothing_worth_keeping_is_not_an_error() -> None:
    """闲聊本来就不该落卡。这是正常结果，宿主该推进游标。"""
    garden = GardenComponent(model=FakeModel(_cards_reply()))
    result = garden.capture(CaptureRequest(window="哈哈哈", locale="zh-Hans"))
    assert result.nothing_worth_keeping and result.error is None


def test_a_parse_failure_is_never_reported_as_nothing_to_keep() -> None:
    """🔴 混淆这两者的代价：宿主以为「没什么可记」→ 推进游标 →
    **这批对话永远不会再被看一眼**，用户说过的话凭空消失。"""
    garden = GardenComponent(model=FakeModel("这不是 JSON"))
    result = garden.capture(CaptureRequest(window="重要的话", locale="zh-Hans"))
    assert result.error is not None
    assert not result.nothing_worth_keeping


# --------------------------------------------------------------- 重问

def test_a_placeholder_card_gets_one_corrective_call() -> None:
    """模型吐了占位符 → 打回重问一次 → 第二次干净就用第二次的。"""
    model = FakeModel(
        _cards_reply({"action": "add", "summary": "[摘要]", "content": "...",
                      "bucket": "工作"}),
        _cards_reply(GOOD_CARD),
    )
    result = GardenComponent(model=model).capture(
        CaptureRequest(window="x", locale="zh-Hans")
    )
    assert model.calls == 2 and result.retried == 1
    assert len(result.mutations) == 1 and result.error is None


def test_supersede_without_a_target_is_asked_again() -> None:
    """本地就能证明是错的：说要覆盖旧卡，却没说覆盖哪张。"""
    model = FakeModel(
        _cards_reply({**GOOD_CARD, "action": "supersede", "target_id": ""}),
        _cards_reply({**GOOD_CARD, "action": "supersede", "target_id": "m_7"}),
    )
    result = GardenComponent(model=model).capture(
        CaptureRequest(window="x", locale="zh-Hans")
    )
    assert result.retried == 1
    assert result.mutations[0]["target_id"] == "m_7"


def test_retries_are_bounded() -> None:
    """模型一直吐脏东西时不能无限烧钱。"""
    bad = _cards_reply({"action": "add", "summary": "[摘要]", "content": "...",
                        "bucket": "工作"})
    model = FakeModel(bad, bad, bad, bad)
    result = GardenComponent(model=model, max_capture_retries=1).capture(
        CaptureRequest(window="x", locale="zh-Hans")
    )
    assert model.calls <= 2
    assert result.error is not None


# --------------------------------------------------------------- locale

def test_locale_has_no_default() -> None:
    """默认成某种语言，等于把一套分类法硬塞给所有使用者 ——
    而他不会知道自己的库里为什么长出了中文桶。"""
    result = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD))).capture(
        CaptureRequest(window="x")
    )
    assert result.error == "locale_required"


# --------------------------------------------------------------- 挑卡

def test_context_reports_which_stage_picked_each_card() -> None:
    """「为什么想不起来」只有这条线索。没有它，用户说「它忘了我的狗」时，
    查不出是没召回、还是根本没落库。"""
    cards = [
        {"id": "m_1", "summary": "他不吃辣", "created_at": "2026-08-01"},
        {"id": "m_2", "summary": "养了只柯基", "created_at": "2026-08-20"},
    ]
    garden = GardenComponent(
        model=FakeModel(),
        selection_policy=Chain(stages=(RecentStage(limit=2, order_by="created_at"),)),
    )
    out = garden.build_context(ContextRequest(query="狗", candidates=cards, limit=2))
    assert set(out.record_ids) == {"m_1", "m_2"}
    assert out.trace["by_stage"]["m_2"] == "recent"


def test_without_a_policy_the_component_says_it_cannot_do_context() -> None:
    """缺能力要**明确声明**，不能让宿主以为它会自动塞记忆、然后一直等。"""
    garden = GardenComponent(model=FakeModel())
    assert garden.capabilities().turn_context is False
    out = garden.build_context(ContextRequest(query="x", candidates=[]))
    assert out.trace.get("skipped") == "no_selection_policy"


# --------------------------------------------------------------- 整理

def _many_cards(n: int) -> list[dict]:
    return [{"id": f"m_{i}", "summary": f"卡{i}", "content": "x",
             "created_at": "2026-08-01"} for i in range(n)]


def test_dry_run_answers_without_burning_a_model_call() -> None:
    """调度器只想问「该整理了吗」，不该为此烧一次模型调用。"""
    model = FakeModel()
    out = GardenComponent(model=model).run_maintenance(
        MaintenanceRequest(cards=_many_cards(15), locale="zh-Hans", dry_run=True)
    )
    assert out.needed is True and model.calls == 0


def test_the_ledger_stops_it_from_dreaming_the_same_garden_twice() -> None:
    """整理完把签名存回账本，同一批卡就不会再整理一次。

    没有这条，每次调度都会重整一遍 —— 烧钱，而且反复改写用户的记忆。
    """
    cards = _many_cards(15)
    first = GardenComponent(model=FakeModel()).run_maintenance(
        MaintenanceRequest(cards=cards, locale="zh-Hans", dry_run=True)
    )
    again = GardenComponent(model=FakeModel()).run_maintenance(
        MaintenanceRequest(
            cards=cards, locale="zh-Hans", dry_run=True,
            last_signature=first.trace["signature"],
            last_seed_card_count=first.trace["seed_card_count"],
        )
    )
    assert again.needed is False
    assert again.trace["reason"] == "already_dreamed"


def test_maintenance_uses_the_dream_model_slot() -> None:
    model = FakeModel(json.dumps({"consolidations": []}))
    GardenComponent(model=model).run_maintenance(
        MaintenanceRequest(cards=_many_cards(15), locale="zh-Hans")
    )
    assert "dream" in model.purposes


# --------------------------------------------------------------- 观测

def test_trace_carries_no_user_content() -> None:
    """观测记录会进日志。**用户内容一个字都不能出现在里面。**"""
    secret = "他在国贸三期上班，女儿叫朵朵"
    garden = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD)))
    result = garden.capture(CaptureRequest(window=secret, locale="zh-Hans"))
    blob = json.dumps(result.trace, ensure_ascii=False)
    for fragment in ("国贸", "朵朵", "他在"):
        assert fragment not in blob


# --------------------------------------------------------------- 异步

class AsyncFakeModel:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["{}"]
        self.calls = 0

    async def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_async_capture_matches_sync_exactly() -> None:
    """同步和异步必须产出同一个结果。

    各写一份编排的话，改了一边忘了另一边，两条路会**悄悄**产出不同的卡 ——
    而这种分岔只有在真模型上才看得见。所以它们共用同一个状态机，
    这条测试守着它们没有分家。
    """
    reply = _cards_reply(GOOD_CARD)
    sync = GardenComponent(model=FakeModel(reply)).capture(
        CaptureRequest(window="用户：我不吃辣", locale="zh-Hans")
    )
    a = _run(GardenComponent(model=AsyncFakeModel(reply)).acapture(
        CaptureRequest(window="用户：我不吃辣", locale="zh-Hans")
    ))
    assert a.mutations == sync.mutations
    assert a.retried == sync.retried and a.error == sync.error


def test_async_capture_retries_the_same_way() -> None:
    bad = _cards_reply({"action": "add", "summary": "[摘要]", "content": "...",
                        "bucket": "工作"})
    good = _cards_reply(GOOD_CARD)
    model = AsyncFakeModel(bad, good)
    out = _run(GardenComponent(model=model).acapture(
        CaptureRequest(window="x", locale="zh-Hans")
    ))
    assert model.calls == 2 and out.retried == 1 and len(out.mutations) == 1


def test_async_capture_separates_empty_from_failed() -> None:
    empty = _run(GardenComponent(model=AsyncFakeModel(_cards_reply())).acapture(
        CaptureRequest(window="哈哈哈", locale="zh-Hans")))
    assert empty.nothing_worth_keeping and empty.error is None

    broken = _run(GardenComponent(model=AsyncFakeModel("这不是 JSON")).acapture(
        CaptureRequest(window="重要的话", locale="zh-Hans")))
    assert broken.error and not broken.nothing_worth_keeping


# --------------------------------------------------------------- 过程可观测

def test_the_host_can_see_every_step() -> None:
    """把编排收进组件之后，宿主**不能因此丢掉它原本看得见的东西**。

    io 现在记着「第一次问了什么、模型回了什么、为什么重问」。换成组件之后
    如果看不到，可观测性就是净退步 —— 那样这层门面是亏的。
    """
    steps = []
    bad = _cards_reply({"action": "add", "summary": "[摘要]", "content": "...",
                        "bucket": "工作"})
    garden = GardenComponent(model=FakeModel(bad, _cards_reply(GOOD_CARD)),
                             on_step=steps.append)
    garden.capture(CaptureRequest(window="用户：我不吃辣", locale="zh-Hans"))

    kinds = [s.kind for s in steps]
    assert kinds == ["prompt_built", "model_called", "parsed", "retrying",
                     "model_called", "parsed", "done"]
    # 重问的**原因**要说得出来，否则查不出「为什么这轮多花了一次调用」
    assert any(s.kind == "retrying" and s.detail.get("why") for s in steps)


def test_step_details_carry_no_user_content() -> None:
    """``detail`` 会进日志 —— 只放长度、计数、错误码。

    提示词和模型回复通过单独的字段给，宿主自己决定要不要落库、要不要脱敏。
    """
    steps = []
    secret = "他在国贸三期上班，女儿叫朵朵"
    GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD)),
                    on_step=steps.append).capture(
        CaptureRequest(window=secret, locale="zh-Hans"))
    blob = json.dumps([s.detail for s in steps], ensure_ascii=False)
    for fragment in ("国贸", "朵朵"):
        assert fragment not in blob


def test_the_raw_prompt_is_available_but_separate() -> None:
    """宿主要记完整轨迹时得拿得到原文 —— 但它不在 detail 里，是显式的另一个字段。"""
    steps = []
    GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD)),
                    on_step=steps.append).capture(
        CaptureRequest(window="用户：我不吃辣", locale="zh-Hans"))
    built = next(s for s in steps if s.kind == "prompt_built")
    assert built.prompt and "不吃辣" in built.prompt
    assert "不吃辣" not in json.dumps(built.detail, ensure_ascii=False)


def test_a_broken_step_callback_never_breaks_capture() -> None:
    """记轨迹失败不该让落卡失败 —— 观测是附加品，不是前置条件。"""
    def explode(step):
        raise RuntimeError("宿主的日志系统挂了")

    result = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD)),
                             on_step=explode).capture(
        CaptureRequest(window="x", locale="zh-Hans"))
    assert result.mutations and result.error is None


def test_async_capture_reports_the_same_steps() -> None:
    """异步版不能少报步骤 —— 否则 V2 换过去之后轨迹会缺。"""
    reply = _cards_reply(GOOD_CARD)
    sync_steps, async_steps = [], []
    GardenComponent(model=FakeModel(reply), on_step=sync_steps.append).capture(
        CaptureRequest(window="x", locale="zh-Hans"))
    _run(GardenComponent(model=AsyncFakeModel(reply),
                         on_step=async_steps.append).acapture(
        CaptureRequest(window="x", locale="zh-Hans")))
    assert [s.kind for s in sync_steps] == [s.kind for s in async_steps]


def test_capture_gives_both_mutations_and_raw_cards() -> None:
    """两种表达同一批卡 —— 宿主有自己的写入格式时用 cards。

    io 的 action 带加密信封和通话溯源，要从卡本身构造；而 mutations 已经把
    action/target_id 拆到外层。逼它拆回去再拼一遍是无谓往返，还容易丢字段。
    """
    out = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD))).capture(
        CaptureRequest(window="x", locale="zh-Hans"))
    assert len(out.cards) == len(out.mutations) == 1
    assert out.cards[0]["action"] == "add"          # 原始卡保留 action
    assert "action" not in out.mutations[0]["card"]  # 指令里拆到了外层 op
    assert out.cards[0]["summary"] == out.mutations[0]["card"]["summary"]


# --------------------------------------------------------------- 截断

class TruncatingModel:
    """第一次返回被截断的回复，第二次正常。

    截断标记由**宿主**给 —— 内核看不到 provider 的 finish_reason。
    """

    def __init__(self, good: str) -> None:
        self.good = good
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> dict:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return {"text": '{"cards":[{"action":"add","summary":"他不吃', "truncated": True}
        return {"text": self.good, "truncated": False}


def test_a_truncated_reply_is_asked_again_with_a_shorter_prompt() -> None:
    """回复被截断时原样重问多半还是会截在同一个位置 —— 要换更简短的提示词。

    这是宿主 io 一直在做的事（provider 层能看到 finish_reason）。收进组件时
    不能把它丢掉，否则半截 JSON 会被当成解析失败、整个窗口的落卡清零。
    """
    model = TruncatingModel(_cards_reply(GOOD_CARD))
    steps = []
    out = GardenComponent(model=model, on_step=steps.append).capture(
        CaptureRequest(window="用户：我不吃辣", locale="zh-Hans"))

    assert model.calls == 2
    assert model.prompts[1] != model.prompts[0], "第二问该换一版更简短的提示词"
    assert out.mutations and out.error is None
    assert any(s.detail.get("why") == "output_truncated" for s in steps)


def test_a_plain_string_reply_still_works() -> None:
    """宿主不关心截断时，返回纯字符串即可 —— 不必为了报一个 bool 改整套调用链。"""
    out = GardenComponent(model=FakeModel(_cards_reply(GOOD_CARD))).capture(
        CaptureRequest(window="x", locale="zh-Hans"))
    assert out.mutations and out.error is None


# --------------------------------------------------- 三种写入来源语义不同

def test_curated_write_never_judges_whether_it_is_worth_keeping() -> None:
    """用户明说「记一下」时，我们的活是记好，不是评估该不该记。

    拿自动落卡那把克制的尺子来量，模型会判「这不值得」然后什么都不发生 ——
    用户以为记住了，其实没有，而且没有任何错误可查。
    所以这条路**根本不调模型做筛选**。
    """
    model = FakeModel(_cards_reply())          # 模型说「没什么可记」
    out = GardenComponent(model=model).write_one(CuratedWriteRequest(
        text="我不吃辣，一吃就胃疼，点菜都得避开", locale="zh-Hans"))
    assert model.calls == 0, "用户明说的事不该拿去问模型值不值得记"
    assert len(out.cards) == 1 and out.error is None


def test_curated_write_still_passes_the_content_gate() -> None:
    """不判断「值不值得」，但仍然要是**真内容** —— 空白和占位符照样拦。"""
    out = GardenComponent(model=FakeModel()).write_one(
        CuratedWriteRequest(text="   ", locale="zh-Hans"))
    assert out.error == "empty_text"


def test_import_declares_it_lacks_its_own_ruler() -> None:
    """能力声明必须说实话。

    ``policies`` 里有 history_import 档，但提示词模板只实现了
    conversation_capture 的结构。声明成 False 而不是假装支持 ——
    后者的表现是「导入成功但几乎没记住什么」，用户和宿主都查不出原因。
    """
    caps = GardenComponent(model=FakeModel()).capabilities()
    assert caps.history_import is False


def test_import_refuses_an_unsupported_ruler_instead_of_silently_downgrading() -> None:
    out = GardenComponent(model=FakeModel()).import_history(
        ImportRequest(material="x", locale="zh-Hans", policy="history_import"))
    assert out.error and out.error.startswith("policy_not_supported")


def test_import_failure_is_never_reported_as_nothing_to_keep() -> None:
    """导入失败必须是失败。报成「没什么可记」的话，用户交出三年记录、
    看到「导入完成」、然后一条都没有 —— 且没有任何错误可查。"""
    many = _cards_reply(*[{
        "action": "add", "summary": f"第 {i} 件事",
        "content": f"这是第 {i} 段有实质内容的正文，长度足够通过内容闸。",
        "bucket": "工作"} for i in range(60)])
    out = GardenComponent(model=FakeModel(many)).import_history(
        ImportRequest(material="三年的聊天记录", locale="zh-Hans"))
    assert out.error is not None
    assert not out.nothing_worth_keeping


# --------------------------------------------------------------- 导出 / 提升

def test_export_includes_archived_by_default() -> None:
    """导出是用户的权利。「你删掉的那条我还留着」和「你看不到自己删过什么」
    都是不该有的状态。"""
    out = GardenComponent(model=FakeModel()).export(
        ExportRequest(),
        [{"record_id": "m_1", "lifecycle": "active"},
         {"record_id": "m_2", "lifecycle": "archived"},
         {"record_id": "m_3", "superseded_by": "m_4"}])
    assert out.counts == {"total": 3, "archived": 1, "superseded": 1}


def test_promote_requires_the_host_to_have_authorized_it() -> None:
    """内核不自行授予权限。没有宿主的校验结论就拒绝，而不是「大概可以吧」。"""
    garden = GardenComponent(model=FakeModel())
    denied = garden.promote(PromoteRequest(record_id="m_1", to_mount="family-shared"))
    assert not denied.ok and denied.error == "not_authorized_by_host"

    ok = garden.promote(PromoteRequest(record_id="m_1", to_mount="family-shared",
                                       authorized=True))
    assert ok.ok and ok.mutations[0]["changes"]["mount"] == "family-shared"


# --------------------------------------------------------------- 展示投影

def test_browse_projection_survives_a_component_without_buckets() -> None:
    """一个不产出桶的组件接进来，通用列表页**不能是空白** ——
    那看起来像数据丢了。没有分组就平铺。"""
    items = GardenComponent(model=FakeModel()).browse([
        {"id": "m_1", "summary": "他不吃辣", "bucket": "偏好与边界", "threads": ["饮食"]},
        {"id": "x_9", "summary": "某条向量记忆"},          # 没有桶
    ])
    assert all(i.record_ref and i.display_text and i.mount for i in items)
    assert items[0].group_label == "偏好与边界"
    assert items[1].group_label == "", "没有桶时留空，由 UI 平铺或显示「未分类」"


# --------------------------------------------- 宿主驱动的形状（自带 provider）

def test_a_host_driven_session_matches_the_built_in_loop() -> None:
    """自带循环和宿主驱动必须产出同一个结果。

    各写一份状态机的话，两种形状会**悄悄**产出不同的卡 —— 而宿主以为
    它们是同一个东西。共用一份，这条测试守着没分家。
    """
    replies = [_cards_reply({"action": "add", "summary": "[摘要]",
                             "content": "...", "bucket": "工作"}),
               _cards_reply(GOOD_CARD)]

    built_in = GardenComponent(model=FakeModel(*replies)).capture(
        CaptureRequest(window="x", locale="zh-Hans"))

    session = GardenComponent(model=FakeModel()).capture_session(
        CaptureRequest(window="x", locale="zh-Hans"))
    i = 0
    while (prompt := session.next_prompt()) is not None:
        session.feed(replies[min(i, len(replies) - 1)])
        i += 1
    driven = session.result()

    assert driven.cards == built_in.cards
    assert driven.error == built_in.error
    assert driven.retried == built_in.retried


def test_a_host_driven_session_can_report_truncation() -> None:
    """截断只有宿主看得见（要看 finish_reason）—— 组件收到通知后换一版
    更简短的提示词重问，而不是原样重问（原样多半还是被截在同一处）。"""
    session = GardenComponent(model=FakeModel()).capture_session(
        CaptureRequest(window="x", locale="zh-Hans"))
    first = session.next_prompt()
    session.feed("半截 JSON{", truncated=True)
    second = session.next_prompt()
    assert second is not None and second != first, "截断后应该换一版提示词"


def test_a_session_with_a_bad_request_ends_immediately() -> None:
    session = GardenComponent(model=FakeModel()).capture_session(
        CaptureRequest(window="x"))          # 没给 locale
    assert session.next_prompt() is None
    assert session.result().error == "locale_required"
