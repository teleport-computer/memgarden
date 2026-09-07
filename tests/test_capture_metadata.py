"""策略承诺的日期及 Card 元数据必须到达实际存储，而非停在声明中。"""
import json

import pytest

from memgarden import CaptureRequest, MountedGarden, Scope
from memgarden.contracts import Actor
from memgarden.prompts.capture import parse_capture_cards
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore


CARD = {"action": "add", "summary": "重要转折", "content": "对方在这天决定换一份工作。",
        "bucket": "工作", "threads": ["职业选择"], "occurred_at": "2024-02-29",
        "role": "turning_point", "is_sensitive": True}


def reply(**changes):
    return json.dumps({"cards": [{**CARD, **changes}]}, ensure_ascii=False)


@pytest.mark.parametrize("policy,keeps_date", [
    ("conversation_capture", False), ("history_import", True),
    ("curated_archive", True),
])
def test_parser_preserves_metadata_without_changing_policy(policy, keeps_date):
    cards, error = parse_capture_cards(reply(is_sensitive=False), policy=policy)
    assert error is None
    assert cards[0]["role"] == "turning_point"
    assert cards[0]["is_sensitive"] is False
    assert cards[0].get("occurred_at") == ("2024-02-29" if keeps_date else None)


@pytest.mark.parametrize("date, expected", [
    (None, None), ("", None), ("2024-02-29", "2024-02-29"),
    ("2024-02-29T08:30:00+08:00", "2024-02-29T00:30:00Z"),
])
def test_import_date_preserves_known_precision_and_does_not_invent_missing_date(date, expected):
    cards, error = parse_capture_cards(reply(occurred_at=date), policy="history_import")
    assert error is None
    assert cards[0].get("occurred_at") == expected


@pytest.mark.parametrize("changes", [
    {"occurred_at": "2023-02-29"}, {"occurred_at": "next week"},
    {"occurred_at": "0001-01-01T00:00:00+14:00"},
    {"occurred_at": "9999-12-31T23:59:59-14:00"},
    {"occurred_at": 20240229}, {"role": ["turning_point"]},
    {"is_sensitive": "false"}, {"is_sensitive": 0}, {"is_sensitive": None},
])
def test_invalid_supplied_metadata_rejects_batch_for_retry(changes):
    raw = json.dumps({"cards": [CARD, {**CARD, **changes}]}, ensure_ascii=False)
    cards, error = parse_capture_cards(raw, policy="history_import")
    assert cards == []
    assert error is not None


def test_missing_optional_metadata_remains_missing():
    raw = json.dumps({"cards": [{"action": "add", "summary": "饮食习惯",
                                 "content": "对方不吃辣。"}]})
    cards, error = parse_capture_cards(raw, policy="history_import")
    assert error is None
    assert not ({"role", "is_sensitive", "occurred_at"} & set(cards[0]))


def test_failed_import_date_does_not_advance_progress_or_write_cards(tmp_path):
    class Model:
        def complete(self, prompt, *, purpose=""):
            return reply(occurred_at="next week")

    store = SqliteStore(tmp_path / "failed-import.db")
    service = Service(MountedGarden(model=Model(), store=store))
    response = service.handle({"id": "bad-date", "method": "history.import", "params": {
        "scope": {"tenant_id": "tenant", "memory_owner_id": "owner"},
        "material": "一段待导入的历史材料。", "locale": "zh-Hans",
        "idempotency_key": "bad-date",
    }})
    assert response["ok"], response
    progress = response["result"]
    assert progress["failed"]
    assert progress["cursor"] == progress["cards_written"] == 0
    assert store.load("tenant", owner="owner").cards == []


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("entry", ["sdk", "host_driven", "history_import"])
def test_metadata_survives_capture_commit_and_export(store_kind, entry, tmp_path):
    class Model:
        def complete(self, prompt, *, purpose=""):
            return reply()

    db = tmp_path / "metadata.db"
    store = InMemoryStore() if store_kind == "memory" else SqliteStore(db)
    garden = MountedGarden(model=None if entry == "host_driven" else Model(), store=store)
    scope = Scope(tenant_id="tenant", memory_owner_id="owner", actor=Actor(user_id="u"))
    service = Service(garden)
    wire_scope = {"tenant_id": scope.tenant_id, "memory_owner_id": scope.memory_owner_id,
                  "actor": scope.actor.as_dict()}

    def call(method, params):
        response = service.handle({"id": "metadata", "method": method, "params": params})
        assert response["ok"], response
        return response["result"]

    if entry == "sdk":
        receipt = garden.capture_and_store(scope, CaptureRequest(
            window="2024-02-29：决定换工作。", locale="zh-Hans",
            policy="curated_archive", idempotency_key="sdk",
        ))
        assert receipt.written and receipt.error is None
    elif entry == "host_driven":
        session = call("capture.begin", {"scope": wire_scope, "locale": "zh-Hans",
            "window": "决定换工作。", "idempotency_key": "host"})
        result = call("capture.feed", {"session_id": session["session_id"], "reply": reply()})
        assert result["status"] == "completed"
        assert result["result"]["written"] and not result["result"]["error"]
    else:
        progress = call("history.import", {"scope": wire_scope, "locale": "zh-Hans",
            "material": "2024-02-29：决定换工作。", "idempotency_key": "import",
            "material_kind": "diary"})
        assert not progress["failed"]
        assert progress["cursor"] == progress["total"]

    # SQLite 重新打开后仍读到同样内容，检查的不是未提交的组件结果。
    reopened = SqliteStore(db) if store_kind == "sqlite" else store
    exported = MountedGarden(model=None, store=reopened).export(scope).items.records
    assert len(exported) == 1
    card = exported[0]
    assert card["role"] == "turning_point"
    assert card["is_sensitive"] is True
    assert card.get("occurred_at") == (None if entry == "host_driven" else "2024-02-29")
    assert card["source_actor"]["user_id"] == "u"
    if entry == "history_import":
        assert card["source"] == "history_import"
        assert card["source_material_kind"] == "diary"


def test_invalid_metadata_is_retried_without_losing_valid_card():
    class Model:
        calls = 0

        def complete(self, prompt, *, purpose=""):
            self.calls += 1
            return reply(is_sensitive="false") if self.calls == 1 else reply(is_sensitive=False)

    model = Model()
    store = InMemoryStore()
    scope = Scope(tenant_id="t", memory_owner_id="o")
    garden = MountedGarden(model=model, store=store)
    result = garden.capture_and_store(scope, CaptureRequest(
        window="决定换工作。", locale="zh-Hans", idempotency_key="retry"))
    assert result.written and not result.error
    assert model.calls == 2
    cards = store.load("t", owner="o").cards
    assert len(cards) == 1
    assert cards[0]["is_sensitive"] is False
