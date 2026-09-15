"""JSON Lines 上新接通的三件事：关联读取、宿主驱动的分批导入、整理的渲染预算。

守的是**wire 这一层**的合同 —— 内核语义另有 test_related / test_import_session 锁着：

  · records.related：owner / 挂载点 / 生命周期按请求里的可信 scope 过滤，
    ids 写别人的卡只拿到空结果；参数和回复都符合公开 schema
  · history.import_begin/feed/commit/fail/cancel：
      service 模式多批写进 Store，每个回复都带可存的 progress；
      服务重启后拿存下的 progress 重开，已提交的批次不再调模型、不重复写卡；
      和一次性 history.import 写出同样的卡、进度可以互相续传；
      host 模式把 mutations 交给宿主、commit 真实 id / fail 记失败；
      别的 owner 续不上、会话过期/结束给 unknown_session、状态不对给 invalid_request；
      并发写冲突时重读重算
  · maintenance.run/begin 透传 cards_limit 等四个预算
  · 老方法（history.import、maintenance.begin 不带预算）行为不变

全部纯合成数据，不 import 任何宿主模块。
"""
from __future__ import annotations

import copy
import json
import pathlib
import tempfile

import pytest

from memgarden import MountedGarden, Scope, SqliteStore
from memgarden.schema import method_schemas, schemas
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore
from memgarden.validate import validate

ALICE = {"tenant_id": "t", "memory_owner_id": "alice"}
BOB = {"tenant_id": "t", "memory_owner_id": "bob"}


# --------------------------------------------------------------------- 夹具

