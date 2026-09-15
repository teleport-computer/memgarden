"""落卡时宿主交「现有卡」—— 索引由组件挑、target_id 只许指向真卡。

## 守什么

宿主以前只能交一段渲染好的索引串（``CaptureRequest.cards``）。有两个坏法，都不报错：

  · 宿主漏渲染 → 提示词里索引是 ``(none)`` → 模型**永远只会 add**，同一件事
    说两次就是两张卡（io 生产上 2026-08-30 起就是这样）。
  · 模型抄错/编一个 id → 内核判不了 → 整批原子提交的宿主把同窗口的好卡一起拒掉。

``existing_cards`` 交的是整批卡：组件自己按这段对话挑索引（相关的优先），并且
知道哪些 id 是真的。不交（``None``）时行为逐字不变。
"""
from __future__ import annotations

import json

from memgarden import CaptureRequest, GardenComponent
from memgarden.rendering import render_card_index_budgeted


class FakeModel:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


def _card(action: str, target: str | None, summary: str) -> dict:
    return {
        "action": action, "type": "fact", "target_id": target, "bucket": "工作",
        "threads": ["工作"], "summary": summary,
        "content": f"{summary}，这是他亲口说的，之后聊工作要以这个为准。",
        "importance": 0.7, "pulse": 0.4,
    }


FILLER = [
    {"id": f"mom_fill_{i:03d}", "summary": f"第{i}次闲聊提到的天气和午饭", "bucket": "日常",
     "importance": 0.2}
    for i in range(120)
]
# 和这段对话无关、但重要度高的卡 —— 只按重要度挑的话，相关的那张会被它们挤掉。
CORE = [
    {"id": f"mom_core_{i:02d}", "summary": f"老王家里的第{i}件大事", "bucket": "家庭",
     "importance": 0.9}
    for i in range(70)
]
JOB = {"id": "mom_job", "summary": "老王在字节跳动做产品经理，负责电商", "bucket": "工作",
       "importance": 0.1}
WINDOW = "老王：我上周从字节跳动离职了，下个月去腾讯做产品经理\n我：恭喜！"


def _garden(model: FakeModel, steps: list | None = None) -> GardenComponent:
    return GardenComponent(model=model, max_capture_retries=1,
                           on_step=(steps.append if steps is not None else None))


def _request(**kw) -> CaptureRequest:
    base = dict(window=WINDOW, locale="zh-Hans", ai_name="io", user_name="老王")
    base.update(kw)
    return CaptureRequest(**base)


def test_index_is_selected_from_existing_cards_relevant_first():
    model = FakeModel(_reply())
    steps: list = []
    result = _garden(model, steps).capture(_request(existing_cards=[*FILLER, *CORE, JOB]))
    prompt = model.prompts[0]
    index = prompt.split("target_id from here)]", 1)[1].split("\n[", 1)[0]
    rows = [line for line in index.splitlines() if line.startswith("- ")]
    # 191 张里只挑 60 张，和这段对话相关的那张（重要度最低）排第一。
    assert len(rows) == 60
    assert rows[0] == "- mom_job: [工作] 老王在字节跳动做产品经理，负责电商"
    assert result.trace["index_candidates"] == 191
    assert result.trace["index_cards"] == 60
    built = [s for s in steps if s.kind == "prompt_built"][0]
    assert built.detail["index_cards"] == 60


def test_without_existing_cards_prompt_is_byte_identical():
    a, b = FakeModel(_reply()), FakeModel(_reply())
    _garden(a).capture(_request(cards="- c1: [工作] 旧卡"))
    _garden(b).capture(_request(cards="- c1: [工作] 旧卡", existing_cards=None))
    assert a.prompts == b.prompts
    assert "- c1: [工作] 旧卡" in a.prompts[0]


def test_host_rendered_cards_win_but_targets_still_validate():
    model = FakeModel(_reply(_card("supersede", "mom_ghost", "老王去腾讯做产品经理")))
    result = _garden(model).capture(
        _request(cards="- mom_job: [工作] 宿主自己渲染的", existing_cards=[JOB]))
    assert "- mom_job: [工作] 宿主自己渲染的" in model.prompts[0]
    assert "负责电商" not in model.prompts[0]
    assert result.cards == []


