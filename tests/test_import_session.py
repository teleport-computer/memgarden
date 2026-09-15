"""宿主驱动的分批历史导入（``GardenComponent.import_session``）。

## 这些测试守什么

宿主（io 这类自己调模型、自己加密写库的 Runtime）用会话导入一份很长的材料：

  · 分批、断点续传：进程死在第 k 批之后，拿存下来的进度重开，已提交的批次
    不再调模型，也不会重复写卡
  · 跨批去重：后面批次的「已有记忆索引」里有前面批次刚写的卡（带宿主的真实 id），
    以及和这一批文字相关、但重要度排不进前 60 的旧卡
  · 解析失败会重问一次；彻底失败时游标不动、成功后失败记录被清掉
  · 单批上限和整次上限都生效，而且说出来
  · 提示词快照：单段式写卡、两段式抽候选、两段式写卡三种提示词
  · MountedGarden（内核调模型写 Store）和宿主驱动产出同样的卡

全部是合成材料，不含真实用户数据。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re

import pytest

from memgarden import GardenComponent, ImportProgress, ImportRequest, MountedGarden, Scope
from memgarden.importing import (
    MAX_CANDIDATES,
    ImportSession,
    converge_bucket,
    select_index_cards,
)
from memgarden.policies import HISTORY_IMPORT_OPENING_RUBRIC
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore

GOLDEN = pathlib.Path(__file__).parent / "golden"


# --------------------------------------------------------------------- 夹具

def _material(*stretches: str) -> str:
    """每段补足到 ~180 字并以换行结尾：batch_chars=200 时一段一批。"""
    out = []
    for text in stretches:
        body = text
        while len(body) < 180:
            body += " " + text
        out.append(body[:180].rstrip() + "\n")
    return "".join(out)


def _card(summary: str, *, action: str = "add", target: str | None = None,
          bucket: str = "偏好与边界", occurred_at: str | None = None) -> dict:
    card = {"action": action, "type": "fact", "target_id": target,
            "bucket": bucket, "threads": ["饮食"], "summary": summary,
            "content": f"{summary}。这是一段足够长、有实质内容的正文。",
            "importance": 0.5, "pulse": 0.2}
    if occurred_at is not None:
        card["occurred_at"] = occurred_at
    return card


def _reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


class ScriptedModel:
    """按提示词里材料的关键词决定回复；记录每次调用。"""

    def __init__(self, rules: list[tuple[str, str]], default: str = '{"cards": []}') -> None:
        self.rules = rules
        self.default = default
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        window = _window_of(prompt)
        for needle, reply in self.rules:
            if needle in window:
                return reply
        return self.default

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self(prompt)


def _window_of(prompt: str) -> str:
    """只看材料那一段，别被已有索引里的字误匹配。"""
    for label in ("[The material they handed you]", "[The material]"):
        if label in prompt:
            return prompt.split(label, 1)[1].split("[Output]", 1)[0]
    return prompt


class HostStore:
    """宿主自己的「库」：只用来模拟 io 的执行器分配 id、执行 supersede。"""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.keys: dict[str, list[str]] = {}
        self.seq = 0

    def write(self, mutations: list[dict], *, idempotency_key: str) -> list[str]:
        if idempotency_key in self.keys:   # 幂等重放
            return self.keys[idempotency_key]
        ids = []
        for m in mutations:
            self.seq += 1
            rid = f"h_{self.seq}"
            if m["op"] == "supersede":
                self.cards[m["target_id"]]["superseded_by"] = rid
            self.cards[rid] = {**m["card"], "id": rid}
            ids.append(rid)
        self.keys[idempotency_key] = ids
        return ids

    def active(self) -> list[dict]:
        return [c for c in self.cards.values() if not c.get("superseded_by")]


def _drive(session: ImportSession, model, store: HostStore, *, max_batches: int | None = None):
    ran = 0
    while max_batches is None or ran < max_batches:
        batch = session.next_batch()
        if batch is None:
            break
        ran += 1
        while (prompt := batch.next_prompt()) is not None:
            batch.feed(model(prompt))
        outcome = batch.result()
        if outcome.error:
            session.commit(outcome)
            break
        ids = store.write(outcome.mutations, idempotency_key=outcome.idempotency_key) \
            if outcome.mutations else []
        session.commit(outcome, record_ids=ids)
    return session.progress


def _garden() -> GardenComponent:
    class _NoModel:
        def complete(self, *_a, **_k):
            raise AssertionError("宿主驱动的会话不许由内核调模型")
    return GardenComponent(model=_NoModel())


def _roundtrip(progress: ImportProgress) -> ImportProgress:
    """宿主把进度序列化存起来，再读回来。"""
    from dataclasses import asdict
    raw = json.loads(json.dumps(asdict(progress), ensure_ascii=False))
    return ImportProgress(**raw)


# --------------------------------------------------------------- 分批 + 续传

def test_multi_batch_import_resumes_after_a_crash_without_recalling_or_rewriting():
    material = _material("我不吃辣，一吃就胃疼", "周末常去爬山", "养了一只叫蛋子的猫")
    request = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                            idempotency_key="imp")
    model = ScriptedModel([
        ("不吃辣", _reply(_card("不吃辣"))),
        ("爬山", _reply(_card("周末爬山", bucket="爱好"))),
        ("蛋子", _reply(_card("养了猫蛋子", bucket="宠物"))),
    ])
    store = HostStore()
    garden = _garden()

    first = garden.import_session(request, owner_key="u1")
    assert first.estimate()["batches_total"] == 3
    saved = _roundtrip(_drive(first, model, store, max_batches=2))
    assert saved.batches_done == 2 and not saved.done
    assert len(model.prompts) == 2 and len(store.active()) == 2

    # 进程重启：宿主重新读库、带着存下的进度重开会话。
    resumed = garden.import_session(request, progress=saved, owner_key="u1",
                                    existing_cards=store.active())
    done = _drive(resumed, model, store)
    assert done.done and done.percent == 100
    assert len(model.prompts) == 3, "已提交的批次不许再调模型"
    assert sorted(c["summary"] for c in store.active()) == ["不吃辣", "养了猫蛋子", "周末爬山"]
    assert done.cards_written == 3

    # 已完成的进度再开一次：什么都不跑。
    again = garden.import_session(request, progress=_roundtrip(done), owner_key="u1")
    assert again.next_batch() is None


def test_progress_cannot_be_resumed_by_another_owner_or_for_other_material():
    request = ImportRequest(material=_material("甲", "乙"), locale="zh-Hans", batch_chars=200)
    store = HostStore()
    session = _garden().import_session(request, owner_key="u1")
    saved = _drive(session, ScriptedModel([]), store, max_batches=1)
    with pytest.raises(ValueError, match="当前请求不同"):
        _garden().import_session(request, progress=_roundtrip(saved), owner_key="u2")
    other = ImportRequest(material=_material("丙", "丁"), locale="zh-Hans", batch_chars=200)
    with pytest.raises(ValueError, match="另一份材料"):
        _garden().import_session(other, progress=_roundtrip(saved), owner_key="u1")


def test_new_options_at_defaults_keep_the_legacy_fingerprint():
    """升级之后，老版本存下的进度在默认请求上必须还能续传。"""
    scope = Scope(tenant_id="t", memory_owner_id="o")
    material = _material("一", "二")
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    legacy = hashlib.sha256(json.dumps(
        ["history-import-v1", digest, "t", "o", "agent-private", "zh-Hans",
         "history_import", "", "", "", "", 50, 6000],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    session = ImportSession(GardenComponent(model=None), ImportRequest(
        material=material, locale="zh-Hans"), binding=("t", scope.owner()))
    assert session.progress.import_fingerprint == legacy


# --------------------------------------------------------------- 跨批去重

def test_cards_written_in_earlier_batches_are_in_later_prompts_and_can_be_merged():
    material = _material("我不吃辣", "后来连微辣也不吃了", "周末常去爬山")
    request = ImportRequest(material=material, locale="zh-Hans", batch_chars=200)
    store = HostStore()

    def model(prompt: str) -> str:
        window = _window_of(prompt)
        if "爬山" in window:
            # 第三批：被取代的旧卡不能再出现在索引里（否则模型会去 merge 一张已退休的卡），
            # 取代它的新卡要在。
            assert "- h_1:" not in prompt and "- h_2: [偏好与边界] 完全不吃辣" in prompt
            return _reply(_card("周末爬山", bucket="爱好"))
        if "微辣" in window:
            # 第二批：索引里必须已经有第一批刚写的卡，带宿主给的真实 id。
            match = re.search(r"- (h_\d+): \[[^\]]*\] 不吃辣", prompt)
            assert match, "第二批的索引里看不到第一批写的卡"
            return _reply(_card("完全不吃辣", action="merge", target=match.group(1)))
        return _reply(_card("不吃辣"))

    progress = _drive(_garden().import_session(request), model, store)
    assert progress.done
    active = store.active()
    assert [c["summary"] for c in active] == ["完全不吃辣", "周末爬山"], "同一件事被写成了两张卡"


def test_index_picks_relevant_old_cards_not_just_the_most_important():
    """花园里有 200 张更重要的无关卡时，和这一批相关的旧卡仍要进索引。

    以前按重要度取前 60，导入第 N 批时这张低重要度的卡看不见，只能再写一张。
    """
    filler = [{"id": f"f{i}", "summary": f"关于项目{i}号的会议安排",
               "content": "会议室和时间", "importance": 0.9} for i in range(200)]
    target = {"id": "cat", "summary": "养了一只叫蛋子的橘猫", "content": "蛋子很黏人",
              "importance": 0.1}
    picked = select_index_cards(filler + [target], "那天蛋子又把杯子推下桌", limit=60)
    assert len(picked) == 60
    assert "cat" in {c["id"] for c in picked}
    # 四分之一名额留给最重要的卡。
    assert sum(1 for c in picked if c["importance"] == 0.9) >= 15

    request = ImportRequest(material=_material("那天蛋子又把杯子推下桌"),
                            locale="zh-Hans", batch_chars=200)
    session = _garden().import_session(request, existing_cards=filler + [target])
    assert "- cat:" in session.next_batch().next_prompt()


def test_host_ranker_is_used_for_the_index():
    cards = [{"id": f"c{i}", "summary": f"卡{i}", "importance": 0.5} for i in range(100)]
    picked = select_index_cards(cards, "x", limit=8, ranker=lambda _t, cs: ["c99", "c98", "nope"])
    assert [c["id"] for c in picked[:2]] == ["c99", "c98"]
    assert len(picked) == 8


def test_bucket_spelling_converges_across_batches():
    assert converge_bucket("work ", ["Work"]) == "Work"
    assert converge_bucket("健康/Health", [], locale="zh-Hans") == "健康"
    assert converge_bucket("Health/健康", [], locale="en") == "Health"
    assert converge_bucket("工作/职业", ["工作"]) == "工作/职业"  # 不是通用桶对，不猜
    # 模型把通用桶清单里相邻两个连着抄成一个桶名（真模型对比里出现过）。
    assert converge_bucket("工作、目标与成长", ["工作"]) == "工作"
    assert converge_bucket("妈妈、爸爸", []) == "妈妈、爸爸"  # 不全是通用桶，不动

    material = _material("I climb every weekend", "I also climb indoors")
    request = ImportRequest(material=material, locale="en", batch_chars=200)
    store = HostStore()
    def en(summary: str, bucket: str) -> dict:
        return {**_card(summary, bucket=bucket), "threads": ["climbing"],
                "content": f"{summary}; a real body with enough substance."}

    model = ScriptedModel([
        ("indoors", _reply(en("Climbs indoors too", "my  Hobbies "))),
        ("weekend", _reply(en("Climbs on weekends", "My hobbies"))),
    ])
    _drive(_garden().import_session(request), model, store)
    # 第一批自创了桶「My hobbies」；第二批写成「my  Hobbies 」也要并到同一个。
    assert {c["bucket"] for c in store.active()} == {"My hobbies"}


# --------------------------------------------------------------- 解析失败

def test_parse_error_is_re_asked_once_then_succeeds():
    request = ImportRequest(material=_material("我不吃辣"), locale="zh-Hans", batch_chars=200)
    batch = _garden().import_session(request).next_batch()
    first = batch.next_prompt()
    batch.feed('{"cards": [{"action": "add", "summary": "不吃辣" "content": 1}]}')
    retry = batch.next_prompt()
    assert retry is not None and retry != first and retry.startswith(first)
    batch.feed(_reply(_card("不吃辣")))
    assert batch.next_prompt() is None
    outcome = batch.result()
    assert outcome.error is None and outcome.retried == 1 and len(outcome.mutations) == 1


def test_hard_failure_keeps_the_cursor_and_a_later_success_clears_it():
    material = _material("第一段", "第二段")
    request = ImportRequest(material=material, locale="zh-Hans", batch_chars=200)
    store = HostStore()
    session = _garden().import_session(request)
    broken = _drive(session, lambda _p: "模型跑偏了，这不是 JSON", store)
    assert broken.failed and broken.cursor == 0 and not broken.done
    assert broken.batches_done == 0

    # 同一个会话里重试同一批。
    fixed = _drive(session, ScriptedModel([("第一段", _reply(_card("第一段的事"))),
                                           ("第二段", _reply(_card("第二段的事")))]), store)
    assert fixed.done and fixed.failed == []
    assert len(store.active()) == 2


def test_host_write_failure_is_recorded_and_the_batch_is_retried():
    request = ImportRequest(material=_material("我不吃辣"), locale="zh-Hans", batch_chars=200)
    session = _garden().import_session(request)
    batch = session.next_batch()
    batch.feed(_reply(_card("不吃辣")))
    outcome = batch.result()
    progress = session.fail(outcome, "encrypt_failed")
    assert progress.failed == [{"offset": 0, "error": "encrypt_failed"}]
    again = session.next_batch()
    assert again.offset == 0 and again.idempotency_key == outcome.idempotency_key


def test_commit_rejects_stale_outcomes_and_mismatched_ids():
    request = ImportRequest(material=_material("甲事", "乙事"), locale="zh-Hans", batch_chars=200)
    session = _garden().import_session(request)
    batch = session.next_batch()
    batch.feed(_reply(_card("甲事")))
    outcome = batch.result()
    with pytest.raises(ValueError, match="不一致"):
        session.commit(outcome, record_ids=[])
    session.commit(outcome, record_ids=["h_1"])
    with pytest.raises(ValueError, match="不是会话当前待提交的批次"):
        session.commit(outcome, record_ids=["h_1"])


# --------------------------------------------------------------- 上限

def test_per_batch_cap_is_announced():
    many = _reply(*[_card(f"第{i}件事") for i in range(8)])
    request = ImportRequest(material=_material("很多事"), locale="zh-Hans",
                            batch_chars=200, max_cards=5)
    batch = _garden().import_session(request).next_batch()
    batch.feed(many)
    outcome = batch.result()
    assert len(outcome.mutations) == 5
    assert outcome.trace["capped_from"] == 8 and outcome.trace["cap"] == 5


def test_total_cap_stops_the_import_and_says_so():
    material = _material("第一段", "第二段", "第三段")
    request = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                            max_total_cards=3)
    model = ScriptedModel([], default=_reply(_card("甲事"), _card("乙事")))
    store = HostStore()
    session = _garden().import_session(request)
    progress = _drive(session, model, store)
    assert len(store.active()) == 3, "整次上限没拦住"
    assert progress.cards_added == 3
    assert len(model.prompts) == 2, "上限满了之后不许再为剩下的批次调模型"
    assert progress.done
    assert any(s["reason"] == "max_total_cards" for s in progress.skipped)


# --------------------------------------------------------------- 日期 / 预切批次

def test_fallback_date_fills_only_undated_cards():
    request = ImportRequest(material=_material("两件事"), locale="zh-Hans", batch_chars=200,
                            fallback_occurred_at="2023-05-01")
    batch = _garden().import_session(request).next_batch()
    batch.feed(_reply(_card("有日期", occurred_at="2024-02-29"), _card("没日期")))
    cards = {c["summary"]: c for c in batch.result().cards}
    assert cards["有日期"]["occurred_at"] == "2024-02-29"
    assert cards["没日期"]["occurred_at"] == "2023-05-01"
    with pytest.raises(ValueError, match="fallback_occurred_at"):
        _garden().import_session(ImportRequest(material="x", locale="zh-Hans",
                                               fallback_occurred_at="上周"))


def test_without_fallback_no_date_is_invented():
    request = ImportRequest(material=_material("一件事"), locale="zh-Hans", batch_chars=200)
    batch = _garden().import_session(request).next_batch()
    batch.feed(_reply(_card("没日期")))
    assert "occurred_at" not in batch.result().cards[0]


def test_host_pre_split_batches_carry_their_time_hints():
    batches = (
        {"text": "我不吃辣\n", "label": "chat export", "occurred_from": "2024-01-01",
         "occurred_to": "2024-01-31"},
        {"text": "   \n"},
        {"text": "周末爬山\n"},
    )
    request = ImportRequest(material="", batches=batches, locale="zh-Hans")
    session = _garden().import_session(request)
    first = session.next_batch()
    assert "[chat export · 2024-01-01 → 2024-01-31]" in first.next_prompt()
    first.feed(_reply(_card("不吃辣")))
    session.commit(first.result(), record_ids=["h_1"])
    second = session.next_batch()
    assert second.offset == len("我不吃辣\n") + len("   \n")
    second.feed('{"cards": []}')
    progress = session.commit(second.result())
    assert progress.done and progress.cursor == progress.total

    changed = request.__class__(material="", locale="zh-Hans", batches=(
        {"text": "我不吃辣\n"}, {"text": "   \n"}, {"text": "周末爬山\n"}))
    with pytest.raises(ValueError):
        _garden().import_session(changed, progress=_roundtrip(progress))
    with pytest.raises(ValueError, match="只能给一个"):
        _garden().import_session(ImportRequest(material="x", batches=batches, locale="zh-Hans"))
    with pytest.raises(ValueError, match="ISO"):
        _garden().import_session(ImportRequest(
            material="", batches=({"text": "x", "occurred_from": "去年"},), locale="zh-Hans"))


# --------------------------------------------------------------- 两段式

def _candidates(*items: tuple[str, str]) -> str:
    return json.dumps({"candidates": [
        {"about": "person", "summary": s, "evidence": e, "occurred_at": None}
        for s, e in items]}, ensure_ascii=False)


def test_two_pass_extracts_candidates_dedupes_them_and_writes_cards_at_the_end():
    material = _material("我不吃辣", "再说一次我不吃辣，还有我养猫")
    request = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                            strategy="two_pass", write_batch_candidates=10)
    store = HostStore()
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        if "[The material]" in prompt:
            window = _window_of(prompt)
            if "养猫" in window:
                return _candidates(("不吃辣", "再说一次我不吃辣"), ("养了一只猫", "我养猫"))
            return _candidates(("不吃辣", "我不吃辣"))
        # 写卡阶段：候选清单里同一件事只能出现一次。
        assert prompt.count("· 不吃辣") == 1
        return _reply(_card("不吃辣"), _card("养了一只猫", bucket="宠物"))

    session = _garden().import_session(request)
    saved = _roundtrip(_drive(session, model, store, max_batches=2))
    assert saved.cursor == saved.total and not saved.done
    assert [c["summary"] for c in saved.candidates] == ["不吃辣", "养了一只猫"]
    assert 60 <= saved.percent < 100
    assert store.active() == [], "抽候选阶段不许写卡"

    resumed = _garden().import_session(request, progress=saved)
    done = _drive(resumed, model, store)
    assert done.done and done.candidates_cursor == 2
    assert sorted(c["summary"] for c in store.active()) == ["不吃辣", "养了一只猫"]
    assert sum(1 for p in prompts if "[The material]" in p) == 2, "续传后不许重跑抽候选"


def test_two_pass_candidate_cap_is_recorded(monkeypatch):
    import memgarden.importing as importing

    monkeypatch.setattr(importing, "MAX_CANDIDATES", 1)
    request = ImportRequest(material=_material("两件事"), locale="zh-Hans", batch_chars=200,
                            strategy="two_pass")
    batch = _garden().import_session(request).next_batch()
    batch.feed(_candidates(("第一件事", "a"), ("第二件事", "b")))
    session = batch._session
    progress = session.commit(batch.result())
    assert len(progress.candidates) == 1
    assert progress.skipped[-1]["reason"] == "candidate_cap"
    assert MAX_CANDIDATES == 4000


def test_unknown_strategy_is_refused():
    with pytest.raises(ValueError, match="strategy"):
        _garden().import_session(ImportRequest(material="x", locale="zh-Hans",
                                               strategy="twopass"))


# --------------------------------------------------------------- 提示词快照

def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("MEMGARDEN_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # 快照文件缺失必须红，不能自动生成后放行 —— 否则删掉快照就等于关掉这条测试。
    assert path.exists(), f"缺少快照 {name}；用 MEMGARDEN_UPDATE_GOLDEN=1 生成"
    assert text == path.read_text(encoding="utf-8"), (
        f"{name} 变了。确认是有意的改动后用 MEMGARDEN_UPDATE_GOLDEN=1 重新生成")


_SNAPSHOT_CARDS = [{"id": "m_1", "summary": "养了一只叫蛋子的橘猫", "bucket": "宠物",
                    "threads": ["蛋子"], "importance": 0.6}]


def test_single_pass_history_import_prompt_snapshot():
    request = ImportRequest(material="2024-03-02 The person: 蛋子今天又把杯子推下桌了\n",
                            locale="zh-Hans", ai_name="小花", user_name="小明",
                            material_kind="chat_export")
    prompt = _garden().import_session(request, existing_cards=_SNAPSHOT_CARDS) \
        .next_batch().next_prompt()
    # 单段式就是最终的卡：不许再说「这是候选阶段、去重在后面」。
    assert "candidate stage" not in prompt
    assert "- m_1: [宠物] 养了一只叫蛋子的橘猫" in prompt
    _golden("history_import_single_pass.txt", prompt)


def test_two_pass_prompts_snapshot():
    request = ImportRequest(material="2024-03-02 The person: 蛋子今天又把杯子推下桌了\n",
                            locale="zh-Hans", ai_name="小花", user_name="小明",
                            material_kind="chat_export", strategy="two_pass")
    session = _garden().import_session(request, existing_cards=_SNAPSHOT_CARDS)
    batch = session.next_batch()
    map_prompt = batch.next_prompt()
    assert HISTORY_IMPORT_OPENING_RUBRIC in map_prompt
    _golden("history_import_two_pass_candidates.txt", map_prompt)
    batch.feed(_candidates(("蛋子会把杯子推下桌", "蛋子今天又把杯子推下桌了")))
    session.commit(batch.result())
    write_prompt = session.next_batch().next_prompt()
    assert "· 蛋子会把杯子推下桌 — “蛋子今天又把杯子推下桌了”" in write_prompt
    _golden("history_import_two_pass_write.txt", write_prompt)


# --------------------------------------------------------------- 两种接法一致

@pytest.mark.parametrize("strategy", ["single_pass", "two_pass"])
def test_mounted_garden_and_host_driven_session_write_the_same_cards(strategy):
    material = _material("我不吃辣", "周末常去爬山")
    rules = [
        ("爬山", _reply(_card("周末爬山", bucket="爱好"))),
        ("不吃辣", _reply(_card("不吃辣"))),
    ]
    if strategy == "two_pass":
        def reply(prompt: str) -> str:
            if "[The material]" in prompt:
                w = _window_of(prompt)
                return _candidates(("周末爬山", "爬山")) if "爬山" in w else _candidates(("不吃辣", "不吃辣"))
            return _reply(_card("不吃辣"), _card("周末爬山", bucket="爱好"))
    else:
        reply = ScriptedModel(rules)

    class Model:
        def complete(self, prompt, *, purpose=""):
            return reply(prompt)

    def shape(cards):
        keep = ("summary", "content", "bucket", "threads", "source", "occurred_at")
        return sorted(({k: c.get(k) for k in keep} for c in cards), key=lambda c: c["summary"])

    request = ImportRequest(material=material, locale="zh-Hans", batch_chars=200,
                            strategy=strategy, idempotency_key="same")
    store = InMemoryStore()
    scope = Scope(tenant_id="t", memory_owner_id="o")
    progress = MountedGarden(model=Model(), store=store).import_history(scope, request)
    assert progress.done

    host = HostStore()
    host_progress = _drive(_garden().import_session(request), reply, host)
    assert host_progress.done
    assert shape(store.load("t", owner="o").cards) == shape(host.active())
    assert progress.cards_written == host_progress.cards_written == 2


def test_wire_history_import_accepts_the_new_options():
    class Model:
        def complete(self, prompt, *, purpose=""):
            if "[The material]" in prompt:
                return _candidates(("不吃辣", "我不吃辣"))
            return _reply(_card("不吃辣"))

    store = InMemoryStore()
    service = Service(MountedGarden(model=Model(), store=store))
    out = service.handle({"id": "1", "method": "history.import", "params": {
        "scope": {"tenant_id": "t", "memory_owner_id": "o"}, "material": "",
        "batches": [{"text": "我不吃辣\n", "occurred_from": "2024-01-01"}],
        "locale": "zh-Hans", "strategy": "two_pass", "max_total_cards": 5,
        "fallback_occurred_at": "2024-01-01"}})
    assert out["ok"], out
    assert out["result"]["done"] is True and out["result"]["strategy"] == "two_pass"
    card = store.load("t", owner="o").cards[0]
    assert card["occurred_at"] == "2024-01-01" and card["source"] == "history_import"


def test_two_pass_candidate_parse_error_is_re_asked_once():
    request = ImportRequest(material=_material("我不吃辣"), locale="zh-Hans", batch_chars=200,
                            strategy="two_pass")
    batch = _garden().import_session(request).next_batch()
    first = batch.next_prompt()
    batch.feed('{"candidates": [{"summary": "不吃辣" "evidence": "x"}]}')
    retry = batch.next_prompt()
    assert retry is not None and retry.startswith(first) and retry != first
    batch.feed(_candidates(("不吃辣", "我不吃辣")))
    assert batch.next_prompt() is None
    outcome = batch.result()
    assert outcome.error is None and outcome.retried == 1
    assert [c["summary"] for c in outcome.candidates] == ["不吃辣"]