def _check_response(method: str, response: dict) -> None:
    """回复必须符合 schema.get 发出去的那份 response schema（内置校验器 + jsonschema）。"""
    spec = method_schemas()[method]["response"]
    wire = json.loads(json.dumps(response, ensure_ascii=False))
    validate(wire, spec, schemas=schemas())
    jsonschema = pytest.importorskip("jsonschema")

    def rewrite(node):
        if isinstance(node, dict):
            return {k: ("#/$defs/" + v.rsplit("/", 1)[-1]
                        if k == "$ref" and isinstance(v, str) else rewrite(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [rewrite(x) for x in node]
        return node

    defs = rewrite(copy.deepcopy(schemas()))
    jsonschema.validate(wire, {**rewrite(copy.deepcopy(spec)), "$defs": defs})


def _call(service: Service, method: str, **params) -> dict:
    response = service.handle({"id": method, "method": method, "params": params})
    if response["ok"]:
        _check_response(method, response)
    return response


def _ok(service: Service, method: str, **params):
    response = _call(service, method, **params)
    assert response["ok"], response
    return response["result"]


def _material(*stretches: str) -> str:
    """每段补足到 ~180 字并以换行结尾：batch_chars=200 时一段一批。"""
    out = []
    for text in stretches:
        body = text
        while len(body) < 180:
            body += " " + text
        out.append(body[:180].rstrip() + "\n")
    return "".join(out)


def _card(summary: str, *, bucket: str = "偏好与边界") -> dict:
    return {"action": "add", "type": "fact", "target_id": None, "bucket": bucket,
            "threads": ["饮食"], "summary": summary,
            "content": f"{summary}。这是一段足够长、有实质内容的正文。",
            "importance": 0.5, "pulse": 0.2}


def _reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


def _window(prompt: str) -> str:
    for label in ("[The material they handed you]", "[The material]"):
        if label in prompt:
            return prompt.split(label, 1)[1].split("[Output]", 1)[0]
    return prompt


RULES = [("不吃辣", _reply(_card("不吃辣"))),
         ("爬山", _reply(_card("周末爬山", bucket="爱好"))),
         ("蛋子", _reply(_card("养了猫蛋子", bucket="宠物")))]


def _answer(prompt: str) -> str:
    window = _window(prompt)
    for needle, reply in RULES:
        if needle in window:
            return reply
    return '{"cards": []}'


class _Model:
    """服务侧模型（只给一次性 history.import 用）。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.prompts.append(prompt)
        return _answer(prompt)


class _NoModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:  # pragma: no cover
        raise AssertionError("宿主驱动的会话不许由服务调模型")


def _service(store=None, model=None) -> Service:
    garden = MountedGarden(model=model or _NoModel(), store=store or InMemoryStore())
    return Service(garden, model_available=model is not None)


def _summaries(store, owner="alice"):
    return sorted(c["summary"] for c in store.load("t", owner=owner).cards)


MATERIAL = _material("我不吃辣，一吃就胃疼", "周末常去爬山", "养了一只叫蛋子的猫")


def _begin(service, **extra):
    params = {"scope": ALICE, "material": MATERIAL, "locale": "zh-Hans",
              "batch_chars": 200, "idempotency_key": "imp", **extra}
    return _ok(service, "history.import_begin", **params)


def _drive(service, state, *, host_calls: list[str] | None = None, stop_after=None):
    """宿主循环：needs_model 就调自己的模型喂回去。返回最后的状态和每步存下的 progress。"""
    saved = [state["progress"]]
    fed = 0
    while state["status"] == "needs_model":
        if stop_after is not None and fed >= stop_after:
            break
        if host_calls is not None:
            host_calls.append(state["next_prompt"])
        state = _ok(service, "history.import_feed", session_id=state["session_id"],
                    reply=_answer(state["next_prompt"]), truncated=False)
        saved.append(state["progress"])
        fed += 1
    return state, saved


# ------------------------------------------------------------ records.related

def _seed_related(store):
    store.apply("t", [
        {"op": "add", "card": {"id": "trip", "summary": "搬家计划", "content": "正文",
                               "threads": ["搬家"], "anchor_memory_ids": ["house"]}},
        {"op": "add", "card": {"id": "house", "summary": "看中了一套两居", "content": "正文",
                               "threads": ["看房"]}},
        {"op": "add", "card": {"id": "boxes", "summary": "打包纸箱买好了", "content": "正文",
                               "threads": ["搬家"]}},
        {"op": "add", "card": {"id": "gone", "summary": "被删掉的搬家卡", "content": "正文",
                               "threads": ["搬家"]}},
        {"op": "add", "card": {"id": "shared", "summary": "家庭群的搬家卡", "content": "正文",
                               "threads": ["搬家"], "mount": "family-shared"}},
    ], owner="alice", idempotency_key="seed")
    store.apply("t", [{"op": "delete", "record_id": "gone", "requested_by": "user"}],
                owner="alice", idempotency_key="del")
    store.apply("t", [
        {"op": "add", "card": {"id": "bob_trip", "summary": "bob 的搬家", "content": "正文",
                               "threads": ["搬家"]}},
    ], owner="bob", idempotency_key="bob")


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    if request.param == "memory":
        return InMemoryStore()
    return SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "wire.db"))


def test_wire_related_matches_the_sdk_and_the_schema(store):
    _seed_related(store)
    service = _service(store)
    got = _ok(service, "records.related", scope=ALICE, ids=["trip"])
    assert got["items"] == MountedGarden(model=None, store=store).related(
        Scope(tenant_id="t", memory_owner_id="alice"), ["trip"])
    assert [(i["id"], i["relation"]) for i in got["items"]] == [
        ("house", "anchor"), ("boxes", "thread")]
    # 硬删的、别的挂载点的、别人的卡都不在里面
    assert {"gone", "shared", "bob_trip"}.isdisjoint(i["id"] for i in got["items"])
    assert _ok(service, "records.related", scope=ALICE, ids=["trip"], cap=1)["items"] == [
        got["items"][0]]


def test_wire_related_scope_comes_from_the_request_scope_not_the_ids(store):
    _seed_related(store)
    service = _service(store)
    # bob 拿 alice 的卡 id 来问：什么都得不到，也不证明那张卡存在。
    assert _ok(service, "records.related", scope=BOB, ids=["trip", "house"])["items"] == []
    # alice 拿 bob 的卡 id 来问：同样为空。
    assert _ok(service, "records.related", scope=ALICE, ids=["bob_trip"])["items"] == []
    # 已删的卡当源卡：为空。
    assert _ok(service, "records.related", scope=ALICE, ids=["gone"])["items"] == []
    # 放开 family-shared 之后才看得到共享挂载点里的邻居。
    wide = {**ALICE, "allowed_mounts": ["agent-private", "family-shared"]}
    assert "shared" in {i["id"] for i in _ok(
        service, "records.related", scope=wide, ids=["trip"])["items"]}


@pytest.mark.parametrize("params, field", [
    ({"scope": ALICE}, "ids"),
    ({"scope": ALICE, "ids": "trip"}, "ids"),
    ({"scope": ALICE, "ids": ["trip"], "cap": -1}, "cap"),
    ({"scope": ALICE, "ids": [1]}, "ids[0]"),
])
def test_wire_related_rejects_bad_params_before_reading(params, field):
    out = _service().handle({"id": "r", "method": "records.related", "params": params})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"
    assert out["error"]["field"] == field


def test_wire_related_requires_a_trusted_owner():
    out = _service().handle({"id": "r", "method": "records.related",
                             "params": {"scope": {"tenant_id": "t"}, "ids": ["x"]}})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"
    out = _service().handle({"id": "r", "method": "records.related",
                             "params": {"scope": {"tenant_id": "t", "memory_owner_id": " "},
                                        "ids": ["x"]}})
    assert out["ok"] is False and out["error"]["code"] == "memory_owner_required"


# ------------------------------------------------- history.import_* service 模式

def test_service_mode_writes_every_batch_and_every_reply_carries_progress():
    store = InMemoryStore()
    service = _service(store)
    state = _begin(service)
    assert state["status"] == "needs_model" and state["batch"]["stage"] == "cards"
    assert state["estimate"]["batches_total"] == 3
    final, saved = _drive(service, state)
    assert final["status"] == "completed" and "session_id" not in final
    assert final["progress"]["done"] is True and final["progress"]["percent"] == 100
    assert final["committed"]["written"] is True
    assert [p["batches_done"] for p in saved] == [0, 1, 2, 3]
    assert _summaries(store) == ["不吃辣", "养了猫蛋子", "周末爬山"]
    # 会话结束后再喂：unknown_session，不是 internal_error。
    late = service.handle({"id": "x", "method": "history.import_feed",
                           "params": {"session_id": state["session_id"], "reply": "{}"}})
    assert late["ok"] is False and late["error"]["code"] == "unknown_session"


def test_resume_after_service_restart_does_not_recall_or_rewrite():
    store = InMemoryStore()
    first = _service(store)
    prompts: list[str] = []
    state, saved = _drive(first, _begin(first), host_calls=prompts, stop_after=2)
    assert state["status"] == "needs_model" and len(prompts) == 2
    progress = json.loads(json.dumps(saved[-1]))   # 宿主存盘再读回
    assert progress["batches_done"] == 2 and progress["done"] is False

    # 服务重启：在途会话全丢了。
    restarted = _service(store)
    gone = restarted.handle({"id": "x", "method": "history.import_feed",
                             "params": {"session_id": state["session_id"], "reply": "{}"}})
    assert gone["error"]["code"] == "unknown_session"

    resumed = _begin(restarted, progress=progress)
    assert resumed["batch"]["offset"] == progress["cursor"]
    final, _ = _drive(restarted, resumed, host_calls=prompts)
    assert final["status"] == "completed"
    assert len(prompts) == 3, "已提交的批次不许再调模型"
    assert _summaries(store) == ["不吃辣", "养了猫蛋子", "周末爬山"]

    # 完成的进度再开一次：直接 completed，不调模型。
    again = _begin(restarted, progress=final["progress"])
    assert again["status"] == "completed" and len(prompts) == 3


def test_session_and_one_shot_import_write_the_same_cards_and_share_progress():
    wire_store, one_shot_store = InMemoryStore(), InMemoryStore()
    wire = _service(wire_store)
    final, _ = _drive(wire, _begin(wire))
    one_shot = _service(one_shot_store, model=_Model())
    out = _ok(one_shot, "history.import", scope=ALICE, material=MATERIAL,
              locale="zh-Hans", batch_chars=200, idempotency_key="imp")
    assert out["done"] and final["progress"]["done"]

    def shape(store):
        keep = ("summary", "content", "bucket", "threads", "source")
        return sorted(({k: c.get(k) for k in keep} for c in store.load("t", owner="alice").cards),
                      key=lambda c: c["summary"])

    assert shape(wire_store) == shape(one_shot_store)
    assert out["import_fingerprint"] == final["progress"]["import_fingerprint"]

    # 一次性导入跑了一批就停：拿它的进度走会话续上。
    partial_store = InMemoryStore()
    model = _Model()
    part = _ok(_service(partial_store, model=model), "history.import", scope=ALICE,
               material=MATERIAL, locale="zh-Hans", batch_chars=200,
               idempotency_key="imp", max_batches=1)
    assert part["batches_done"] == 1
    service = _service(partial_store)
    rest, _ = _drive(service, _begin(service, progress=part))
    assert rest["status"] == "completed"
    assert shape(partial_store) == shape(one_shot_store)


def test_another_owner_cannot_resume_the_progress():
    service = _service()
    _, saved = _drive(service, _begin(service), stop_after=1)
    out = service.handle({"id": "b", "method": "history.import_begin", "params": {
        "scope": BOB, "material": MATERIAL, "locale": "zh-Hans", "batch_chars": 200,
        "idempotency_key": "imp", "progress": saved[-1]}})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"
    assert "不同" in out["error"]["message"]
    # 消息里不带材料或进度里的内容
    assert "吃辣" not in out["error"]["message"]


def test_conflicting_write_is_recomputed_on_a_fresh_read():
    store = InMemoryStore()
    service = _service(store)
    state = _begin(service)
    # 模型还在想的时候，别的写入进来了。
    store.apply("t", [{"op": "add", "card": {"id": "other", "summary": "别处写的卡",
                                               "content": "正文"}}],
                owner="alice", idempotency_key="concurrent")
    retry = _ok(service, "history.import_feed", session_id=state["session_id"],
                reply=_answer(state["next_prompt"]))
    assert retry["status"] == "needs_model" and retry["retrying_after"] == "conflict"
    assert retry["batch"]["offset"] == 0 and "别处写的卡" in retry["next_prompt"]
    assert retry["progress"]["batches_done"] == 0 and not retry["progress"]["failed"]
    final, _ = _drive(service, retry)
    assert final["status"] == "completed"
    assert _summaries(store) == ["不吃辣", "养了猫蛋子", "别处写的卡", "周末爬山"]


def test_parse_failure_ends_the_session_and_keeps_the_cursor():
    store = InMemoryStore()
    service = _service(store)
    state = _begin(service)
    # 可重问的格式错：先重问一次（同一批），再错就彻底失败。
    broken = '{"cards": [{"action": "add", "summary": "不吃辣" "content": 1}]}'
    state = _ok(service, "history.import_feed", session_id=state["session_id"],
                reply=broken)
    assert state["status"] == "needs_model" and state["batch"]["offset"] == 0
    failed = _ok(service, "history.import_feed", session_id=state["session_id"],
                 reply=broken)
    assert failed["status"] == "failed" and failed["error"]
    assert failed["progress"]["cursor"] == 0 and failed["progress"]["failed"]
    assert store.load("t", owner="alice").cards == []
    # 续传会重试这一批；成功后失败记录清掉。
    final, _ = _drive(service, _begin(service, progress=failed["progress"]))
    assert final["status"] == "completed" and final["progress"]["failed"] == []


def test_host_fail_while_waiting_for_the_model_records_the_batch():
    service = _service()
    state, _ = _drive(service, _begin(service), stop_after=1)
    failed = _ok(service, "history.import_fail", session_id=state["session_id"],
                 error="provider_timeout")
    assert failed["status"] == "failed" and failed["error"] == "provider_timeout"
    assert failed["progress"]["failed"] == [
        {"offset": state["batch"]["offset"], "error": "provider_timeout"}]
    assert failed["progress"]["batches_done"] == 1
    out = service.handle({"id": "c", "method": "history.import_cancel",
                          "params": {"session_id": state["session_id"]}})
    assert out["result"] == {"cancelled": False}


def test_cancel_drops_the_session_without_touching_progress():
    store = InMemoryStore()
    service = _service(store)
    state = _begin(service)
    assert _ok(service, "history.import_cancel",
               session_id=state["session_id"]) == {"cancelled": True}
    assert store.load("t", owner="alice").cards == []
    gone = service.handle({"id": "f", "method": "history.import_feed",
                           "params": {"session_id": state["session_id"], "reply": "{}"}})
    assert gone["error"]["code"] == "unknown_session"


def test_service_mode_refuses_commit_and_host_cards():
    service = _service()
    state = _begin(service)
    out = service.handle({"id": "c", "method": "history.import_commit", "params": {
        "session_id": state["session_id"], "record_ids": ["x"]}})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"
    out = service.handle({"id": "b", "method": "history.import_begin", "params": {
        "scope": ALICE, "material": MATERIAL, "locale": "zh-Hans",
        "existing_cards": [{"id": "h1", "summary": "x"}]}})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"


def test_two_pass_progress_carries_candidates_and_writes_at_the_end():
    store = InMemoryStore()
    service = _service(store)

    def answer(prompt):
        if "[The material]" in prompt:
            window = _window(prompt)
            summary = "不吃辣" if "不吃辣" in window else "周末爬山" if "爬山" in window else "养猫"
            return json.dumps({"candidates": [{"summary": summary, "evidence": summary}]},
                              ensure_ascii=False)
        return _reply(_card("不吃辣"), _card("周末爬山", bucket="爱好"), _card("养猫", bucket="宠物"))

    state = _begin(service, strategy="two_pass")
    stages = []
    while state["status"] == "needs_model":
        stages.append(state["batch"]["stage"])
        state = _ok(service, "history.import_feed", session_id=state["session_id"],
                    reply=answer(state["next_prompt"]))
        if state["status"] == "needs_model" and state["batch"]["stage"] == "write":
            # 读完材料：进度里的候选含用户内容，宿主要按记忆正文等级存它。
            assert len(state["progress"]["candidates"]) == 3
            assert state["progress"]["percent"] == 70
    assert stages == ["candidates", "candidates", "candidates", "write"]
    assert state["status"] == "completed" and state["progress"]["strategy"] == "two_pass"
    assert _summaries(store) == ["不吃辣", "养猫", "周末爬山"]


def test_import_sessions_share_the_session_capacity():
    service = Service(MountedGarden(model=None, store=InMemoryStore()),
                      model_available=False, max_active_sessions=1)
    _begin(service)
    out = service.handle({"id": "c", "method": "capture.begin", "params": {
        "scope": ALICE, "window": "User: 我不吃辣", "locale": "zh-Hans"}})
    assert out["ok"] is False and out["error"]["code"] == "session_capacity"


# ---------------------------------------------------- history.import_* host 模式

class _HostDb:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.seq = 0

    def write(self, mutations):
        ids = []
        for m in mutations:
            self.seq += 1
            rid = f"h_{self.seq}"
            self.cards[rid] = {**m["card"], "id": rid}
            ids.append(rid)
        return ids


def test_host_mode_hands_mutations_to_the_host_and_registers_real_ids():
    service_store = InMemoryStore()
    service = _service(service_store)
    db = _HostDb()
    state = _begin(service, write_mode="host",
                   existing_cards=[{"id": "old_1", "summary": "早就记过的卡", "content": "x"}])
    assert "old_1: [" in state["next_prompt"] or "早就记过的卡" in state["next_prompt"]
    commits = 0
    while state["status"] != "completed":
        if state["status"] == "needs_model":
            state = _ok(service, "history.import_feed", session_id=state["session_id"],
                        reply=_answer(state["next_prompt"]))
            continue
        assert state["status"] == "needs_commit"
        batch = state["batch"]
        assert batch["mutations"] and batch["idempotency_key"]
        ids = db.write(batch["mutations"])
        # id 数量对不上：拒绝，会话还在 needs_commit，可以带正确的 id 重试。
        bad = service.handle({"id": "c", "method": "history.import_commit", "params": {
            "session_id": state["session_id"], "record_ids": ids + ["extra"]}})
        assert bad["ok"] is False and bad["error"]["code"] == "invalid_request"
        state = _ok(service, "history.import_commit", session_id=state["session_id"],
                    record_ids=ids)
        commits += 1
        if state["status"] == "needs_model" and commits == 1:
            # 刚写进宿主库的卡（带宿主真实 id）要出现在下一批的索引里。
            assert "h_1" in state["next_prompt"]
    assert commits == 3 and state["progress"]["done"]
    assert sorted(c["summary"] for c in db.cards.values()) == ["不吃辣", "养了猫蛋子", "周末爬山"]
    assert service_store.load("t", owner="alice").cards == [], "host 模式服务不写自己的 Store"


def test_host_mode_write_failure_keeps_the_cursor():
    service = _service()
    state = _begin(service, write_mode="host")
    state = _ok(service, "history.import_feed", session_id=state["session_id"],
                reply=_answer(state["next_prompt"]))
    assert state["status"] == "needs_commit"
    fed = _call(service, "history.import_feed", session_id=state["session_id"], reply="{}")
    assert fed["ok"] is False and fed["error"]["code"] == "invalid_request"
    failed = _ok(service, "history.import_fail", session_id=state["session_id"],
                 error="host_db_down")
    assert failed["status"] == "failed"
    assert failed["progress"]["cursor"] == 0
    assert failed["progress"]["failed"] == [{"offset": 0, "error": "host_db_down"}]


# ---------------------------------------------------------- maintenance 预算

class _DreamModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.prompts.append(prompt)
        return json.dumps({"operations": []})


def _dream_store(n: int = 12) -> InMemoryStore:
    store = InMemoryStore()
    store.apply("t", [{"op": "add", "card": {
        "id": f"c{i:02d}", "summary": f"第{i}件事", "content": f"BODY{i:02d} " + "正文" * 40,
        "bucket": "生活"}} for i in range(n)], owner="alice", idempotency_key="seed")
    return store


def test_maintenance_begin_passes_the_render_budgets_through():
    service = _service(_dream_store())
    default = _ok(service, "maintenance.begin", scope=ALICE, locale="zh-Hans")
    assert default["status"] == "needs_model"
    assert default["next_prompt"].count("BODY") == 12
    _ok(service, "maintenance.cancel", session_id=default["session_id"])

    capped = _ok(service, "maintenance.begin", scope=ALICE, locale="zh-Hans",
                 cards_limit=3, card_body_chars=10, card_summary_chars=50,
                 cards_budget_chars=100_000)
    assert capped["status"] == "needs_model"
    assert "TRUNCATED" in capped["next_prompt"]
    assert capped["next_prompt"].count("BODY") == 3
    done = _ok(service, "maintenance.feed", session_id=capped["session_id"],
               reply=json.dumps({"operations": []}))
    assert done["status"] == "completed"
    trace = done["result"]["trace"]
    assert trace["cards_rendered"] == 3 and trace["cards_truncated"] == 3


def test_maintenance_run_passes_the_render_budgets_through():
    model = _DreamModel()
    service = _service(_dream_store(), model=model)
    receipt = _ok(service, "maintenance.run", scope=ALICE, locale="zh-Hans", cards_limit=2)
    assert receipt["trace"]["cards_rendered"] == 2
    assert len(model.prompts) == 1


@pytest.mark.parametrize("field", ["cards_limit", "cards_budget_chars",
                                   "card_body_chars", "card_summary_chars"])
def test_maintenance_budgets_must_be_positive(field):
    out = _service(_dream_store()).handle({"id": "m", "method": "maintenance.begin", "params": {
        "scope": ALICE, "locale": "zh-Hans", field: 0}})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"
    assert out["error"]["field"] == field
