"""最终收尾的回归测试：覆盖此前“绿灯但语义错误”的边界。"""
from __future__ import annotations

import json

import pytest

from memgarden.contracts import Actor, ImportRequest, MaintenanceRequest
from memgarden.importing import ImportProgress
from memgarden.mounted import MountedGarden, OperationReceipt, Scope
from memgarden.service import Service
from memgarden.schema import method_schemas, schemas
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore
from memgarden.storage import FULL_CAPABILITIES, Snapshot
from memgarden.validate import validate


SCOPE = Scope(tenant_id="tenant", memory_owner_id="owner")


class _Tidy:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return json.dumps({"consolidations": [{
            "op": "merge",
            "card_ids": ["m_1", "m_2"],
            "rationale": "同一件事",
            "result": {"summary": "合并", "content": "合并后的正文"},
        }]}, ensure_ascii=False)


class _MustNotCallModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        raise AssertionError("host-driven 路径不应由 memgarden 调模型")


def _seed(store, count: int = 3) -> None:
    store.apply(
        "tenant",
        [{"op": "add", "card": {"id": f"m_{i}",
                                    "summary": f"卡 {i}", "content": "正文"}}
         for i in range(1, count + 1)],
        owner="owner", idempotency_key="seed", expected_revision=None,
    )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_no_op_does_not_advance_card_revision(store_kind, tmp_path):
    store = (InMemoryStore() if store_kind == "memory"
             else SqliteStore(tmp_path / "noop.db"))
    before = store.load("tenant", owner="owner").revision
    out = store.apply(
        "tenant", [{"op": "no_op", "reason": "checked"}],
        owner="owner", idempotency_key="noop", expected_revision=before,
    )
    assert out.revision == before
    assert store.load("tenant", owner="owner").revision == before


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_maintenance_watermark_is_monotonic_and_dream_cards_do_not_self_trigger(
    store_kind, tmp_path,
):
    store = (InMemoryStore() if store_kind == "memory"
             else SqliteStore(tmp_path / "maintenance.db"))
    _seed(store)
    garden = MountedGarden(model=_Tidy(), store=store,
                           min_new_cards_for_maintenance=1)
    first = garden.run_and_store_maintenance(
        SCOPE, MaintenanceRequest(locale="zh-Hans"))
    assert first.written, first.error

    everything = store.load(
        "tenant", owner="owner", include_archived=True,
        include_superseded=True).cards
    dream_cards = [c for c in everything if c.get("source") == "memory_dream"]
    assert len(dream_cards) == 1
    assert garden.check_maintenance(SCOPE).needed is False

    revision = store.load("tenant", owner="owner").revision
    store.apply(
        "tenant", [{"op": "add", "card": {
            "summary": "后来新增", "content": "新的原始记忆"}}],
        owner="owner", idempotency_key="new-seed",
        expected_revision=revision,
    )
    check = garden.check_maintenance(SCOPE)
    assert check.needed is True
    assert check.trace["new_cards"] == 1


def test_host_driven_maintenance_works_without_a_service_model():
    store = InMemoryStore()
    _seed(store)
    service = Service(MountedGarden(
        model=_MustNotCallModel(), store=store,
        min_new_cards_for_maintenance=1))
    scope = {"tenant_id": "tenant", "memory_owner_id": "owner"}

    begun = service.handle({
        "id": "1", "method": "maintenance.begin",
        "params": {"scope": scope, "locale": "zh-Hans"},
    })
    assert begun["ok"] and begun["result"]["status"] == "needs_model"
    sid = begun["result"]["session_id"]
    reply = json.dumps({"consolidations": [{
        "op": "merge", "card_ids": ["m_1", "m_2"],
        "rationale": "相同", "result": {
            "summary": "合并", "content": "合并后的正文"},
    }]}, ensure_ascii=False)
    finished = service.handle({
        "id": "2", "method": "maintenance.feed",
        "params": {"session_id": sid, "reply": reply},
    })
    assert finished["ok"]
    assert finished["result"]["status"] == "completed"
    assert finished["result"]["result"]["written"] is True
    validate(finished, method_schemas()["maintenance.feed"]["response"],
             schemas=schemas())
    active = store.load("tenant", owner="owner").cards
    assert len(active) == 2


