"""Regression evidence for cross-connection reads and mounted/Store parity."""
import pytest

from memgarden import CaptureRequest, GardenComponent, MountedGarden, Scope, SqliteStore
from memgarden.storage import RevisionConflict
from memgarden.stores.memory import InMemoryStore

SCOPE = Scope(tenant_id="audit", memory_owner_id="person")


class Model:
    def __init__(self):
        self.prompts = []

    def complete(self, prompt, *, purpose=""):
        self.prompts.append(prompt)
        return '{"cards": []}'


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else SqliteStore(str(tmp_path / "audit.db"))


def seed(store, cards):
    return store.apply("audit", [{"op": "add", "card": c} for c in cards],
                       owner="person", idempotency_key="seed")


def test_mounted_capture_uses_same_relevant_index_and_budget_as_component(store):
    cards = [{"id": f"f{i}", "summary": f"Family decoration {i}",
              "content": "Unrelated family details", "importance": .9} for i in range(70)]
    cards.append({"id": "job", "summary": "Worked at Acme as an engineer",
                  "content": "Old job", "importance": .1})
    seed(store, cards)
    request = CaptureRequest(window="I left Acme and joined Globex as an engineer.",
                             locale="en", index_cards_limit=1, index_budget_chars=100)
    direct, mounted = Model(), Model()
    from dataclasses import replace
    GardenComponent(model=direct).capture(replace(request, existing_cards=cards))
    MountedGarden(model=mounted, store=store).capture_and_store(SCOPE, request)
    assert "- job:" in direct.prompts[0]
    assert "- job:" in mounted.prompts[0]
    assert not any(line.startswith("- f") for line in mounted.prompts[0].splitlines())


def test_delete_on_nondefault_only_scope_and_retry(store):
    seed(store, [{"id": "u", "summary": "User private", "content": "Owned data",
                  "mount": "user-private"},
                 {"id": "hidden", "summary": "Hidden", "content": "Private"}])
    scope = Scope(tenant_id="audit", memory_owner_id="person", allowed_mounts=("user-private",))
    garden = MountedGarden(model=Model(), store=store)
    assert garden.delete_record(scope, "hidden", requested_by="person").error == "record_not_found"
    assert garden.delete_record(scope, "u", requested_by="person").error is None
    assert garden.delete_record(scope, "u", requested_by="person").error is None
    assert len(garden.browse(scope)) == 0


def test_inmemory_isolates_nested_inputs_and_cached_outputs():
    store = InMemoryStore()
    threads = ["original"]
    first = seed(store, [{"id": "a", "summary": "Memory", "content": "Body", "threads": threads}])
    revision = first.revision
    first.results.clear()
    threads.append("changed-without-write")
    after = store.load("audit", owner="person")
    assert after.cards[0]["threads"] == ["original"]
    assert after.revision == revision
    replay = seed(store, [{"id": "a", "summary": "Memory", "content": "Body", "threads": ["original"]}])
    assert replay.results[0]["id"] == "a"
    ledger = {"mount": "agent-private", "reviewed": {"a": "version"}}
    store.apply("audit", [], owner="person", idempotency_key="ledger", maintenance_state=ledger)
    ledger["reviewed"].clear()
    read = store.maintenance_state("audit", owner="person", mount="agent-private")
    read["reviewed"].clear()
    assert store.maintenance_state("audit", owner="person", mount="agent-private")["reviewed"] == {"a": "version"}


def test_sqlite_snapshot_is_coherent_across_connections(tmp_path):
    path = str(tmp_path / "two-connections.db")
    reader, writer = SqliteStore(path), SqliteStore(path)
    seed(reader, [{"id": "a", "summary": "Preference", "content": "old value"}])
    real_read = reader._cards_of

    def interleaved_read(conn, tenant, owner):
        cards = real_read(conn, tenant, owner)
        writer.apply("audit", [{"op": "update", "record_id": "a",
                      "changes": {"content": "concurrent new value"}}],
                     owner="person", idempotency_key="concurrent-change")
        return cards

    reader._cards_of = interleaved_read
    snapshot = reader.load("audit", owner="person")
    reader._cards_of = real_read
    assert snapshot.cards[0]["content"] == "old value"
    assert snapshot.revision != writer.load("audit", owner="person").revision
    with pytest.raises(RevisionConflict):
        reader.apply("audit", [{"op": "update", "record_id": "a",
                      "changes": {"content": "derived from stale old value"}}],
                     owner="person", expected_revision=snapshot.revision,
                     idempotency_key="stale-derived-update")
    assert writer.load("audit", owner="person").cards[0]["content"] == "concurrent new value"


def test_reference_host_follows_export_and_browse_cursors():
    from memgarden.conformance import ReferenceHost
    host = ReferenceHost("memory")
    host.store.apply(host.tenant, [{"op": "add", "card": {
        "id": f"c{i:05d}", "summary": f"Memory {i}", "content": f"Body {i}"}}
        for i in range(1001)], owner="person", idempotency_key="seed-many")
    assert host.fetch("person", ["c01000"])[0]["id"] == "c01000"
    assert len(host.index("person")) == len(host.history("person")) == 1001
