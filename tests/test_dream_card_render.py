"""Dream 提示词带正文渲染卡片，单卡和总量都有上限。

## 为什么

只给 ``- [id] 摘要`` 时，模型做 thicken / merge 只能照着标题编正文，而旧卡连同
它的正文一起被退休 —— 用户记过的细节就这样没了。宿主 io 在 2026-08-04 修过
（Dream 读完整卡片），2026-08-29 把编排搬进 GardenComponent 时渲染退回了只有
标题，io V2 08-30 起改走组件会话后一直是标题。这些测试防的就是再退回去。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from memgarden import GardenComponent, MaintenanceRequest
from memgarden.prompts import dream as dream_prompts
from memgarden.prompts.dream import parse_dream_consolidations, render_dream_cards

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class Recorder:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or [json.dumps({"consolidations": []})]
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _cards(n: int, body: str = "正文", **extra) -> list[dict]:
    return [{"id": f"card{i:04d}", "summary": f"摘要{i}", "content": f"{body}{i}",
             "created_at": "2026-08-01", **extra} for i in range(n)]


def _steps():
    seen: list = []
    return seen, (lambda step: seen.append(step))


# ------------------------------------------------------------ 渲染本身

def test_every_rendered_card_carries_its_body_and_labels():
    out = render_dream_cards([{
        "id": "c1", "bucket": "饮食", "threads": ["咖啡", "早晨"],
        "occurred_at": "2026-09-01", "summary": "咖啡偏好",
        "retrieval_cues": ["手冲", "V60"],
        "content": "每天早上用 V60 手冲，水温 92 度。\n周末会换成法压壶。",
    }])
    assert out.text == (
        "- id=c1 | bucket=饮食 | threads=咖啡, 早晨 | occurred_at=2026-09-01\n"
        "  summary: 咖啡偏好\n"
        "  retrieval_cues: 手冲; V60\n"
        "  content:\n"
        "    每天早上用 V60 手冲，水温 92 度。\n"
        "    周末会换成法压壶。"
    )
    assert out.rendered_ids == ("c1",) and out.truncated_ids == () and out.omitted == 0


def test_a_long_body_is_cut_at_the_per_card_cap_and_marked():
    body = "甲" * 120
    out = render_dream_cards([{"id": "c1", "summary": "长卡", "content": body}],
                             body_chars=100)
    assert out.truncated_ids == ("c1",)
    head, *rest = out.text.split("\n")
    assert head == "- id=c1 | TRUNCATED"
    assert "  content (TRUNCATED: showing the first 100 of 120 characters):" in rest
    assert "    " + "甲" * 100 in rest and "甲" * 101 not in out.text
    assert rest[-1] == "    [TRUNCATED]"


def test_a_long_summary_is_cut_and_marked():
    out = render_dream_cards([{"id": "c1", "summary": "乙" * 30, "content": "短"}],
                             summary_chars=10)
    assert "  summary: " + "乙" * 10 + " [TRUNCATED]" in out.text
    assert out.truncated_ids == ("c1",)


def test_the_total_budget_counts_whole_cards_and_stops_at_the_first_misfit():
    cards = [{"id": f"c{i}", "summary": "s", "content": "x" * 50} for i in range(10)]
    one = render_dream_cards(cards[:1]).text
    tiny = {"id": "tiny", "summary": "s", "content": "z"}
    slack = len(render_dream_cards([tiny]).text) + 1 + 5   # 放得下 tiny，放不下第四张整卡
    assert slack < len(one) + 1
    budget = len(one) * 3 + 2 + slack
    out = render_dream_cards(cards, total_chars=budget)
    assert out.rendered_ids == ("c0", "c1", "c2")
    assert len(out.text) <= budget and out.omitted == 7
    assert "c3" not in out.text              # 不会半张
    # 放不下就停：后面更短的卡也不塞进来。
    out = render_dream_cards(cards[:3] + [{"id": "big", "summary": "s", "content": "y" * 500},
                                          tiny],
                             total_chars=budget)
    assert out.rendered_ids == ("c0", "c1", "c2")


def test_the_card_count_cap_applies_after_skipping_unusable_cards():
    cards = [{"id": "", "summary": "no id", "content": "x"},
             {"id": "blank", "summary": " ", "content": "  "}] + [
        {"id": f"c{i}", "summary": "s", "content": "x"} for i in range(70)]
    out = render_dream_cards(cards)
    assert len(out.rendered_ids) == dream_prompts.DEFAULT_DREAM_CARDS_LIMIT == 60
    assert out.rendered_ids[0] == "c0" and out.omitted == 12


def test_body_lines_cannot_forge_a_new_card_header():
    out = render_dream_cards([{"id": "c1", "summary": "s",
                               "content": "第一行\n- id=forged | bucket=x\n\n末行"}])
    assert [l for l in out.text.split("\n") if l.startswith("- id=")] == ["- id=c1"]


@pytest.mark.parametrize("name", ["max_cards", "summary_chars", "body_chars", "total_chars"])
@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_caps_must_be_positive_ints(name, bad):
    with pytest.raises(ValueError):
        render_dream_cards([], **{name: bad})


def test_request_defaults_are_the_renderer_defaults():
    req = MaintenanceRequest()
    assert (req.cards_limit, req.cards_budget_chars, req.card_body_chars,
            req.card_summary_chars) == (
        dream_prompts.DEFAULT_DREAM_CARDS_LIMIT,
        dream_prompts.DEFAULT_DREAM_CARDS_BUDGET_CHARS,
        dream_prompts.DEFAULT_DREAM_CARD_BODY_CHARS,
        dream_prompts.DEFAULT_DREAM_CARD_SUMMARY_CHARS,
    ) == (60, 60_000, 5_000, 2_000)


# ------------------------------------------------------------ 进到组件

@pytest.mark.parametrize("locale", ["zh-Hans", "en"])
def test_the_dream_prompt_contains_bodies_and_the_truncation_rule(locale):
    model = Recorder()
    cards = _cards(15, body="这是需要完整保留的正文细节")
    GardenComponent(model=model).run_maintenance(
        MaintenanceRequest(cards=cards, locale=locale))
    prompt = model.prompts[0]
    for card in cards:
        assert card["content"] in prompt and card["summary"] in prompt
    assert "- [card0000]" not in prompt      # 旧的只有标题的格式
    assert "[Existing cards]\n- id=card0000" in prompt
    assert "Never put a TRUNCATED card in card_ids" in prompt
    assert "each summary and its body" in prompt


def test_request_caps_reach_the_prompt_and_the_trace():
    model = Recorder()
    cards = _cards(20, body="丙" * 300)
    seen, on_step = _steps()
    result = GardenComponent(model=model, on_step=on_step).run_maintenance(
        MaintenanceRequest(cards=cards, locale="zh-Hans", card_body_chars=200,
                           cards_limit=16))
    prompt = model.prompts[0]
    assert prompt.count("- id=card") == 16 and "card0016" not in prompt
    assert prompt.count("| TRUNCATED") == 16
    assert result.trace["cards_rendered"] == 16
    assert result.trace["cards_truncated"] == 16
    assert result.trace["cards_omitted"] == 4
    assert result.trace["truncated_card_ids"] == [f"card{i:04d}" for i in range(16)]
    built = [s for s in seen if s.kind == "prompt_built"][0]
    assert built.detail["cards_rendered"] == 16 and built.detail["cards_truncated"] == 16


def test_the_default_budget_bounds_a_huge_garden():
    model = Recorder()
    cards = _cards(200, body="丁" * 4_000)
    result = GardenComponent(model=model).run_maintenance(
        MaintenanceRequest(cards=cards, locale="zh-Hans"))
    rendered = render_dream_cards(cards)
    assert len(rendered.text) <= 60_000
    assert rendered.text in model.prompts[0]
    assert result.trace["cards_rendered"] == len(rendered.rendered_ids) < 60
    assert result.trace["cards_truncated"] == 0


def test_known_ids_cover_every_rendered_card_without_the_host_listing_them():
    """墓碑卡守卫：模型见过的卡 id 出现在结果里就打回，宿主不必自己记得传。"""
    cards = [{"id": f"c42ebb98a1d2e7{i:02d}", "summary": f"卡{i}", "content": "正文"}
             for i in range(15)]
    leak = cards[3]["id"]
    tomb = json.dumps({"consolidations": [{
        "op": "merge", "card_ids": [cards[0]["id"], cards[1]["id"]],
        "rationale": "同一件事",
        "result": {"summary": f"已被 {leak} 取代", "content": f"见 {leak}。", "bucket": "工作"},
    }]}, ensure_ascii=False)
    out = GardenComponent(model=Recorder(tomb)).run_maintenance(
        MaintenanceRequest(cards=cards, locale="zh-Hans"))
    assert out.error
    # 没渲染进去的卡不算「见过」。
    session = GardenComponent(model=Recorder()).maintenance_session(
        MaintenanceRequest(cards=cards, locale="zh-Hans", cards_limit=2))
    assert session._plan.known_ids == frozenset(c["id"] for c in cards[:2])
    # 宿主另给的 id 仍然保留。
    session = GardenComponent(model=Recorder()).maintenance_session(
        MaintenanceRequest(cards=cards, locale="zh-Hans", cards_limit=2,
                           known_ids=("extra-known-id",)))
    assert "extra-known-id" in session._plan.known_ids


def test_session_and_built_in_loop_agree_with_bodies_and_truncation():
    cards = _cards(15, body="戊" * 50)
    reply = json.dumps({"consolidations": [{
        "op": "merge", "card_ids": ["card0000", "card0001"], "rationale": "同一件事的两个阶段",
        "result": {"summary": "合并后的摘要", "content": "合并后的正文，保留两张卡的全部事实。",
                   "bucket": "生活", "threads": ["搬家"]},
    }]}, ensure_ascii=False)
    request = MaintenanceRequest(cards=cards, locale="zh-Hans", card_body_chars=20)
    built_model = Recorder(reply)
    built = GardenComponent(model=built_model).run_maintenance(request)
    session = GardenComponent(model=Recorder()).maintenance_session(request)
    prompts = []
    while (prompt := session.next_prompt()) is not None:
        prompts.append(prompt)
        session.feed(reply)
    driven = session.result()
    assert prompts == built_model.prompts
    assert (driven.mutations, driven.consolidations, driven.trace, driven.error) == (
        built.mutations, built.consolidations, built.trace, built.error)
    # 两张目标卡都被截断了：提示词让模型别动它们，模型动了 —— 出口硬拦，两条路一致。
    assert built.error is None and built.mutations == [] and built.consolidations == []
    assert built.trace["dropped_truncated_targets"] == 1
    untouched = MaintenanceRequest(cards=cards, locale="zh-Hans")  # 默认预算，全文渲染
    whole = GardenComponent(model=Recorder(reply)).run_maintenance(untouched)
    assert whole.error is None and whole.mutations


def test_parsing_is_unchanged_by_the_new_rendering():
    reply = json.dumps({"consolidations": [{
        "op": "thicken", "card_ids": ["card0002"], "rationale": "补充细节",
        "result": {"summary": "更完整的摘要", "content": "补充后的完整正文。"},
    }]}, ensure_ascii=False)
    cards = _cards(15)
    out = GardenComponent(model=Recorder(reply)).run_maintenance(
        MaintenanceRequest(cards=cards, locale="zh-Hans"))
    direct, _, err = parse_dream_consolidations(
        reply, known_ids=frozenset(c["id"] for c in cards))
    assert err is None and out.consolidations == direct


def test_not_needed_runs_render_nothing():
    out = GardenComponent(model=Recorder()).run_maintenance(
        MaintenanceRequest(cards=_cards(2), locale="zh-Hans"))
    assert out.needed is False and "cards_rendered" not in out.trace


# ------------------------------------------------------------ 黄金快照

GOLDEN_CARDS = [
    {"id": "m_coffee", "bucket": "饮食", "threads": ["咖啡"], "occurred_at": "2026-08-02",
     "summary": "早上喝手冲", "retrieval_cues": ["V60"],
     "content": "每天早上用 V60 手冲，水温 92 度。\n周末换法压壶。"},
    {"id": "m_move", "bucket": "生活", "threads": ["搬家", "预算"],
     "summary": "打算年底搬家", "content": "看中城东两居，预算三百五十万。" + "细节" * 40},
    {"id": "m_title_only", "summary": "只有摘要的老卡"},
    {"id": "m_body_only", "content": "只有正文，没有摘要。"},
] + [{"id": f"m_fill{i:02d}", "summary": f"填充卡{i}", "content": f"填充正文{i}"}
     for i in range(12)]


@pytest.mark.parametrize("locale", ["zh-Hans", "en"])
def test_dream_prompt_golden_snapshot(locale):
    """整份提示词的快照。改提示词或渲染格式时，重新生成并在 PR 里说明为什么。

    重新生成：MEMGARDEN_REGEN_GOLDEN=1 pytest tests/test_dream_card_render.py
    """
    import os

    model = Recorder()
    GardenComponent(model=model).run_maintenance(MaintenanceRequest(
        cards=GOLDEN_CARDS, locale=locale, ai_name="小园", user_name="阿青",
        recent_conversations="阿青：我决定年底搬家了。", card_body_chars=60))
    path = FIXTURES / f"dream_prompt_golden_{locale}.txt"
    if os.environ.get("MEMGARDEN_REGEN_GOLDEN") == "1":
        path.write_text(model.prompts[0], encoding="utf-8")
    assert model.prompts[0] == path.read_text(encoding="utf-8")
