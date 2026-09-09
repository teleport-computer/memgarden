"""独立交叉审查发现的协议、存储与长驻服务边界。"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from memgarden.contracts import (
    Actor,
    CaptureRequest,
    CuratedWriteRequest,
    ImportRequest,
    MaintenanceRequest,
    MaintenanceResult,
    MigrateRequest,
    PromoteRequest,
)
from memgarden.importing import ImportProgress
from memgarden.mounted import MountedGarden, OperationReceipt, Scope
from memgarden.schema import method_schemas, schemas
from memgarden.service import Service
from memgarden.storage import Capabilities, FULL_CAPABILITIES, IdempotencyConflict
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore


SCOPE = Scope(
    tenant_id="tenant", memory_owner_id="owner",
    actor=Actor(user_id="u", agent_id="a", session_id="s"),
)
WIRE_SCOPE = {
    "tenant_id": "tenant", "memory_owner_id": "owner",
    "actor": {"user_id": "u", "agent_id": "a", "session_id": "s"},
}
CARD_REPLY = json.dumps({"cards": [{
    "action": "add", "summary": "喜欢安静", "content": "对方偏好安静的环境。",
    "bucket": "偏好与边界", "threads": ["环境"],
}]}, ensure_ascii=False)


class _ReplyModel:
    def __init__(self, reply: str = CARD_REPLY) -> None:
        self.reply = reply

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self.reply


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_supplied_then_deleted_numeric_id_is_never_reused(kind, tmp_path):
    store = (InMemoryStore() if kind == "memory"
             else SqliteStore(tmp_path / "ids.db"))
    store.apply("tenant", [{"op": "add", "card": {
        "id": "m_10", "summary": "旧卡", "content": "旧正文"}}],
        owner="owner", idempotency_key="supplied")
    store.apply("tenant", [{"op": "delete", "record_id": "m_10"}],
        owner="owner", idempotency_key="delete")
    result = store.apply("tenant", [{"op": "add", "card": {
        "summary": "新卡", "content": "新正文"}}],
        owner="owner", idempotency_key="generated")
    assert result.results[0]["id"] == "m_11"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_maintenance_ledger_is_part_of_idempotency_digest(kind, tmp_path):
    store = (InMemoryStore() if kind == "memory"
             else SqliteStore(tmp_path / "digest.db"))
    mutation = [{"op": "no_op", "reason": "checked"}]
    first = {"mount": "agent-private", "signature": "a",
             "seed_card_count": 1}
    second = {**first, "seed_card_count": 2}
    store.apply("tenant", mutation, owner="owner", idempotency_key="same",
                maintenance_state=first)
    with pytest.raises(IdempotencyConflict):
        store.apply("tenant", mutation, owner="owner", idempotency_key="same",
                    maintenance_state=second)


def test_sqlite_ledger_failure_rolls_back_cards_generation_and_revision(tmp_path):
    class _BrokenLedger(SqliteStore):
        def _put_ledger(self, conn, tenant, owner, state, revision):
            raise OSError("simulated ledger failure")

    store = _BrokenLedger(tmp_path / "rollback.db")
    before = store.load("tenant", owner="owner")
    with pytest.raises(OSError, match="ledger failure"):
        store.apply(
            "tenant", [{"op": "add", "card": {
                "summary": "不应留下", "content": "正文"}}],
            owner="owner", idempotency_key="atomic",
            expected_revision=before.revision,
            maintenance_state={"mount": "agent-private", "signature": "s",
                               "seed_card_count": 1})
    after = store.load("tenant", owner="owner")
    assert after.cards == []
    assert after.revision == before.revision
    assert after.seed_generations == {}
    assert store.maintenance_state(
        "tenant", owner="owner", mount="agent-private") == {}


def test_v2_sqlite_upgrade_never_moves_seed_waterline_below_ledger(tmp_path):
    path = tmp_path / "v2.db"
    store = SqliteStore(path)
    for i in range(1, 11):
        store.apply("tenant", [{"op": "add", "card": {
            "id": f"m_{i}", "summary": f"卡 {i}", "content": "正文"}}],
            owner="owner", idempotency_key=f"seed-{i}")
    store.apply(
        "tenant", [{"op": "no_op", "reason": "maintained"}], owner="owner",
        idempotency_key="ledger", maintenance_state={
            "mount": "agent-private", "signature": "old",
            "seed_card_count": 10})
    for i in range(1, 10):
        store.apply("tenant", [{"op": "delete", "record_id": f"m_{i}"}],
            owner="owner", idempotency_key=f"delete-{i}")

    # 模拟真实 v2：它已有 Maintenance ledger，但还没有 v3 seed_generations。
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE seed_generations")
        conn.execute("PRAGMA user_version=2")

    upgraded = SqliteStore(path)
    assert upgraded.load("tenant", owner="owner").seed_generations[
        "agent-private"] == 10
    upgraded.apply("tenant", [{"op": "add", "card": {
        "summary": "迁移后新增", "content": "正文"}}],
        owner="owner", idempotency_key="after-upgrade")
    garden = MountedGarden(model=None, store=upgraded,
                           min_new_cards_for_maintenance=1)
    check = garden.check_maintenance(SCOPE)
    assert check.needed is True
    assert check.trace["new_cards"] == 1


def test_sqlite_two_instances_replay_one_atomic_request(tmp_path):
    path = tmp_path / "race.db"
    stores = (SqliteStore(path), SqliteStore(path))
    barrier = threading.Barrier(2)
    outputs: list[object] = []

    def write(store: SqliteStore) -> None:
        barrier.wait()
        outputs.append(store.apply(
            "tenant", [{"op": "add", "card": {
                "summary": "只写一次", "content": "正文"}}],
            owner="owner", idempotency_key="same"))

    threads = [threading.Thread(target=write, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert len(outputs) == 2
    assert outputs[0].results == outputs[1].results
    assert len(stores[0].load("tenant", owner="owner").cards) == 1


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_promote_moves_mount_waterline_once_and_redundant_replay_is_noop(
    kind, tmp_path,
):
    store = (InMemoryStore() if kind == "memory"
             else SqliteStore(tmp_path / "promote.db"))
    store.apply("tenant", [{"op": "add", "card": {
        "id": "m_1", "summary": "卡", "content": "正文",
        "mount": "agent-private"}}], owner="owner", idempotency_key="seed")
    scope = replace(
        SCOPE, allowed_mounts=("agent-private", "family-shared"))
    garden = MountedGarden(model=None, store=store)
    first = garden.promote(scope, PromoteRequest(
        record_id="m_1", to_mount="family-shared", authorized=True))
    assert first.written
    snapshot = store.load("tenant", owner="owner")
    assert snapshot.cards[0]["mount"] == "family-shared"
    assert snapshot.seed_generations == {
        "agent-private": 1, "family-shared": 1}
    revision = snapshot.revision

    replay = garden.promote(scope, PromoteRequest(
        record_id="m_1", to_mount="family-shared", authorized=True))
    assert replay.written
    unchanged = store.load("tenant", owner="owner")
    assert unchanged.revision == revision
    assert unchanged.seed_generations == snapshot.seed_generations


def test_target_mount_is_an_authorization_boundary():
    store = InMemoryStore()
    store.apply("tenant", [{"op": "add", "card": {
        "id": "shared", "summary": "共享卡", "content": "正文",
        "mount": "family-shared"}}], owner="owner", idempotency_key="seed")
    garden = MountedGarden(model=None, store=store)
    hidden = garden._apply(SCOPE, "agent-private", [{
        "op": "update", "record_id": "shared", "changes": {"summary": "偷改"},
    }], idempotency_key="hidden", trace={})
    assert hidden.error == "record_not_found"

    both = replace(SCOPE, allowed_mounts=("agent-private", "family-shared"))
    mismatch = garden._apply(both, "agent-private", [{
        "op": "update", "record_id": "shared", "changes": {"summary": "错层"},
    }], idempotency_key="mismatch", trace={})
    assert mismatch.error == "target_mount_mismatch"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_delete_replay_returns_original_success_after_card_is_gone(kind, tmp_path):
    store = (InMemoryStore() if kind == "memory"
             else SqliteStore(tmp_path / "delete.db"))
    store.apply("tenant", [{"op": "add", "card": {
        "id": "m_1", "summary": "卡", "content": "正文"}}],
        owner="owner", idempotency_key="seed")
    garden = MountedGarden(model=None, store=store)
    first = garden.delete_record(SCOPE, "m_1", requested_by="user:u")
    replay = garden.delete_record(SCOPE, "m_1", requested_by="user:u")
    assert first.written and replay.written
    assert replay.record_ids == first.record_ids == ("m_1",)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_hard_delete_does_not_hide_the_next_seed_from_maintenance(kind, tmp_path):
    store = (InMemoryStore() if kind == "memory"
             else SqliteStore(tmp_path / "waterline.db"))
    for i in range(1, 4):
        store.apply("tenant", [{"op": "add", "card": {
            "id": f"m_{i}", "summary": f"卡 {i}", "content": "正文"}}],
            owner="owner", idempotency_key=f"seed-{i}")
    garden = MountedGarden(model=_ReplyModel('{"consolidations": []}'),
                           store=store, min_new_cards_for_maintenance=1)
    assert garden.run_and_store_maintenance(
        SCOPE, MaintenanceRequest(locale="zh-Hans")).written
    assert garden.check_maintenance(SCOPE).needed is False

    assert garden.delete_record(
        SCOPE, "m_1", requested_by="user:u").written
    store.apply("tenant", [{"op": "add", "card": {
        "summary": "删除后新增", "content": "正文"}}],
        owner="owner", idempotency_key="after-delete")
    check = garden.check_maintenance(SCOPE)
    assert check.needed is True
    assert check.trace["new_cards"] == 1


def test_modelless_and_weak_store_manifest_does_not_overclaim():
    store = InMemoryStore()
    service = Service(MountedGarden(model=None, store=store),
                      model_available=False)
    caps = service.handle({"id": "m", "method": "manifest.get"})["result"][
        "capabilities"]
    assert caps["capture"] is True  # 完整 begin/feed/cancel lane 仍可用
    assert caps["maintenance"] is True
    assert caps["history_import"] is False
    assert caps["migrate"] is False

    store.capabilities = lambda: replace(  # type: ignore[method-assign]
        FULL_CAPABILITIES, supports_hard_delete=False,
        supports_monotonic_seed_generation=False)
    weak_garden = MountedGarden(model=_ReplyModel(), store=store)
    caps = Service(weak_garden).handle(
        {"id": "m", "method": "manifest.get"})["result"]["capabilities"]
    assert caps["delete"] is False
    assert caps["maintenance"] is False
    assert caps["capture"] is True
    assert weak_garden.check_maintenance(SCOPE).error == (
        "storage_failed:maintenance_state")
    bypass = weak_garden.store_maintenance_result(
        SCOPE, MaintenanceRequest(locale="zh-Hans"),
        MaintenanceResult(needed=True, mutations=[{"op": "no_op"}],
                          trace={"signature": "s", "seed_card_count": 0}))
    assert bypass.error == "storage_failed:maintenance_state"

    store.capabilities = lambda: replace(  # type: ignore[method-assign]
        FULL_CAPABILITIES, supports_custom_fields=False)
    declared = Service(MountedGarden(model=None, store=store)).handle(
        {"id": "m", "method": "manifest.get"})["result"]
    assert declared["storage"]["capabilities"]["supports_custom_fields"] is False
    assert declared["storage"]["degradations"][0]["capability"] == (
        "supports_custom_fields")


def test_old_six_field_capabilities_shape_remains_source_compatible():
    caps = Capabilities(True, True, True, True, True, True)
    assert caps.supports_maintenance_state is False
    assert caps.supports_monotonic_seed_generation is False


def test_store_without_owner_scoping_is_refused_before_any_read():
    class _UnsafeStore(InMemoryStore):
        def capabilities(self):
            return replace(FULL_CAPABILITIES, supports_owner_scoping=False)

        def load(self, tenant, *, owner, **filters):
            raise AssertionError("owner scoping 缺失时不应触碰读路径")

    service = Service(MountedGarden(model=None, store=_UnsafeStore()),
                      model_available=False)
    manifest_caps = service.handle(
        {"id": "m", "method": "manifest.get"})["result"]["capabilities"]
    assert not any(manifest_caps.values())
    out = service.handle({"id": "r", "method": "context.get", "params": {
        "scope": WIRE_SCOPE, "query": "private",
    }})
    assert out["ok"] is False
    assert out["error"]["code"] == "storage_lacks_capabilities"


def test_abandoned_sessions_expire_and_capacity_is_shared_between_lanes():
    now = [100.0]
    store = InMemoryStore()
    store.apply("tenant", [{"op": "add", "card": {
        "id": f"m_{i}", "summary": f"卡 {i}", "content": "正文"}}
        for i in range(1, 13)], owner="owner", idempotency_key="seed")
    service = Service(
        MountedGarden(model=None, store=store), model_available=False,
        session_ttl_seconds=10, max_active_sessions=1, clock=lambda: now[0])
    begun = service.handle({"id": "1", "method": "capture.begin", "params": {
        "scope": WIRE_SCOPE, "window": "用户：我喜欢安静", "locale": "zh-Hans",
    }})
    assert begun["ok"]
    sid = begun["result"]["session_id"]

    full = service.handle({"id": "2", "method": "maintenance.begin", "params": {
        "scope": WIRE_SCOPE, "locale": "zh-Hans",
    }})
    assert full["ok"] is False
    assert full["error"]["code"] == "session_capacity"

    now[0] += 11
    expired = service.handle({"id": "3", "method": "capture.feed", "params": {
        "session_id": sid, "reply": CARD_REPLY,
    }})
    assert expired["error"]["code"] == "unknown_session"
    reopened = service.handle({
        "id": "4", "method": "capture.begin", "params": {
            "scope": WIRE_SCOPE, "window": "用户：我喜欢安静", "locale": "zh-Hans",
        }})
    assert reopened["ok"] is True


def test_serve_survives_valid_json_that_is_not_an_object():
    service = Service(MountedGarden(model=None, store=InMemoryStore()),
                      model_available=False)
    output = io.StringIO()
    service.serve(io.StringIO('[]\n{"id":"ok","method":"health.get"}\n'),
                  output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == "invalid_request"
    assert replies[1]["ok"] is True


@pytest.mark.parametrize("bad_id", [1.5, True, {}, []])
def test_wire_rejects_ids_outside_the_declared_schema(bad_id):
    service = Service(MountedGarden(model=None, store=InMemoryStore()),
                      model_available=False)
    out = service.handle({"id": bad_id, "method": "health.get"})
    assert out["id"] is None
    assert out["error"]["code"] == "invalid_request"
    root = {**method_schemas()["health.get"]["response"],
            "schemas": schemas()}
    Draft202012Validator(root).validate(out)


@pytest.mark.parametrize("method,params", [
    ("capture.run", {"scope": WIRE_SCOPE, "window": "x", "locale": "en"}),
    ("maintenance.run", {"scope": WIRE_SCOPE, "locale": "en"}),
    ("records.migrate", {"scope": WIRE_SCOPE, "old_cards": "x",
                         "allowed_ids": ["m_1"], "locale": "en"}),
    ("history.import", {"scope": WIRE_SCOPE, "material": "x", "locale": "en"}),
])
def test_direct_model_methods_report_model_not_configured(method, params):
    service = Service(MountedGarden(model=None, store=InMemoryStore()))
    out = service.handle({"id": "1", "method": method, "params": params})
    assert out["ok"] is False
    assert out["error"]["code"] == "model_not_configured"


def test_history_import_resume_binds_names_that_enter_the_prompt():
    garden = MountedGarden(model=None, store=InMemoryStore())
    garden.capture_and_store = lambda scope, request: OperationReceipt(  # type: ignore[method-assign]
        reason="nothing_worth_keeping")
    request = ImportRequest(
        material="第一段\n第二段", locale="zh-Hans", ai_name="小花",
        user_name="小明")
    complete = garden.import_history(SCOPE, request)
    stale = ImportProgress(
        cursor=1, total=complete.total, source_digest=complete.source_digest,
        import_fingerprint=complete.import_fingerprint)
    with pytest.raises(ValueError, match="当前请求不同"):
        garden.import_history(
            SCOPE, replace(request, ai_name="另一个名字"), progress=stale)


def test_history_import_resume_binds_the_store_idempotency_prefix():
    garden = MountedGarden(model=None, store=InMemoryStore())
    garden.capture_and_store = lambda scope, request: OperationReceipt(  # type: ignore[method-assign]
        reason="nothing_worth_keeping")
    request = ImportRequest(material="一段历史", locale="zh-Hans",
                            idempotency_key="import-1")
    complete = garden.import_history(SCOPE, request)
    stale = ImportProgress(
        cursor=1, total=complete.total, source_digest=complete.source_digest,
        import_fingerprint=complete.import_fingerprint)
    with pytest.raises(ValueError, match="当前请求不同"):
        garden.import_history(
            SCOPE, replace(request, idempotency_key="import-2"), progress=stale)


def test_migrate_wire_path_updates_existing_card():
    reply = json.dumps({"upgrades": [{
        "id": "m_1", "summary": "新的摘要", "content": "新的正文内容。",
        "bucket": "工作", "threads": ["项目"],
    }]}, ensure_ascii=False)
    store = InMemoryStore()
    store.apply("tenant", [{"op": "add", "card": {
        "id": "m_1", "summary": "旧", "content": "旧正文"}}],
        owner="owner", idempotency_key="seed")
    service = Service(MountedGarden(model=_ReplyModel(reply), store=store))
    out = service.handle({"id": "1", "method": "records.migrate", "params": {
        "scope": WIRE_SCOPE, "old_cards": "[m_1] 旧", "allowed_ids": ["m_1"],
        "locale": "zh-Hans", "idempotency_key": "migrate-1",
    }})
    assert out["ok"] and out["result"]["written"] is True
    card = store.load("tenant", owner="owner").cards[0]
    assert card["summary"] == "新的摘要"
    assert card["bucket"] == "工作"


def test_all_creation_paths_persist_source_and_trusted_actor():
    store = InMemoryStore()
    garden = MountedGarden(model=_ReplyModel(), store=store,
                           min_new_cards_for_maintenance=1)
    assert garden.capture_and_store(SCOPE, CaptureRequest(
        window="用户：我喜欢安静", locale="zh-Hans",
        idempotency_key="capture")).written
    assert garden.write_one(SCOPE, CuratedWriteRequest(
        text="记住我周五不喝咖啡", locale="zh-Hans",
        idempotency_key="curated")).written

    history = garden.import_history(SCOPE, ImportRequest(
        material="过去一直喜欢安静。", material_kind="diary",
        locale="zh-Hans", idempotency_key="history"))
    assert history.done
    sources = {card["source"]: card for card in store.load(
        "tenant", owner="owner").cards}
    assert {"conversation_capture", "curated", "history_import"} <= set(sources)
    assert sources["history_import"]["source_material_kind"] == "diary"
    for card in sources.values():
        assert card["source_actor"] == SCOPE.actor.as_dict()

    garden.component._model = _ReplyModel(json.dumps({"consolidations": [{
        "op": "merge", "card_ids": ["m_1", "m_2"], "rationale": "相关",
        "result": {"summary": "安静的习惯", "content": "偏好安静，也有固定习惯。"},
    }]}, ensure_ascii=False))
    dream = garden.run_and_store_maintenance(
        SCOPE, MaintenanceRequest(locale="zh-Hans"))
    assert dream.written
    all_cards = store.load("tenant", owner="owner", include_archived=True,
                           include_superseded=True).cards
    dreamed = next(card for card in all_cards
                   if card.get("source") == "memory_dream")
    assert dreamed["source_actor"] == SCOPE.actor.as_dict()


def test_standard_json_schema_accepts_real_success_and_error_envelopes():
    service = Service(MountedGarden(model=None, store=InMemoryStore()),
                      model_available=False)
    fixtures = {
        "health.get": service.handle({"id": 1, "method": "health.get"}),
        "context.get": service.handle({"id": "2", "method": "context.get",
                                        "params": {"scope": WIRE_SCOPE,
                                                   "query": "anything"}}),
        "capture.begin": service.handle({"id": "3", "method": "capture.begin",
                                          "params": {"scope": WIRE_SCOPE,
                                                     "window": "hello",
                                                     "locale": "en"}}),
        "capture.feed": service.handle({"id": None, "method": "capture.feed",
                                         "params": {"session_id": "missing",
                                                    "reply": "{}"}}),
    }
    # 每个公开 method 至少用真实分发器产出一份 success 或 structured error；
    # 这样 ErrorEnvelope 不会只在四个碰巧挑中的入口上看起来正确。
    for method in service._methods:
        fixtures.setdefault(method, service.handle({
            "id": f"empty:{method}", "method": method, "params": {}}))
    definitions = schemas()
    for method, response in fixtures.items():
        root = {**method_schemas()[method]["response"],
                "schemas": definitions}
        Draft202012Validator.check_schema(root)
        Draft202012Validator(root).validate(response)


def test_standard_json_schema_accepts_each_public_success_shape():
    class _PurposeModel:
        def complete(self, prompt: str, *, purpose: str = "") -> str:
            if purpose == "migrate":
                return json.dumps({"upgrades": [{
                    "id": "m_1", "summary": "升级后", "content": "升级后的正文。",
                    "bucket": "工作", "threads": ["项目"],
                }]}, ensure_ascii=False)
            if purpose == "dream":
                return '{"consolidations": []}'
            return CARD_REPLY

    store = InMemoryStore()
    service = Service(MountedGarden(model=_PurposeModel(), store=store))
    scope = {**WIRE_SCOPE,
             "allowed_mounts": ["agent-private", "family-shared"]}
    responses: dict[str, dict] = {}

    def call(method: str, params: dict | None = None) -> dict:
        response = service.handle({"id": method, "method": method,
                                   "params": params or {}})
        assert response["ok"], response
        responses[method] = response
        return response

    call("manifest.get")
    call("schema.get")
    call("health.get")
    call("records.write", {"scope": scope, "text": "记住第一条事实",
                            "idempotency_key": "curated"})
    call("capture.run", {"scope": scope, "window": "我喜欢安静",
                         "locale": "zh-Hans", "idempotency_key": "capture"})
    begun = call("capture.begin", {"scope": scope, "window": "我喜欢安静",
                                    "locale": "zh-Hans",
                                    "idempotency_key": "host-capture"})
    call("capture.feed", {"session_id": begun["result"]["session_id"],
                           "reply": CARD_REPLY})
    cancel_begun = call("capture.begin", {
        "scope": scope, "window": "我还喜欢散步", "locale": "zh-Hans",
        "idempotency_key": "cancelled"})
    call("capture.cancel", {
        "session_id": cancel_begun["result"]["session_id"]})
    call("context.get", {"scope": scope, "query": "安静"})
    call("maintenance.check", {"scope": scope})
    call("maintenance.run", {"scope": scope, "locale": "zh-Hans"})
    call("maintenance.begin", {"scope": scope, "locale": "zh-Hans"})
    call("maintenance.cancel", {"session_id": "already-finished"})
    call("records.browse", {"scope": scope})
    call("records.export", {"scope": scope})
    call("records.migrate", {
        "scope": scope, "old_cards": "[m_1] 旧", "allowed_ids": ["m_1"],
        "locale": "zh-Hans", "idempotency_key": "migrate"})
    call("history.import", {
        "scope": scope, "material": "过去喜欢安静。", "locale": "zh-Hans",
        "idempotency_key": "history"})
    call("records.promote", {"scope": scope, "record_id": "m_1",
                              "to_mount": "family-shared", "authorized": True})
    call("tool.list")
    call("tool.invoke", {"scope": scope, "name": "memory_search",
                          "arguments": {"query": "安静"}})
    call("records.delete", {"scope": scope, "record_id": "m_1",
                             "requested_by": "user:u"})

    definitions = schemas()
    # maintenance.feed 的成功形状由专项 host-driven CAS 测试覆盖；这里至少
    # 验证其和 capture.feed 共用的 discriminated SessionState 契约。
    for method, response in responses.items():
        root = {**method_schemas()[method]["response"],
                "schemas": definitions}
        Draft202012Validator(root).validate(response)