def test_maintenance_with_no_consolidations_advances_the_ledger_once():
    class _NothingToMerge:
        def complete(self, prompt: str, *, purpose: str = "") -> str:
            return '{"consolidations": []}'

    store = InMemoryStore()
    _seed(store)
    garden = MountedGarden(model=_NothingToMerge(), store=store,
                           min_new_cards_for_maintenance=1)
    first = garden.run_and_store_maintenance(
        SCOPE, MaintenanceRequest(locale="zh-Hans"))
    assert first.written
    assert garden.check_maintenance(SCOPE).needed is False


def test_maintenance_idempotency_prefix_can_be_reused_for_a_new_snapshot():
    class _NothingToMerge:
        def complete(self, prompt: str, *, purpose: str = "") -> str:
            return '{"consolidations": []}'

    store = InMemoryStore()
    _seed(store)
    garden = MountedGarden(model=_NothingToMerge(), store=store,
                           min_new_cards_for_maintenance=1)
    request = MaintenanceRequest(locale="zh-Hans", idempotency_key="scheduled")
    assert garden.run_and_store_maintenance(SCOPE, request).written

    revision = store.load("tenant", owner="owner").revision
    store.apply(
        "tenant", [{"op": "add", "card": {
            "summary": "新快照", "content": "新增的原始记忆"}}],
        owner="owner", idempotency_key="next", expected_revision=revision,
    )
    assert garden.check_maintenance(SCOPE).needed
    assert garden.run_and_store_maintenance(SCOPE, request).written
    assert garden.check_maintenance(SCOPE).needed is False


def test_maintenance_check_uses_the_persisted_ledger_after_sqlite_reopen(tmp_path):
    path = tmp_path / "reopen.db"
    store = SqliteStore(path)
    _seed(store)
    garden = MountedGarden(model=_Tidy(), store=store,
                           min_new_cards_for_maintenance=1)
    assert garden.run_and_store_maintenance(
        SCOPE, MaintenanceRequest(locale="zh-Hans")).written
    reopened = MountedGarden(model=_Tidy(), store=SqliteStore(path),
                             min_new_cards_for_maintenance=1)
    assert reopened.check_maintenance(SCOPE).needed is False


def test_maintenance_fails_closed_when_the_store_has_no_ledger_contract():
    class _StoreWithoutLedger:
        def capabilities(self):
            return FULL_CAPABILITIES

        def load(self, tenant, *, owner, **filters):
            return Snapshot(cards=[], revision="0", owner=owner)

        def apply(self, tenant, mutations, *, owner, idempotency_key,
                  expected_revision, maintenance_state=None):
            raise AssertionError("账本缺失时不应尝试写")

    garden = MountedGarden(model=_MustNotCallModel(), store=_StoreWithoutLedger())
    check = garden.check_maintenance(SCOPE)
    assert check.needed is False
    assert check.error == "storage_failed:maintenance_state"
    run = garden.run_and_store_maintenance(
        SCOPE, MaintenanceRequest(locale="zh-Hans"))
    assert run.error == "storage_failed:maintenance_state"


