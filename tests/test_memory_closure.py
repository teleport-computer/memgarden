"""Public SDK/wire closure: complete reads, original-request replay and progress."""
import json
from dataclasses import replace

import pytest

from memgarden import CaptureRequest, MaintenanceRequest, MountedGarden, Scope, SqliteStore, ToolCall
from memgarden.service import Service
from memgarden.storage import IdempotencyConflict
from memgarden.stores.memory import InMemoryStore

SCOPE = Scope(tenant_id="t", memory_owner_id="alice")
WIRE_SCOPE = {"tenant_id": "t", "memory_owner_id": "alice"}


class Model:
    def __init__(self, reply='{"consolidations": []}'):
        self.reply, self.prompts = reply, []

    def complete(self, prompt, *, purpose=""):
        self.prompts.append(prompt)
        return self.reply


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "g.db")


def seed(store, cards, key="seed"):
    return store.apply("t", [{"op": "add", "card": c} for c in cards],
                       owner="alice", idempotency_key=key)


def invoke(service, method, **params):
    response = service.handle({"method": method, "params": params})
    assert response["ok"], response
    return response["result"]


def test_search_then_lossless_full_read_and_cursor_invalidation(store):
    content = "Nora 的礼物是一条黄色裙子，48 dollars。" + "🌼" * 21000
    seed(store, [{"id": "gift", "summary": "Nora birthday gift", "content": content}])
    garden = MountedGarden(model=None, store=store)
    found = garden.invoke_tool(SCOPE, ToolCall("memory_search", {"query": "Nora"}))
    assert "[gift]" in found.content
    text, cursor = "", ""
    while True:
        chunk = garden.invoke_tool(SCOPE, ToolCall("memory_read", {"record_id": "gift", "cursor": cursor}))
        assert chunk.ok
        data = json.loads(chunk.content)
        assert len(data["text"]) <= 5000
        text += data["text"]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert json.loads(text)["content"] == content
    first = garden.read_record(SCOPE, "gift", limit=17)
    store.apply("t", [{"op": "update", "record_id": "gift", "changes": {"content": "changed"}}],
                owner="alice", idempotency_key="edit")
    with pytest.raises(ValueError, match="record_changed"):
        garden.read_record(SCOPE, "gift", cursor=first["next_cursor"])
    for limit in (0, -1, 20001, True):
        with pytest.raises(ValueError):
            garden.read_record(SCOPE, "gift", limit=limit)


def test_wire_read_filters_tenant_owner_mount_and_lifecycle(store):
    seed(store, [{"id": "active", "summary": "Visible", "content": "body"},
                 {"id": "other-mount", "summary": "Hidden", "content": "body", "mount": "shared"},
                 {"id": "archived", "summary": "Retired", "content": "body", "archived": True}])
    service = Service(MountedGarden(model=None, store=store), model_available=False)
    assert "body" in invoke(service, "records.get", scope=WIRE_SCOPE, record_id="active")["text"]
    for scope, rid in ((WIRE_SCOPE, "other-mount"), (WIRE_SCOPE, "archived"),
                       ({"tenant_id": "other", "memory_owner_id": "alice"}, "active"),
                       ({"tenant_id": "t", "memory_owner_id": "bob"}, "active")):
        assert invoke(service, "records.get", scope=scope, record_id=rid) == {"error": "record_not_found"}


@pytest.mark.parametrize("empty", [False, True])
def test_capture_replay_survives_new_garden_and_model_variation(store, empty):
    model = Model('{"cards": []}' if empty else json.dumps({"cards": [{
        "action": "add", "summary": "Likes jasmine tea", "content": "Alice likes jasmine tea.", "bucket": "Food"}]}))
    garden = MountedGarden(model=model, store=store)
    request = CaptureRequest(window="User: I like jasmine tea.", locale="en", idempotency_key="turn-1")
    first = garden.capture_and_store(SCOPE, request)
    assert first.error is None
    if isinstance(store, SqliteStore):
        store = SqliteStore(store._path)
    changed_model = Model("INVALID IF CALLED")
    fresh = MountedGarden(model=changed_model, store=store)
    replay = fresh.capture_and_store(SCOPE, request)
    assert replay.error is None and replay.record_ids == first.record_ids
    assert not changed_model.prompts
    assert fresh.capture_and_store(SCOPE, replace(request, window="different input")).error == "idempotency_conflict"
    if first.record_ids:
        assert fresh.delete_record(SCOPE, first.record_ids[0], requested_by="alice").error is None
        replay = fresh.capture_and_store(SCOPE, request)
        assert replay.error is None and not fresh.browse(SCOPE)


