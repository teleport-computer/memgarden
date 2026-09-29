"""Pre-merge regressions at the public preparation/commit boundary."""
import pytest

from memgarden import CaptureRequest, CaptureResult, MaintenanceRequest, MountedGarden, Scope, SqliteStore
from memgarden.stores.memory import InMemoryStore

SCOPE = Scope(tenant_id="tenant", memory_owner_id="owner")


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "acceptance.db")


def test_concurrent_empty_capture_winner_returns_empty_receipt(store, monkeypatch):
    garden = MountedGarden(model=None, store=store)
    request, revision = garden.prepare_capture(
        SCOPE, CaptureRequest(window="User: I enjoy tea.", locale="en", idempotency_key="turn"))
    actual_apply = store.apply

    def interleaved_apply(tenant, mutations, **kwargs):
        # A second execution commits an empty decision after the first execution's
        # receipt lookup but before its apply. Empty receipts do not change revision.
        actual_apply(tenant, [], **kwargs)
        return actual_apply(tenant, mutations, **kwargs)

    monkeypatch.setattr(store, "apply", interleaved_apply)
    receipt = garden.store_capture_result(SCOPE, request, CaptureResult(mutations=[{
        "op": "add", "card": {"summary": "Tea", "content": "Enjoys tea"}}]),
        expected_revision=revision)
    assert receipt.error is None
    assert not receipt.written
    assert receipt.reason == "nothing_worth_keeping"
    assert not receipt.record_ids and not garden.browse(SCOPE)


def test_prepared_capture_requires_snapshot_revision(store):
    garden = MountedGarden(model=None, store=store)
    request, _revision = garden.prepare_capture(
        SCOPE, CaptureRequest(window="User: I enjoy tea.", locale="en", idempotency_key="turn"))
    receipt = garden.store_capture_result(SCOPE, request, CaptureResult(mutations=[{
        "op": "add", "card": {"summary": "Tea", "content": "Enjoys tea"}}]))
    assert receipt.error == "expected_revision_required"
    assert not garden.browse(SCOPE)


def test_prepared_maintenance_requires_snapshot_revision(store):
    from memgarden import MaintenanceResult
    store.apply("tenant", [{"op": "add", "card": {
        "id": "card", "summary": "Tea", "content": "Enjoys tea"}}],
        owner="owner", idempotency_key="seed")
    garden = MountedGarden(model=None, store=store)
    request, _revision = garden.prepare_maintenance(SCOPE, MaintenanceRequest(locale="en"))
    receipt = garden.store_maintenance_result(
        SCOPE, request, MaintenanceResult(needed=True, trace={"reviewed_card_ids": ["card"]}))
    assert receipt.error == "expected_revision_required"
    assert not garden.maintenance_ledger(SCOPE)


def test_capture_and_maintenance_use_the_same_lifecycle_filter_as_reads(store):
    store.apply("tenant", [{"op": "add", "card": {
        "id": f"card-{status}", "summary": "Must not be reused", "content": "Old detail",
        "status": status}} for status in ("deleted", "archived", "superseded", "unknown")],
        owner="owner", idempotency_key="seed")
    garden = MountedGarden(model=None, store=store)
    request, _ = garden.prepare_capture(SCOPE, CaptureRequest(window="User: Hello.", locale="en"))
    assert not request.existing_cards
    assert not garden.check_maintenance(SCOPE).needed