def test_supersede_of_indexed_card_passes_through():
    model = FakeModel(_reply(_card("supersede", "mom_job", "老王下个月去腾讯做产品经理")))
    result = _garden(model).capture(_request(existing_cards=[*FILLER, JOB]))
    assert len(model.prompts) == 1
    assert [c["target_id"] for c in result.cards] == ["mom_job"]
    assert result.mutations[0]["op"] == "supersede"
    assert result.mutations[0]["target_id"] == "mom_job"


def test_unknown_target_is_reasked_then_fixed():
    model = FakeModel(
        _reply(_card("supersede", "mom_made_up", "老王下个月去腾讯做产品经理")),
        _reply(_card("supersede", "mom_job", "老王下个月去腾讯做产品经理")),
    )
    result = _garden(model).capture(_request(existing_cards=[JOB]))
    assert len(model.prompts) == 2
    assert "你给的 target_id 不是现有的卡" in model.prompts[1]
    assert [c["target_id"] for c in result.cards] == ["mom_job"]
    assert result.error is None


def test_unknown_target_still_unknown_after_reask_drops_only_that_card():
    good = _card("add", None, "老王喜欢周末爬山")
    bad = _card("merge", "mom_made_up", "老王下个月去腾讯做产品经理")
    model = FakeModel(_reply(good, bad), _reply(good, bad))
    steps: list = []
    result = _garden(model, steps).capture(_request(existing_cards=[JOB]))
    assert [c["summary"] for c in result.cards] == ["老王喜欢周末爬山"]
    assert all(m.get("target_id") != "mom_made_up" for m in result.mutations)
    dropped = [s.detail for s in steps if s.kind == "dropped"]
    assert dropped == [{"why": "unknown_target", "cards": 1}]
    assert result.trace["dropped_unknown_target"] == 1


def test_real_card_outside_the_index_budget_is_not_rejected():
    # 校验用全部现有卡，不是挤进索引的那几张。
    model = FakeModel(_reply(_card("supersede", "mom_fill_119", "天气那张的更正")))
    result = _garden(model).capture(
        _request(existing_cards=[*FILLER, JOB], index_cards_limit=5))
    assert "mom_fill_119" not in model.prompts[0]
    assert len(model.prompts) == 1
    assert [c["target_id"] for c in result.cards] == ["mom_fill_119"]


def test_empty_existing_cards_means_no_valid_target():
    card = _card("supersede", "mom_job", "老王下个月去腾讯做产品经理")
    model = FakeModel(_reply(card), _reply(card))
    result = _garden(model).capture(_request(existing_cards=[]))
    assert "target_id from here)](none)" in model.prompts[0]
    assert len(model.prompts) == 2
    assert result.cards == []
    # 对照：不交现有卡时同一张卡原样通过（target 留给宿主写库时校验）。
    control = FakeModel(_reply(card))
    assert [c["target_id"] for c in _garden(control).capture(_request()).cards] == ["mom_job"]


def test_recapture_with_feedback_keeps_index_and_validation():
    bad = _card("supersede", "mom_made_up", "老王下个月去腾讯做产品经理")
    good = _card("supersede", "mom_job", "老王下个月去腾讯做产品经理")
    model = FakeModel(_reply(bad, good))
    result = _garden(model).recapture_with_feedback(
        _request(existing_cards=[JOB]), ["target_id 不属于这个人"])
    assert "- mom_job: [工作]" in model.prompts[0]
    assert [c["target_id"] for c in result.cards] == ["mom_job"]


def test_budgeted_render_keeps_order_clips_and_is_one_line():
    cards = [
        {"id": "b", "summary": "第二\n行", "importance": 0.1},
        {"id": "a", "summary": "x" * 50, "bucket": "工 作", "importance": 0.9},
        {"id": "", "summary": "no id"},
        {"id": "c", "summary": "三", "importance": 0.5},
    ]
    text, ids = render_card_index_budgeted(cards, budget_chars=10_000, summary_chars=10)
    assert ids == ["b", "a", "c"]
    assert text.splitlines() == ["- b: 第二 行", "- a: [工 作] " + "x" * 9 + "…", "- c: 三"]
    text, ids = render_card_index_budgeted(cards, budget_chars=len("- b: 第二 行") + 3,
                                           summary_chars=10)
    assert ids == ["b"]