def test_capture_wire_replay_short_circuits_begin(store):
    service = Service(MountedGarden(model=None, store=store), model_available=False)
    params = dict(scope=WIRE_SCOPE, window="User: I like jasmine tea.", locale="en", idempotency_key="wire-turn")
    begin = invoke(service, "capture.begin", **params)
    done = invoke(service, "capture.feed", session_id=begin["session_id"], reply='{"cards": []}')
    assert done["status"] == "completed" and done["result"]["error"] is None
    restarted = Service(MountedGarden(model=None, store=store), model_available=False)
    assert invoke(restarted, "capture.begin", **params)["status"] == "completed"


def test_distinct_tool_save_after_delete_and_stable_retry(store):
    garden = MountedGarden(model=None, store=store)
    args = {"summary": "Likes tea", "content": "Alice likes tea."}
    first = garden.invoke_tool(SCOPE, ToolCall("memory_write", args, idempotency_key="call-1"))
    rid = first.mutations[0]["record_id"]
    garden.delete_record(SCOPE, rid, requested_by="alice")
    replay = garden.invoke_tool(SCOPE, ToolCall("memory_write", args, idempotency_key="call-1"))
    assert replay.ok and not garden.browse(SCOPE)
    second = garden.invoke_tool(SCOPE, ToolCall("memory_write", args, idempotency_key="call-2"))
    assert second.ok and second.mutations[0]["record_id"] != rid
    garden.delete_record(SCOPE, second.mutations[0]["record_id"], requested_by="alice")
    assert garden.invoke_tool(SCOPE, ToolCall("memory_write", args)).ok
    assert len(garden.browse(SCOPE)) == 1


def test_explicit_write_without_key_can_save_same_fact_after_deletion(store):
    from memgarden import CuratedWriteRequest
    garden = MountedGarden(model=Model(), store=store)
    request = CuratedWriteRequest(text="Alice likes jasmine tea.", locale="en")
    first = garden.write_one(SCOPE, request)
    assert first.error is None and first.record_ids
    garden.delete_record(SCOPE, first.record_ids[0], requested_by="alice")
    second = garden.write_one(SCOPE, request)
    assert second.error is None and second.record_ids != first.record_ids
    assert len(garden.browse(SCOPE)) == 1


