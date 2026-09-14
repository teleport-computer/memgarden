import json

import pytest

from memgarden import CaptureRequest, MaintenanceRequest, MountedGarden, Scope
from memgarden.adapt import to_card
from memgarden.component import GardenComponent
from memgarden.prompts import capture, dream, recall_fields
from memgarden.records import Card, mutation_from_dict
from memgarden.schema import schemas
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore


def test_optional_cues_preserve_existing_card_positional_arguments():
    old_fields = {
        "summary": "A summary", "content": "A factual body", "bucket": "life",
        "threads": ["topic"], "importance": 0.7, "pulse": 0.4,
        "occurred_at": "2026-09-01", "role": "turning_point", "is_sensitive": True,
        "source": "import", "source_material_kind": "diary",
        "source_actor": {"id": "fixture-actor"},
    }
    legacy = Card(*old_fields.values())
    for name, expected in old_fields.items():
        assert getattr(legacy, name) == expected
    assert legacy.retrieval_cues == []


def card():
    return {"summary": "The parcel pickup code is AB-123.",
            "content": "The parcel pickup code is AB-123. It was left at the west gate on September 1.",
            "bucket": "life", "threads": ["parcel"], "importance": 0.7, "pulse": 0.4,
            "retrieval_cues": ["parcel", "pickup code", "2026-09-01"], "importance_level": 2}


def test_capture_cues_survive_parser_and_keep_legacy_importance():
    row = {**card(), "action": "add", "type": "fact"}
    rows, error = capture.parse_capture_cards(json.dumps({"cards": [row]}))
    assert error is None
    assert rows[0]["retrieval_cues"] == row["retrieval_cues"]
    assert rows[0]["importance"] == 0.4
    row.pop("retrieval_cues")
    row.pop("importance_level")
    rows, error = capture.parse_capture_cards(json.dumps({"cards": [row]}))
    assert error is None and rows[0]["importance"] == 0.7
    assert "retrieval_cues" not in rows[0]


def test_dream_cues_survive_parser_without_rewriting_body():
    result = card()
    rows, questions, error = dream.parse_dream_consolidations(json.dumps({"consolidations": [
        {"op": "thicken", "card_ids": ["old"], "rationale": "Preserve facts and repair missing retrieval hints.", "result": result}], "questions_to_ask": []}))
    assert error is None and not questions
    assert rows[0]["result"]["retrieval_cues"] == result["retrieval_cues"]
    assert rows[0]["result"]["content"] == result["content"]
    assert rows[0]["result"]["importance"] == 0.4


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
def test_five_levels_map_to_existing_normalized_storage(level):
    assert recall_fields.importance({"importance_level": level}, lambda v: 0.7) == level / 5


def test_cues_are_optional_bounded_strings_not_objects():
    assert recall_fields.retrieval_cues([{}, True, 12, None, " a  b ", "a b", "x" * 200, *map(str, range(8))]) == ["a b", "x" * 120, "0", "1", "2"]
    assert recall_fields.retrieval_cues("not a list") == []
    for invalid in [True, 0, 6, "3", None]:
        assert recall_fields.importance({"importance_level": invalid, "importance": 0.7}, float) == 0.7


def test_prompts_require_grounded_cues_event_time_and_five_levels():
    for prompt in [capture._CAPTURE_PROMPT_TEMPLATE, dream._DREAM_PROMPT_TEMPLATE]:
        assert "retrieval_cues" in prompt and "importance_level" in prompt
        assert "event time" in prompt and "Do not invent" in prompt
        assert "0.2/0.4/0.6/0.8/1.0" in prompt


def test_cues_are_declared_and_survive_typed_mutation_round_trip():
    cues = ["parcel", "pickup code", "2026-09-01"]
    payload = {"op": "add", "card": {
        "summary": "Parcel pickup", "content": "Collect parcel at the west gate.",
        "retrieval_cues": cues,
    }}
    typed = mutation_from_dict(payload)
    assert typed.card is not None
    assert typed.card.retrieval_cues == cues
    assert typed.as_dict()["card"]["retrieval_cues"] == cues
    cue_schema = schemas()["Card"]["properties"]["retrieval_cues"]
    assert cue_schema["type"] == "array"
    assert cue_schema["items"] == {"type": "string"}


def test_default_field_map_passes_existing_cues_through_without_rewriting_them():
    cues = ["parcel", "pickup code", "2026-09-01"]
    projected = to_card({
        "id": "external-1", "summary": "Parcel pickup",
        "content": "Collect parcel at the west gate.",
        "retrieval_cues": cues,
    })
    assert projected["retrieval_cues"] == cues


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("entry", ["sdk", "host_driven"])
def test_cues_survive_capture_store_reopen_and_export(store_kind, entry, tmp_path):
    cues = ["parcel", "pickup code", "2026-09-01"]
    raw = json.dumps({"cards": [{
        "action": "add", "type": "fact", "summary": "Parcel pickup",
        "content": "Collect parcel AB-123 at the west gate on September 1.",
        "retrieval_cues": cues,
    }]})

    class Model:
        def complete(self, prompt, *, purpose=""):
            return raw

    db = tmp_path / "retrieval-cues.db"
    store = InMemoryStore() if store_kind == "memory" else SqliteStore(db)
    garden = MountedGarden(model=Model() if entry == "sdk" else None, store=store)
    scope = Scope(tenant_id="tenant", memory_owner_id="owner")
    if entry == "sdk":
        receipt = garden.capture_and_store(scope, CaptureRequest(
            window="Remember parcel AB-123.", locale="en",
            idempotency_key="capture-cues",
        ))
        assert receipt.written and not receipt.error
    else:
        service = Service(garden)
        wire_scope = {"tenant_id": "tenant", "memory_owner_id": "owner"}
        begun = service.handle({"id": "begin", "method": "capture.begin", "params": {
            "scope": wire_scope, "window": "Remember parcel AB-123.",
            "locale": "en", "idempotency_key": "host-cues",
        }})
        assert begun["ok"] and begun["result"]["status"] == "needs_model", begun
        finished = service.handle({"id": "feed", "method": "capture.feed", "params": {
            "session_id": begun["result"]["session_id"], "reply": raw,
        }})
        assert finished["ok"], finished
        assert finished["result"]["result"]["written"], finished

    reopened = SqliteStore(db) if store_kind == "sqlite" else store
    exported = MountedGarden(model=None, store=reopened).export(scope).items.records
    assert len(exported) == 1
    assert exported[0]["retrieval_cues"] == cues


def test_dream_cues_survive_the_actual_maintenance_mutation_and_typed_round_trip():
    cues = ["weekend running", "park", "Sunday 08:00"]
    old_cards = [{
        "id": f"m_{i}", "summary": f"Running note {i}",
        "content": "They run in the park every weekend morning.",
    } for i in range(10)]
    reply = json.dumps({"consolidations": [{
        "op": "merge", "card_ids": ["m_0", "m_1"],
        "rationale": "These notes describe the same recurring habit.",
        "result": {
            "summary": "Weekend park running habit",
            "content": "They regularly run in the park on weekend mornings.",
            "retrieval_cues": cues,
        },
    }]})

    class Model:
        def complete(self, prompt, *, purpose=""):
            return reply

    result = GardenComponent(model=Model()).run_maintenance(MaintenanceRequest(
        cards=old_cards, locale="en",
    ))
    assert result.error is None and len(result.mutations) == 1
    mutation = result.mutations[0]
    assert mutation["card"]["retrieval_cues"] == cues
    assert mutation["card"]["source"] == "memory_dream"
    assert mutation_from_dict(mutation).as_dict()["card"]["retrieval_cues"] == cues