def test_history_import_retry_clears_the_old_failure_and_finishes():
    garden = MountedGarden(model=_MustNotCallModel(), store=InMemoryStore())
    calls = 0

    def capture(scope, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return OperationReceipt(error="temporary")
        return OperationReceipt(written=True, record_ids=("m_1",))

    garden.capture_and_store = capture  # type: ignore[method-assign]
    request = ImportRequest(material="一段历史", locale="zh-Hans")
    first = garden.import_history(SCOPE, request)
    assert not first.done and first.failed
    second = garden.import_history(SCOPE, request, progress=first)
    assert second.done
    assert second.failed == []
    assert second.cards_written == 1


def test_history_import_wire_path_persists_and_resumes_without_duplicates():
    class _HistoryModel:
        def complete(self, prompt: str, *, purpose: str = "") -> str:
            return json.dumps({"cards": [{
                "action": "add", "bucket": "偏好与边界",
                "threads": ["饮食"], "summary": "不吃辣",
                "content": "对方不吃辣，一吃就胃疼。",
            }]}, ensure_ascii=False)

    store = InMemoryStore()
    service = Service(MountedGarden(model=_HistoryModel(), store=store))
    scope = {"tenant_id": "tenant", "memory_owner_id": "owner"}
    first = service.handle({
        "id": "import-1", "method": "history.import",
        "params": {"scope": scope, "material": "我不吃辣，一吃就胃疼。",
                   "locale": "zh-Hans", "max_batches": 1},
    })
    assert first["ok"] and first["result"]["done"] is True
    assert first["result"]["source_digest"]
    assert len(store.load("tenant", owner="owner").cards) == 1

    replay = service.handle({
        "id": "import-2", "method": "history.import",
        "params": {"scope": scope, "material": "我不吃辣，一吃就胃疼。",
                   "locale": "zh-Hans", "progress": first["result"]},
    })
    assert replay["ok"] and replay["result"]["done"] is True
    assert len(store.load("tenant", owner="owner").cards) == 1


def test_history_import_blank_material_finishes_and_changed_material_is_rejected():
    garden = MountedGarden(model=_MustNotCallModel(), store=InMemoryStore())
    blank = garden.import_history(
        SCOPE, ImportRequest(material="  \n  ", locale="zh-Hans"))
    assert blank.done and blank.cursor == blank.total

    progress = ImportProgress(cursor=1, total=3, source_digest="not-this-input")
    with pytest.raises(ValueError, match="另一份材料"):
        garden.import_history(
            SCOPE, ImportRequest(material="abc", locale="zh-Hans"),
            progress=progress)

    legacy = ImportProgress(cursor=1, total=3)
    with pytest.raises(ValueError, match="没有 source_digest"):
        garden.import_history(
            SCOPE, ImportRequest(material="abc", locale="zh-Hans"),
            progress=legacy)


def test_history_import_consumes_trailing_blank_batches_and_rejects_bad_cursor():
    garden = MountedGarden(model=_MustNotCallModel(), store=InMemoryStore())
    garden.capture_and_store = lambda scope, request: OperationReceipt(  # type: ignore[method-assign]
        reason="nothing_worth_keeping")
    material = "A\n" + " " * 7000
    progress = garden.import_history(
        SCOPE, ImportRequest(material=material, locale="zh-Hans"),
        max_batches=1)
    assert progress.done
    assert progress.cursor == len(material)

    digest = progress.source_digest
    with pytest.raises(ValueError, match="不是合法批次边界"):
        garden.import_history(
            SCOPE, ImportRequest(material=material, locale="zh-Hans"),
            progress=ImportProgress(cursor=1, total=len(material),
                                    source_digest=digest,
                                    import_fingerprint=(
                                        progress.import_fingerprint)))


def test_stored_actor_provenance_comes_from_trusted_scope():
    scope = Scope(tenant_id="tenant", memory_owner_id="owner",
                  actor=Actor(user_id="real-user", agent_id="real-agent",
                              session_id="real-session"))
    store = InMemoryStore()
    garden = MountedGarden(model=_MustNotCallModel(), store=store)
    receipt = garden._apply(  # 直接测统一写入关口，所有写路径都会经过这里。
        scope, "agent-private", [{
            "op": "add",
            "card": {"summary": "一条", "content": "正文",
                     "source_actor": {"user_id": "forged"}},
        }], idempotency_key="actor", trace={})
    assert receipt.written
    card = store.load("tenant", owner="owner").cards[0]
    assert card["source_actor"] == scope.actor.as_dict()
