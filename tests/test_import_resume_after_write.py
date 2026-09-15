"""历史导入：崩在「写进去了、进度没存下来」之间，续传不许卡死；编造的 target 不许漏出去（发布前复审）。

## 卡死是怎么发生的

批次的幂等键只由材料和导入语义决定，不含模型回复。续传时同一批重跑，模型看到的已有记忆
索引里多了上次刚写的卡，回复几乎一定不同：

    宿主驱动     拿新回复 commit，id 数和上次写入记录对不上 → ValueError，永远过不去；
                 数对上了，新内容被登记到旧 id 上
    Store 路径   同键不同内容 → idempotency_conflict → session.fail，游标永远不动

## 编造的 target

导入的写卡请求没交现有卡，``target_id`` 不做校验：模型编一个 id 去 supersede，
整批原子提交的 Store 会连同好卡一起拒掉。
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pytest

from memgarden import GardenComponent, ImportProgress, ImportRequest, MountedGarden, Scope
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore

ALICE = Scope(tenant_id="t", memory_owner_id="alice")
WIRE_ALICE = {"tenant_id": "t", "memory_owner_id": "alice"}


def _material(*stretches: str) -> str:
    out = []
    for text in stretches:
        body = text
        while len(body) < 180:
            body += " " + text
        out.append(body[:180].rstrip() + "\n")
    return "".join(out)


MATERIAL = _material("我不吃辣，一吃就胃疼", "周末常去爬山")


def _card(summary: str, *, action: str = "add", target: str | None = None) -> dict:
    return {"action": action, "type": "fact", "target_id": target, "bucket": "偏好与边界",
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


class Model:
    """第一轮和续传后回复不同：续传时多写一张（模型看到了索引里的新卡，换了写法）。"""

    def __init__(self) -> None:
        self.round = 1
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        window = _window(prompt)
        if "不吃辣" in window:
            if self.round == 1:
                return _reply(_card("不吃辣"))
            return _reply(_card("不吃辣，一吃就胃疼"), _card("吃辣会胃疼"))
        if "爬山" in window:
            return _reply(_card("周末爬山"))
        return '{"cards": []}'

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self(prompt)


class HostDb:
    """宿主的库：同一个幂等键第二次写入不同内容时报冲突（和参考 Store 同语义），并留写入记录。"""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.log: dict[str, tuple[str, list[str]]] = {}
        self.seq = 0

    def write(self, mutations: list[dict], key: str) -> list[str]:
        digest = json.dumps(mutations, ensure_ascii=False, sort_keys=True)
        if key in self.log:
            if self.log[key][0] != digest:
                raise RuntimeError("idempotency_conflict")
            return self.log[key][1]
        ids = []
        for m in mutations:
            self.seq += 1
            rid = f"h_{self.seq}"
            self.cards[rid] = {**m["card"], "id": rid}
            ids.append(rid)
        self.log[key] = (digest, ids)
        return ids


def _no_model_garden() -> GardenComponent:
    class _NoModel:
        def complete(self, *_a, **_k):
            raise AssertionError("宿主驱动的会话不许由内核调模型")
    return GardenComponent(model=_NoModel())


def _request() -> ImportRequest:
    return ImportRequest(material=MATERIAL, locale="zh-Hans", batch_chars=200, idempotency_key="imp")


# ------------------------------------------------------------------ 宿主驱动

def test_host_driven_resume_after_write_before_commit_advances_without_the_model():
    model, db, garden = Model(), HostDb(), _no_model_garden()
    session = garden.import_session(_request(), owner_key="u1")
    before_crash = copy.deepcopy(asdict(session.progress))      # 宿主上次存下的进度
    batch = session.next_batch()
    while (prompt := batch.next_prompt()) is not None:
        batch.feed(model(prompt))
    outcome = batch.result()
    db.write(outcome.mutations, outcome.idempotency_key)
    # 💥 进程死在这里：写进去了，commit / 存进度都没发生。

    model.round = 2
    resumed = garden.import_session(_request(), owner_key="u1",
                                    progress=ImportProgress(**before_crash),
                                    existing_cards=list(db.cards.values()))
    batch = resumed.next_batch()
    assert batch.offset == 0 and batch.idempotency_key in db.log

    # 不照文档做（重问模型再 commit）就是原来的死结：回复变了，id 数对不上。
    probe = resumed.next_batch()
    while (prompt := probe.next_prompt()) is not None:
        probe.feed(model(prompt))
    rerun = probe.result()
    with pytest.raises(ValueError, match="不一致"):
        resumed.commit(rerun, record_ids=db.log[rerun.idempotency_key][1])

    # 按文档：先查写入记录，命中就 commit_applied，不调模型。
    calls = model.calls
    batch = resumed.next_batch(existing_cards=list(db.cards.values()))
    progress = resumed.commit_applied(batch, record_ids=db.log[batch.idempotency_key][1])
    assert model.calls == calls
    assert progress.cursor == batch.end and progress.batches_done == 1
    assert progress.cards_written == 1
    assert {"offset": 0, "reason": "already_applied"} in progress.skipped

    nxt = resumed.next_batch(existing_cards=list(db.cards.values()))
    assert nxt.offset == batch.end
    while (prompt := nxt.next_prompt()) is not None:
        nxt.feed(model(prompt))
    out = nxt.result()
    resumed.commit(out, record_ids=db.write(out.mutations, out.idempotency_key))
    assert resumed.progress.done
    assert sorted(c["summary"] for c in db.cards.values()) == ["不吃辣", "周末爬山"]


def test_commit_applied_checks_the_batch_and_refuses_candidate_batches():
    garden = _no_model_garden()
    session = garden.import_session(_request(), owner_key="u1")
    first = session.next_batch()
    session.commit_applied(first)
    with pytest.raises(ValueError, match="不是会话当前待提交的批次"):
        session.commit_applied(first)
    two_pass = garden.import_session(
        ImportRequest(material=MATERIAL, locale="zh-Hans", batch_chars=200, strategy="two_pass"))
    candidates = two_pass.next_batch()
    assert candidates.stage == "candidates"
    with pytest.raises(ValueError, match="候选批次"):
        two_pass.commit_applied(candidates)


def test_tenant_binds_the_fingerprint_and_defaults_keep_old_progress():
    garden = _no_model_garden()
    legacy = garden.import_session(_request(), owner_key="u1").progress.import_fingerprint
    assert garden.import_session(_request(), owner_key="u1", tenant="").progress.import_fingerprint \
        == legacy
    tenant_a = garden.import_session(_request(), owner_key="u1", tenant="a")
    saved = asdict(tenant_a.progress)
    tenant_a.commit_applied(tenant_a.next_batch())
    saved = asdict(tenant_a.progress)
    with pytest.raises(ValueError, match="当前请求不同"):
        garden.import_session(_request(), owner_key="u1", tenant="b",
                              progress=ImportProgress(**saved))
    # 与 MountedGarden 绑同一对 (tenant, owner)：两边的进度可以互相续传。
    mounted = MountedGarden(model=Model(), store=InMemoryStore())
    assert mounted.import_session(ALICE, _request()).progress.import_fingerprint == \
        garden.import_session(_request(), owner_key="alice", tenant="t").progress.import_fingerprint


# ------------------------------------------------------------------ Store 路径

def test_mounted_import_resume_after_write_before_progress_save_is_not_stuck():
    store, model = InMemoryStore(), Model()
    garden = MountedGarden(model=model, store=store)
    before_crash = copy.deepcopy(asdict(garden.import_session(ALICE, _request()).progress))
    garden.import_history(ALICE, _request(), max_batches=1)      # 写了第一批，进度没存下来

    model.round = 2
    for _attempt in range(3):
        progress = garden.import_history(ALICE, _request(), progress=ImportProgress(**before_crash))
        if progress.done:
            break
    assert progress.done and progress.failed == []
    assert {"offset": 0, "reason": "already_applied"} in progress.skipped
    summaries = sorted(c["summary"] for c in store.load("t", owner="alice").cards)
    assert summaries == ["不吃辣", "周末爬山"], "第一批不许写第二份"


def _svc(store, model=None):
    return Service(MountedGarden(model=model or Model(), store=store),
                   model_available=model is not None)


def _begin(service, **extra):
    out = service.handle({"id": "b", "method": "history.import_begin", "params": {
        "scope": WIRE_ALICE, "material": MATERIAL, "locale": "zh-Hans", "batch_chars": 200,
        "idempotency_key": "imp", **extra}})
    assert out["ok"], out
    return out["result"]


def _feed(service, state, reply):
    out = service.handle({"id": "f", "method": "history.import_feed", "params": {
        "session_id": state["session_id"], "reply": reply}})
    assert out["ok"], out
    return out["result"]


def test_wire_service_mode_resume_with_a_different_reply_moves_on():
    store, model = InMemoryStore(), Model()
    service = _svc(store)
    state = _begin(service)
    crash_progress = json.loads(json.dumps(state["progress"]))   # 宿主存下的最后一份
    state = _feed(service, state, model(state["next_prompt"]))    # 服务写了第一批
    assert state["progress"]["cursor"] > 0                        # 这份进度没来得及存

    model.round = 2
    restarted = _svc(store)
    state = _begin(restarted, progress=crash_progress)
    assert state["batch"]["offset"] == 0
    state = _feed(restarted, state, model(state["next_prompt"]))
    assert state["status"] == "needs_model", state
    assert {"offset": 0, "reason": "already_applied"} in state["progress"]["skipped"]
    assert state["committed"]["reason"] == "already_applied"
    state = _feed(restarted, state, model(state["next_prompt"]))
    assert state["status"] == "completed" and state["progress"]["done"]
    assert sorted(c["summary"] for c in store.load("t", owner="alice").cards) == ["不吃辣", "周末爬山"]


def test_wire_host_mode_commit_already_applied_advances_from_needs_model():
    db, model = HostDb(), Model()
    service = _svc(InMemoryStore())
    state = _begin(service, write_mode="host")
    crash_progress = json.loads(json.dumps(state["progress"]))
    state = _feed(service, state, model(state["next_prompt"]))
    assert state["status"] == "needs_commit"
    db.write(state["batch"]["mutations"], state["batch"]["idempotency_key"])
    # 💥 写进宿主库之后、import_commit 之前崩了。

    restarted = _svc(InMemoryStore())
    state = _begin(restarted, write_mode="host", progress=crash_progress,
                   existing_cards=list(db.cards.values()))
    key = state["batch"]["idempotency_key"]
    assert state["status"] == "needs_model" and key in db.log
    out = restarted.handle({"id": "c", "method": "history.import_commit", "params": {
        "session_id": state["session_id"], "record_ids": db.log[key][1],
        "already_applied": True}})
    assert out["ok"], out
    state = out["result"]
    assert state["status"] == "needs_model" and state["batch"]["offset"] > 0
    assert state["progress"]["cards_written"] == 1
    assert {"offset": 0, "reason": "already_applied"} in state["progress"]["skipped"]


# ------------------------------------------------------------------ 服务中途出错也带回进度

class BreaksAfterFirstWrite(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    def apply(self, *args, **kwargs):
        result = super().apply(*args, **kwargs)
        self.broken = True
        return result

    def load(self, *args, **kwargs):
        if self.broken:
            raise OSError("disk gone")
        return super().load(*args, **kwargs)


def test_wire_import_reports_progress_when_reading_the_next_batch_fails():
    store, model = BreaksAfterFirstWrite(), Model()
    service = _svc(store)
    state = _begin(service)
    out = service.handle({"id": "f", "method": "history.import_feed", "params": {
        "session_id": state["session_id"], "reply": model(state["next_prompt"])}})
    assert out["ok"], out
    failed = out["result"]
    assert failed["status"] == "failed" and failed["error"] == "storage_failed:OSError"
    assert failed["progress"]["cursor"] > 0 and failed["progress"]["batches_done"] == 1
    assert failed["committed"]["written"] is True
    late = service.handle({"id": "x", "method": "history.import_fail", "params": {
        "session_id": state["session_id"], "error": "give_up"}})
    assert late["ok"] is False and late["error"]["code"] == "unknown_session"


def test_import_begin_at_capacity_still_reports_session_capacity():
    service = Service(MountedGarden(model=None, store=InMemoryStore()),
                      model_available=False, max_active_sessions=1)
    _begin(service)
    out = service.handle({"id": "b", "method": "history.import_begin", "params": {
        "scope": WIRE_ALICE, "material": MATERIAL, "locale": "zh-Hans", "batch_chars": 200}})
    assert out["ok"] is False and out["error"]["code"] == "session_capacity"


# ------------------------------------------------------------------ 编造的 target

def test_import_drops_a_fabricated_supersede_target_and_keeps_the_good_card():
    real = {"id": "real_1", "summary": "早就记过：不吃香菜", "content": "不吃香菜。"}
    bad = _reply(_card("不吃辣", action="supersede", target="FAKE-123"), _card("周末爬山"))
    session = _no_model_garden().import_session(_request(), owner_key="u1", existing_cards=[real])
    batch = session.next_batch()
    prompts = 0
    while (prompt := batch.next_prompt()) is not None:
        prompts += 1
        batch.feed(bad)
    outcome = batch.result()
    assert prompts == 2, "编造的 target 先重问一次"
    assert [m["op"] for m in outcome.mutations] == ["add"]
    assert outcome.mutations[0]["card"]["summary"] == "周末爬山"
    assert outcome.trace["dropped_unknown_target"] == 1

    ok = _reply(_card("不吃香菜也不吃辣", action="supersede", target="real_1"))
    batch = session.next_batch()
    while (prompt := batch.next_prompt()) is not None:
        batch.feed(ok)
    assert [m.get("target_id") for m in batch.result().mutations] == ["real_1"]


def test_store_path_picks_the_index_once_per_batch():
    """prepare_import_batch 以前先取一次批（按旧索引挑卡）再带着重读的卡取第二次：卡多于 60 张时
    每批白跑一遍 BM25。"""
    from memgarden.retrieval import DefaultTokenizer

    class Counting(DefaultTokenizer):
        name = "counting"
        windows = 0

        def tokenize(self, text):
            if "周末常去爬山" in text and "早就记过" not in text:
                Counting.windows += 1
            return super().tokenize(text)

    store = InMemoryStore()
    store.apply("t", [{"op": "add", "card": {"summary": f"早就记过的第{i}件事", "content": "正文"}}
                      for i in range(70)], owner="alice", idempotency_key="seed")
    garden = MountedGarden(model=Model(), store=store, tokenizer=Counting())
    garden.import_history(ALICE, _request(), max_batches=2)   # 第二批时会话已经带着上一批读到的卡
    assert Counting.windows == 1
