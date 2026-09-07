"""Card write times are persisted facts, not merely declared Record fields."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from memgarden import CaptureRequest, MountedGarden, Scope
from memgarden.selection import Chain, RecentStage
from memgarden.storage import IdempotencyConflict, MutationRejected, RevisionConflict
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore
from memgarden.timestamps import parse_ts


T0 = "2026-09-08T00:00:00Z"
T1 = "2026-09-08T01:00:00Z"
T2 = "2026-09-08T02:00:00Z"
SCOPE = Scope(tenant_id="tenant", memory_owner_id="owner")


class Clock:
    value = T0

    def now_iso(self):
        return self.value


def add(card_id="card", **fields):
    return {"op": "add", "card": {
        "id": card_id, "summary": "Work decision", "content": "They changed jobs.", **fields}}


def apply(store, operations, key="write", **kwargs):
    return store.apply("tenant", operations, owner="owner", idempotency_key=key, **kwargs)


def cards(store):
    return {c["id"]: c for c in store.load(
        "tenant", owner="owner", include_archived=True, include_superseded=True).cards}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_new_cards_have_real_creation_and_update_times(kind, tmp_path):
    store = InMemoryStore() if kind == "memory" else SqliteStore(tmp_path / "real.db")
    apply(store, [add()])
    card = cards(store)["card"]
    assert parse_ts(card.get("created_at")) is not None
    assert card["updated_at"] == card["created_at"]
    assert not card.get("occurred_at")


@pytest.fixture(params=["memory", "sqlite"])
def timed_store(request, tmp_path):
    clock = Clock()
    store = (InMemoryStore(clock=clock) if request.param == "memory"
             else SqliteStore(tmp_path / "timed.db", clock=clock))
    return store, clock


def test_changes_update_time_but_reads_noops_and_replays_do_not(timed_store):
    store, clock = timed_store
    original = add(occurred_at="2020-02-29")
    untouched = copy.deepcopy(original)
    receipt = apply(store, [original], "add")
    assert original == untouched  # generated times must not enter the replay digest
    clock.value = T1
    assert apply(store, [original], "add") == receipt
    for index, operation in enumerate([
        {"op": "no_op"}, {"op": "update", "record_id": "card", "changes": {}},
        {"op": "update", "record_id": "card", "changes": {"content": "They changed jobs."}},
    ]):
        assert apply(store, [operation], f"noop-{index}").revision == receipt.revision
        assert cards(store)["card"]["updated_at"] == T0
    update = {"op": "update", "record_id": "card", "changes": {"content": "They changed careers."}}
    changed = apply(store, [update], "update")
    assert cards(store)["card"] == {
        **untouched["card"], "content": "They changed careers.",
        "created_at": T0, "updated_at": T1,
    }
    clock.value = T2
    assert apply(store, [update], "update") == changed
    assert cards(store)["card"]["updated_at"] == T1
    with pytest.raises(IdempotencyConflict):
        apply(store, [add(content="Different")], "add")
    with pytest.raises(RevisionConflict):
        apply(store, [update], "stale", expected_revision=receipt.revision)
    assert cards(store)["card"]["updated_at"] == T1


def test_archive_and_promote_only_stamp_actual_changes(timed_store):
    store, clock = timed_store
    apply(store, [add(mount="agent-private")], "add")
    clock.value = T1
    archive = {"op": "archive", "record_id": "card", "reason": "old"}
    apply(store, [archive], "archive")
    assert cards(store)["card"]["updated_at"] == T1
    clock.value = T2
    apply(store, [archive], "archive-again")
    assert cards(store)["card"]["updated_at"] == T1
    promote = {"op": "promote", "record_id": "card", "to_mount": "family-shared"}
    apply(store, [promote], "promote")
    assert cards(store)["card"]["updated_at"] == T2
    clock.value = "2026-09-08T03:00:00Z"
    apply(store, [promote], "promote-again")
    assert cards(store)["card"]["updated_at"] == T2
    assert cards(store)["card"]["created_at"] == T0


def test_supersede_dates_new_card_and_updates_each_old_card(timed_store):
    store, clock = timed_store
    apply(store, [add("a"), add("b")], "seed")
    clock.value = T1
    apply(store, [{"op": "supersede", "target_ids": ["a", "b"],
                   "card": {**add("merged")["card"], "source": "memory_dream"}}], "merge")
    result = cards(store)
    for old_id in ("a", "b"):
        assert result[old_id]["created_at"] == T0
        assert result[old_id]["updated_at"] == T1
        assert result[old_id]["superseded_by"] == "merged"
    assert result["merged"]["created_at"] == result["merged"]["updated_at"] == T1


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_regular_update_cannot_forge_managed_times(timed_store, field):
    store, clock = timed_store
    apply(store, [add()], "add")
    clock.value = T1
    with pytest.raises(MutationRejected):
        apply(store, [{"op": "update", "record_id": "card", "changes": {field: T2}}])
    assert cards(store)["card"]["created_at"] == cards(store)["card"]["updated_at"] == T0


def test_explicit_historical_times_survive_initial_restore(timed_store):
    store, clock = timed_store
    apply(store, [add(created_at="2020-01-01", updated_at="2020-02-01")], "restore")
    assert cards(store)["card"]["created_at"] == "2020-01-01"
    assert cards(store)["card"]["updated_at"] == "2020-02-01"
    apply(store, [{"op": "update", "record_id": "card", "changes": {"summary": "New title"}}])
    assert cards(store)["card"]["created_at"] == "2020-01-01"
    assert cards(store)["card"]["updated_at"] == T0


@pytest.mark.parametrize("empty", [None, "", " "])
def test_empty_new_card_times_are_filled_and_clock_is_normalized(timed_store, empty):
    store, clock = timed_store
    clock.value = "2026-09-08T08:00:00.123456+08:00"
    apply(store, [add(created_at=empty, updated_at=empty)])
    card = cards(store)["card"]
    assert card["created_at"] == card["updated_at"] == "2026-09-08T00:00:00.123456Z"


def test_legacy_card_missing_creation_time_is_not_backfilled(timed_store):
    store, clock = timed_store
    legacy = add()["card"]
    # Simulate an actual pre-fix row, bypassing the now-correct new-card writer.
    if isinstance(store, InMemoryStore):
        store._cards[("tenant", "owner")] = {"card": legacy}
    else:
        with sqlite3.connect(store._path) as conn:
            conn.execute("INSERT INTO cards(tenant, owner, id, doc) VALUES(?,?,?,?)",
                         ("tenant", "owner", "card", json.dumps(legacy)))
        store = SqliteStore(store._path, clock=clock)
    assert "created_at" not in cards(store)["card"]
    assert "updated_at" not in cards(store)["card"]
    apply(store, [{"op": "update", "record_id": "card", "changes": {"summary": "New title"}}])
    assert "created_at" not in cards(store)["card"]
    assert cards(store)["card"]["updated_at"] == T0


def test_failed_batch_rolls_back_times_and_retry_uses_successful_write_time(timed_store):
    store, clock = timed_store
    apply(store, [add()], "seed")
    clock.value = T1
    update = {"op": "update", "record_id": "card", "changes": {"summary": "New title"}}
    with pytest.raises(MutationRejected):
        apply(store, [update, {"op": "update", "record_id": "missing", "changes": {}}], "retry")
    assert cards(store)["card"]["updated_at"] == T0
    clock.value = T2
    apply(store, [update], "retry")
    assert cards(store)["card"]["updated_at"] == T2


def test_sqlite_reopen_preserves_times_and_replay_receipt(tmp_path):
    clock = Clock()
    db = tmp_path / "reopen.db"
    first = SqliteStore(db, clock=clock)
    receipt = apply(first, [add()])
    clock.value = T1
    second = SqliteStore(db, clock=clock)
    assert apply(second, [add()]) == receipt
    assert cards(second)["card"]["created_at"] == cards(second)["card"]["updated_at"] == T0


def test_sqlite_write_failure_rolls_back_already_executed_time_update(tmp_path):
    class FailingStore(SqliteStore):
        fail = False

        def _put(self, conn, tenant, owner, card):
            super()._put(conn, tenant, owner, card)
            if self.fail:
                raise sqlite3.OperationalError("injected failure after real SQL write")

    clock = Clock()
    db = tmp_path / "rollback.db"
    store = FailingStore(db, clock=clock)
    apply(store, [add()], "seed")
    clock.value = T1
    store.fail = True
    update = {"op": "update", "record_id": "card", "changes": {"summary": "New title"}}
    with pytest.raises(sqlite3.OperationalError):
        apply(store, [update], "retry")
    reopened = SqliteStore(db, clock=clock)
    assert cards(reopened)["card"]["updated_at"] == T0
    clock.value = T2
    apply(reopened, [update], "retry")
    assert cards(reopened)["card"]["updated_at"] == T2


def test_capture_export_and_recent_selection_use_persisted_times(timed_store):
    store, clock = timed_store

    class Model:
        def complete(self, prompt, *, purpose=""):
            return json.dumps({"cards": [{"action": "add", "summary": "Career change",
                                           "content": "They decided to change careers."}]})

    garden = MountedGarden(model=Model(), store=store,
        selection_policy=Chain(stages=(RecentStage(limit=1),)))
    apply(store, [add("z-old")], "old")
    clock.value = T1
    receipt = garden.capture_and_store(SCOPE, CaptureRequest(
        window="I decided to change careers.", locale="en", idempotency_key="capture"))
    assert receipt.written and not receipt.error
    exported = garden.export(SCOPE).items.records
    new = next(card for card in exported if card["id"] != "z-old")
    assert new["created_at"] == new["updated_at"] == T1
    assert garden.context_for_turn(SCOPE, "anything").record_ids == [new["id"]]
