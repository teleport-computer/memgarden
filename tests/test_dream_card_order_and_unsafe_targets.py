"""Dream：Store 路径先给最新的卡；截断卡、没渲染的卡不许被改写（发布前复审）。

两个缺陷都不报错：

- Store 读出来的顺序没有保证（SQLite 的 SELECT 不带 ORDER BY），渲染按顺序取前 60 张。
  花园一过 60 张，提示词里永远是最老的那批；触发这次整理的新卡模型看不到，
  签名和水位线却照样推进。
- 提示词要求模型别改 TRUNCATED 的卡，但只是请求：模型把它放进 ``card_ids``，
  这张卡就被一张只看过前 N 个字的新卡取代，后半截正文随旧卡退休。
"""
from __future__ import annotations

import json
import pathlib
import re
import tempfile

import pytest

from memgarden import GardenComponent, MaintenanceRequest, MountedGarden, Scope
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore

ALICE = Scope(tenant_id="t", memory_owner_id="alice")


class TickingClock:
    """每次写入往后走一秒 —— created_at 由 Store 决定，测试靠它排出新旧。"""

    def __init__(self) -> None:
        self.seconds = 0

    def now_iso(self) -> str:
        self.seconds += 1
        minutes, seconds = divmod(self.seconds, 60)
        return f"2026-09-01T00:{minutes:02d}:{seconds:02d}Z"


class Recorder:
    def __init__(self, reply: str = '{"consolidations": []}') -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.prompts.append(prompt)
        return self.reply


def _store(kind: str):
    if kind == "memory":
        return InMemoryStore(clock=TickingClock())
    return SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db"), clock=TickingClock())


def _seed(store, n: int) -> None:
    # 一张一次写入：created_at 逐张递增，插入顺序 = 从旧到新。
    for i in range(n):
        store.apply("t", [{"op": "add", "card": {
            "summary": f"第{i}件事", "content": f"MARK{i:03d} 这件事的正文。", "bucket": "生活"}}],
            owner="alice", idempotency_key=f"seed-{i}")


def _marks(prompt: str) -> set[int]:
    return {int(m) for m in re.findall(r"MARK(\d{3})", prompt)}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_store_path_dream_sees_the_newest_cards_first(kind):
    store = _store(kind)
    _seed(store, 75)
    model = Recorder()
    garden = MountedGarden(model=model, store=store)
    receipt = garden.run_and_store_maintenance(ALICE, MaintenanceRequest(locale="zh-Hans"))
    assert receipt.error is None, receipt
    assert len(model.prompts) == 1
    seen = _marks(model.prompts[0])
    assert len(seen) == 60
    assert seen == set(range(15, 75)), "最新的 60 张（15–74）进提示词，最老的 15 张让位"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_wire_maintenance_run_sees_the_newest_cards_first(kind):
    store = _store(kind)
    _seed(store, 70)
    model = Recorder()
    service = Service(MountedGarden(model=model, store=store), model_available=True)
    out = service.handle({"id": "m", "method": "maintenance.run", "params": {
        "scope": {"tenant_id": "t", "memory_owner_id": "alice"}, "locale": "zh-Hans"}})
    assert out["ok"], out
    assert 69 in _marks(model.prompts[0]) and 0 not in _marks(model.prompts[0])


def test_same_created_at_ties_break_by_id_deterministically():
    store = InMemoryStore(clock=type("Fixed", (), {"now_iso": lambda self: "2026-09-01T00:00:00Z"})())
    store.apply("t", [{"op": "add", "card": {"summary": f"第{i}件事", "content": f"MARK{i:03d} 正文"}}
                      for i in range(12)], owner="alice", idempotency_key="seed")
    garden = MountedGarden(model=Recorder(), store=store)
    prepared, _ = garden.prepare_maintenance(ALICE, MaintenanceRequest(locale="zh-Hans"))
    ids = [c["id"] for c in prepared.cards]
    assert ids == sorted(ids)


# ----------------------------------------------------------------------- 截断卡 / 没渲染的卡

def _cards(n: int, *, long: set[int] = frozenset()) -> list[dict]:
    return [{"id": f"card{i:04d}", "summary": f"摘要{i}",
             "content": ("戊" * 80 if i in long else f"正文{i}"), "created_at": "2026-08-01"}
            for i in range(n)]


def _merge(*ids: str, summary: str = "合并后的摘要") -> dict:
    return {"op": "merge", "card_ids": list(ids), "rationale": "同一件事",
            "result": {"summary": summary, "content": f"{summary}的完整正文。", "bucket": "生活"}}


def test_consolidations_touching_truncated_or_unrendered_cards_are_dropped():
    cards = _cards(15, long={0})
    reply = json.dumps({"consolidations": [
        _merge("card0000", "card0002", summary="动了截断卡"),
        _merge("card0003", "card0014", summary="动了没渲染的卡"),
        _merge("card0003", "card0004", summary="安全的合并"),
    ]}, ensure_ascii=False)
    out = GardenComponent(model=Recorder(reply)).run_maintenance(
        MaintenanceRequest(cards=cards, locale="zh-Hans", card_body_chars=20, cards_limit=10))
    assert out.error is None
    assert [c["result"]["summary"] for c in out.consolidations] == ["安全的合并"]
    assert len(out.mutations) == 1
    assert sorted(out.mutations[0]["target_ids"]) == ["card0003", "card0004"]
    assert out.trace["dropped_truncated_targets"] == 1
    assert out.trace["dropped_unrendered_targets"] == 1
    assert out.trace["consolidations"] == 1


def test_nothing_is_dropped_when_every_target_was_rendered_whole():
    reply = json.dumps({"consolidations": [_merge("card0001", "card0002")]}, ensure_ascii=False)
    out = GardenComponent(model=Recorder(reply)).run_maintenance(
        MaintenanceRequest(cards=_cards(15), locale="zh-Hans"))
    assert len(out.mutations) == 1
    assert "dropped_truncated_targets" not in out.trace
