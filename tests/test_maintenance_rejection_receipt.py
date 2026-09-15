"""A rejected plan is not a successful empty maintenance decision."""
import json

import pytest

from memgarden import MaintenanceRequest, MountedGarden, Scope
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_all_unsafe_proposals_preserve_cards_and_ledger(kind, tmp_path):
    store = InMemoryStore() if kind == "memory" else SqliteStore(str(tmp_path / "g.db"))
    scope = Scope(tenant_id="t", memory_owner_id="a")
    store.apply("t", [{"op": "add", "card": {
        "id": f"card{i:04d}", "summary": f"Activity {i}",
        "content": "A complete account of the activity. " * 20,
        "bucket": "Life"}} for i in range(15)], owner="a", idempotency_key="seed")

    class Model:
        def complete(self, prompt, *, purpose=""):
            return json.dumps({"consolidations": [{
                "op": "merge", "rationale": "Same activity", "card_ids": ["card0000", "card0001"],
                "result": {"summary": "Merged activity", "content": "A substantive merged account.",
                           "bucket": "Life", "threads": [], "importance": 0.5, "pulse": 0.2},
            }]})

    garden = MountedGarden(model=Model(), store=store)
    before = garden.maintenance_ledger(scope)
    receipt = garden.run_and_store_maintenance(
        scope, MaintenanceRequest(locale="en", card_body_chars=20))
    assert receipt.error == "maintenance_targets_rejected"
    assert not receipt.written
    assert garden.maintenance_ledger(scope) == before
    assert len(store.load("t", owner="a").cards) == 15