def test_sqlite_two_connections_commit_one_request_outcome(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = tmp_path / "concurrent.db"
    stores = [SqliteStore(path), SqliteStore(path)]
    barrier = Barrier(2)

    def write(i):
        barrier.wait(timeout=5)
        return stores[i].apply("t", [{"op": "add", "card": {
            "summary": f"Generated wording {i}", "content": "Same original fact"}}],
            owner="alice", idempotency_key="concurrent", request_digest="original-input")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert results[0] == results[1]
    assert len(SqliteStore(path).load("t", owner="alice").cards) == 1


def test_same_request_with_different_generated_output_commits_once(store):
    def mutation(body):
        return [{"op": "add", "card": {"summary": "Tea", "content": body}}]
    first = store.apply("t", mutation("Likes tea"), owner="alice", idempotency_key="request",
                        request_digest="original-input")
    replay = store.apply("t", mutation("Prefers tea"), owner="alice", idempotency_key="request",
                         request_digest="original-input", expected_revision="stale")
    assert replay == first
    with pytest.raises(IdempotencyConflict):
        store.apply("t", mutation("Likes tea"), owner="alice", idempotency_key="request")


def test_incremental_restart_edit_related_old_and_no_self_trigger(store):
    seed(store, [{"id": "trip", "summary": "Kyoto autumn trip", "content": "Booked Kyoto flights",
                  "threads": ["Kyoto trip"]},
                 {"id": "food", "summary": "Likes tea", "content": "Jasmine tea"}])
    model = Model()
    garden = MountedGarden(model=model, store=store)
    assert garden.run_and_store_maintenance(SCOPE, MaintenanceRequest(locale="en")).error is None
    assert not garden.check_maintenance(SCOPE).needed
    if isinstance(store, SqliteStore):
        store = SqliteStore(store._path)
    garden = MountedGarden(model=model, store=store)
    seed(store, [{"id": "hotel", "summary": "Kyoto autumn hotel", "content": "Booked a hotel",
                  "threads": ["Kyoto trip"]}], key="new")
    prepared, _ = garden.prepare_maintenance(SCOPE, MaintenanceRequest(locale="en", cards_limit=2))
    assert prepared.pending_ids == ("hotel",)
    assert {c["id"] for c in prepared.cards} == {"hotel", "trip"}
    assert garden.run_and_store_maintenance(SCOPE, MaintenanceRequest(locale="en")).error is None
    store.apply("t", [{"op": "update", "record_id": "food", "changes": {"content": "Now green tea"}}],
                owner="alice", idempotency_key="edit")
    assert garden.check_maintenance(SCOPE).needed
    model.reply = json.dumps({"consolidations": [{"op": "thicken", "card_ids": ["food"],
        "rationale": "Preserve updated tea preference", "result": {"summary": "Green tea preference",
        "content": "Alice now drinks green tea.", "bucket": "Food", "threads": [], "importance": .4, "pulse": .1}}]})
    assert garden.run_and_store_maintenance(SCOPE, MaintenanceRequest(locale="en")).error is None
    assert not garden.check_maintenance(SCOPE).needed


def test_dream_budget_error_does_not_advance_ledger(store):
    seed(store, [{"id": "long", "summary": "Long card", "content": "x" * 100}])
    model = Model()
    garden = MountedGarden(model=model, store=store)
    receipt = garden.run_and_store_maintenance(SCOPE, MaintenanceRequest(locale="en", card_body_chars=10))
    assert receipt.error == "maintenance_budget_too_small"
    assert not model.prompts and not garden.maintenance_ledger(SCOPE)
    assert garden.check_maintenance(SCOPE).needed


def test_invalid_progress_rolls_back_cards_and_receipt(store):
    before = store.load("t", owner="alice")
    with pytest.raises((TypeError, ValueError)):
        store.apply("t", [{"op": "add", "card": {"summary": "Must roll back", "content": "Body"}}],
                    owner="alice", idempotency_key="invalid-progress", request_digest="request",
                    maintenance_state={"mount": "agent-private", "reviewed_versions": [1]})
    assert store.load("t", owner="alice") == before
    assert store.request_receipt("t", owner="alice", idempotency_key="invalid-progress",
                                 request_digest="request") is None


def test_dry_maintenance_never_commits_progress(store):
    seed(store, [{"id": "a", "summary": "Fact", "content": "Known detail"}])
    before = store.load("t", owner="alice")
    model = Model()
    garden = MountedGarden(model=model, store=store)
    result = garden.run_and_store_maintenance(SCOPE, MaintenanceRequest(locale="en", dry_run=True))
    assert result.reason == "dry_run" and not result.written and not model.prompts
    assert store.load("t", owner="alice") == before
    assert not garden.maintenance_ledger(SCOPE)


def test_legacy_sqlite_progress_upgrade_and_original_receipt_boundary(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    store = SqliteStore(path)
    seed(store, [{"id": "old", "summary": "Old fact", "content": "Preserved"}])
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE maintenance_state DROP COLUMN reviewed_versions")
        conn.execute("ALTER TABLE applied DROP COLUMN request_digest")
        conn.execute("PRAGMA user_version=3")
    upgraded = SqliteStore(path)
    assert upgraded.load("t", owner="alice").cards[0]["content"] == "Preserved"
    assert MountedGarden(model=None, store=upgraded).check_maintenance(SCOPE).needed
    with pytest.raises(IdempotencyConflict):
        upgraded.request_receipt("t", owner="alice", idempotency_key="seed", request_digest="unknown")


@pytest.mark.parametrize("op", ["update", "supersede"])
def test_memory_nested_updates_do_not_alias_caller(op):
    store = InMemoryStore()
    seed(store, [{"id": "original", "summary": "Original", "content": "Body"}])
    threads = ["one"]
    mutation = ({"op": "update", "record_id": "original", "changes": {"threads": threads}}
                if op == "update" else {"op": "supersede", "target_id": "original",
                    "card": {"summary": "Replacement", "content": "New body", "threads": threads}})
    store.apply("t", [mutation], owner="alice", idempotency_key="change")
    before = store.load("t", owner="alice")
    threads.append("without-write")
    assert store.load("t", owner="alice") == before
