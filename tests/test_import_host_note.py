"""宿主补充指引 ``host_note``（``ImportRequest`` / ``CaptureRequest``）。

## 这些测试守什么

  · 不给（或只给空白）时，三种写卡/抽候选提示词与没有这个字段时**逐字节相同**
    （既有 golden 快照另外守着默认形状，这里直接比「给空」和「不给」）
  · 给了就原样出现在写卡提示词里：材料之后、``[Output]`` 之前
  · 两段式只进写卡批次，抽候选那一步不带
  · 不进续传指纹：换一份指引续传，进度照常续、幂等键不变
  · wire ``history.import_begin`` 接受它；日常 Capture 也认它

全部是合成材料，不含真实用户数据。
"""
from __future__ import annotations

import json

from memgarden import CaptureRequest, GardenComponent, ImportRequest, MountedGarden
from memgarden.prompts.capture import build_capture_prompt
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore

NOTE = "The garden already holds 12 cards; aim for 20-46 in total, never invent."


class _NoModel:
    def complete(self, *_a, **_k):
        raise AssertionError("宿主驱动的会话不许由内核调模型")


def _garden() -> GardenComponent:
    return GardenComponent(model=_NoModel())


def _request(**extra) -> ImportRequest:
    return ImportRequest(material="2024-03-02 The person: 蛋子今天又把杯子推下桌了\n",
                         locale="zh-Hans", ai_name="小花", user_name="小明",
                         material_kind="chat_export", **extra)


def _two_pass_prompts(**extra) -> tuple[str, str]:
    session = _garden().import_session(_request(strategy="two_pass", **extra))
    batch = session.next_batch()
    candidates_prompt = batch.next_prompt()
    batch.feed(json.dumps({"candidates": [
        {"about": "person", "summary": "蛋子会把杯子推下桌",
         "evidence": "蛋子今天又把杯子推下桌了", "occurred_at": None}]}, ensure_ascii=False))
    session.commit(batch.result())
    return candidates_prompt, session.next_batch().next_prompt()


def test_empty_host_note_keeps_every_prompt_byte_identical():
    default_single = _garden().import_session(_request()).next_batch().next_prompt()
    for blank in ("", "   \n"):
        single = _garden().import_session(_request(host_note=blank)).next_batch().next_prompt()
        assert single == default_single
        assert _two_pass_prompts(host_note=blank) == _two_pass_prompts()
    assert "[Host guidance]" not in default_single

    kwargs = dict(ai_name="a", user_name="u", buckets="", threads="", identity="",
                  window="hi", locale="en")
    assert build_capture_prompt(**kwargs, host_note="") == build_capture_prompt(**kwargs)


def test_single_pass_write_prompt_carries_the_note_between_material_and_output():
    prompt = _garden().import_session(_request(host_note=NOTE)).next_batch().next_prompt()
    block = f"\n[Host guidance]\n{NOTE}\n"
    assert prompt.count(block) == 1
    assert prompt.index("蛋子今天又把杯子推下桌了") < prompt.index(block) < prompt.index("[Output]")
    default = _garden().import_session(_request()).next_batch().next_prompt()
    assert prompt.replace(block, "", 1) == default, "除了这一段，提示词其余部分不许变"


def test_two_pass_only_the_write_stage_gets_the_note():
    candidates_prompt, write_prompt = _two_pass_prompts(host_note=NOTE)
    default_candidates, default_write = _two_pass_prompts()
    assert NOTE not in candidates_prompt and candidates_prompt == default_candidates
    assert write_prompt.count(NOTE) == 1
    assert write_prompt.replace(f"\n[Host guidance]\n{NOTE}\n", "", 1) == default_write


def test_host_note_is_not_part_of_the_resume_fingerprint():
    material = "我不吃辣\n" * 30 + "周末常去爬山\n" * 30
    plain = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                          idempotency_key="imp")
    noted = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                          idempotency_key="imp", host_note=NOTE)
    first = _garden().import_session(noted, owner_key="u1")
    batch = first.next_batch()
    batch.feed('{"cards": []}')
    saved = first.commit(batch.result())
    assert saved.cursor > 0

    # 换一份指引（或去掉）续传：不许报「语义不同」，下一批的幂等键也不变。
    other = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                          idempotency_key="imp", host_note="something else")
    resumed = _garden().import_session(other, progress=saved, owner_key="u1")
    resumed_plain = _garden().import_session(plain, progress=saved, owner_key="u1")
    assert saved.import_fingerprint == resumed_plain.progress.import_fingerprint
    assert resumed.next_batch().idempotency_key == resumed_plain.next_batch().idempotency_key


def test_capture_request_renders_the_note_too():
    prompts: list[str] = []

    class Model:
        def complete(self, prompt, *, purpose=""):
            prompts.append(prompt)
            return '{"cards": []}'

    GardenComponent(model=Model()).capture(
        CaptureRequest(window="我不吃辣", locale="zh-Hans", host_note=NOTE))
    assert f"\n[Host guidance]\n{NOTE}\n" in prompts[0]
    GardenComponent(model=Model()).capture(CaptureRequest(window="我不吃辣", locale="zh-Hans"))
    assert prompts[1] == prompts[0].replace(f"\n[Host guidance]\n{NOTE}\n", "", 1)


def test_wire_import_begin_accepts_host_note():
    service = Service(MountedGarden(model=None, store=InMemoryStore()))
    out = service.handle({"id": "1", "method": "history.import_begin", "params": {
        "scope": {"tenant_id": "t", "memory_owner_id": "o"},
        "material": "我不吃辣\n", "locale": "zh-Hans", "host_note": NOTE}})
    assert out["ok"], out
    assert out["result"]["status"] == "needs_model"
    assert f"\n[Host guidance]\n{NOTE}\n" in out["result"]["next_prompt"]
