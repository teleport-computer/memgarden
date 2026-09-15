"""主动搜索合同：只返回真实命中，无命中为空（MG-5）。

这条合同以前不存在：``MountedGarden`` 的 ``memory_search`` 工具复用 ``context_for_turn``，
挑卡策略里有 ``RecentStage`` 时，搜一个花园里根本没有的东西也会拿回「最近写的几张」
（Seven 2026-09-14 §4.2）。自动想起可以打底，主动搜索不行。

另按 Seven §7.1 覆盖第三方宿主的验收项：中文 / 英文 / 编号 / 短语的命中与无命中、
另一个 owner 搜不到、真删之后搜不到、被取代的旧卡不出现，以及 SDK / JSON Lines
两条入口各自声明并接通。全部纯合成数据，不 import 任何宿主模块。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

_SRC_PATH = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

import pytest  # noqa: E402

from memgarden import (  # noqa: E402
    GardenComponent, MountedGarden, Scope, SearchRequest, SqliteStore,
)
from memgarden.contracts import ToolCall  # noqa: E402
from memgarden.mounted import MountPermissionError  # noqa: E402
from memgarden.retrieval import select_context  # noqa: E402
from memgarden.schema import schemas  # noqa: E402
from memgarden.selection import Chain, RecentStage, RelevanceStage  # noqa: E402
from memgarden.service import Service  # noqa: E402

ALICE = Scope(tenant_id="t", memory_owner_id="alice")
BOB = Scope(tenant_id="t", memory_owner_id="bob")

CARDS = [
    {"id": "spicy", "summary": "不吃辣，一吃就胃疼", "content": "点菜要避开辣味。",
     "bucket": "偏好与边界", "created_at": "2026-01-01T00:00:00Z"},
    {"id": "jira", "summary": "JIRA-4821 线上事故复盘", "content": "token refresh 失败，回滚后恢复。",
     "bucket": "工作", "created_at": "2026-02-01T00:00:00Z"},
    {"id": "aurora", "summary": "Project Aurora deadline moved", "content": "The launch slipped to October.",
     "bucket": "Work", "created_at": "2026-03-01T00:00:00Z"},
    {"id": "plant", "summary": "买了一盆绿萝", "content": "放在窗台上。",
     "bucket": "生活", "created_at": "2026-09-14T00:00:00Z"},
] + [{"id": f"f{i:02d}", "summary": f"周{i}吃了一碗面", "content": "味道一般。",
      "bucket": "生活", "created_at": "2025-06-01T00:00:00Z"} for i in range(12)]


class _NoModel:
    def complete(self, prompt, *, purpose=""):
        raise AssertionError("搜索不该调模型")


def _seeded(owner: str = "alice", cards=CARDS) -> SqliteStore:
    store = SqliteStore(str(pathlib.Path(tempfile.mkdtemp()) / "g.db"))
    store.apply("t", [{"op": "add", "card": dict(c)} for c in cards],
                owner=owner, idempotency_key="seed")
    return store


def _mounted(store) -> MountedGarden:
    # 策略里**故意**带 RecentStage —— 这正是以前让搜索混进最近卡的配置。
    return MountedGarden(model=_NoModel(), store=store,
                         selection_policy=Chain(stages=(RelevanceStage(limit=8),
                                                        RecentStage(limit=4))))


# --------------------------------------------------------------------------- #
# 组件层：GardenComponent.search
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("query, expected", [
    ("辣", "spicy"),                         # 中文单字
    ("点菜避开辣味", "spicy"),                # 中文短语
    ("JIRA-4821", "jira"),                   # 编号
    ("aurora deadline", "aurora"),           # 英文
])
def test_component_search_hits(query, expected):
    out = GardenComponent(model=_NoModel()).search(SearchRequest(query=query, candidates=CARDS))
    assert out.record_ids[:1] == [expected]
    assert out.hits[0]["id"] == expected and out.hits[0]["score"] > 0
    assert out.ranking.startswith("memgarden-bm25-v1+tok:")


@pytest.mark.parametrize("query", ["我有没有去过冰岛", "what's my blood type", "PR #999", ""])
def test_component_search_no_hit_is_empty(query):
    out = GardenComponent(model=_NoModel()).search(SearchRequest(query=query, candidates=CARDS))
    assert out.record_ids == [] and out.hits == []
    assert out.ranking, "无命中也要带排序版本 —— trace 才对得上是哪把尺子说的「没有」"


def test_search_and_automatic_recall_report_the_same_ruler():
    out = GardenComponent(model=_NoModel()).search(SearchRequest(query="辣", candidates=CARDS))
    _picked, trace = select_context("辣", CARDS)
    assert out.ranking == trace["version"]


def test_component_tokenizer_is_injectable_and_named_in_the_ranking():
    class Upper:
        name = "whitespace-test"

        def tokenize(self, text):
            return str(text).casefold().split()

    garden = GardenComponent(model=_NoModel(), tokenizer=Upper())
    out = garden.search(SearchRequest(query="aurora", candidates=CARDS))
    assert out.record_ids == ["aurora"]
    assert out.ranking.endswith("+tok:whitespace-test")


def test_component_search_limit():
    out = GardenComponent(model=_NoModel()).search(
        SearchRequest(query="周 面", candidates=CARDS, limit=3))
    assert len(out.record_ids) == 3


# --------------------------------------------------------------------------- #
# 挂载层：MountedGarden.search 与 memory_search 工具
# --------------------------------------------------------------------------- #

def test_tool_search_with_recent_stage_in_policy_returns_nothing_on_no_hit():
    garden = _mounted(_seeded())
    # 同一个查询，自动想起会带最近卡（RecentStage 的设计）……
    assert "plant" in garden.context_for_turn(ALICE, "我有没有去过冰岛").record_ids
    # ……主动搜索不许
    assert garden.search(ALICE, "我有没有去过冰岛").record_ids == []
    out = garden.invoke_tool(ALICE, ToolCall(name="memory_search",
                                             arguments={"query": "我有没有去过冰岛"}))
    assert out.ok and out.content == ""


def test_tool_search_returns_only_real_matches():
    garden = _mounted(_seeded())
    out = garden.invoke_tool(ALICE, ToolCall(name="memory_search", arguments={"query": "JIRA-4821"}))
    assert out.ok and out.content == "- JIRA-4821 线上事故复盘"


def test_another_owner_cannot_search():
    garden = _mounted(_seeded(owner="alice"))
    assert garden.search(ALICE, "辣").record_ids == ["spicy"]
    assert garden.search(BOB, "辣").record_ids == []
    out = garden.invoke_tool(BOB, ToolCall(name="memory_search", arguments={"query": "辣"}))
    assert out.content == ""


def test_deleted_card_is_not_searchable():
    garden = _mounted(_seeded())
    assert garden.search(ALICE, "JIRA-4821").record_ids == ["jira"]
    receipt = garden.delete_record(ALICE, "jira", requested_by="alice")
    assert not receipt.error, receipt.error
    assert garden.search(ALICE, "JIRA-4821").record_ids == []
    reopened = _mounted(garden._store)
    assert reopened.search(ALICE, "JIRA-4821").record_ids == []


def test_superseded_card_is_not_searchable():
    store = _seeded()
    store.apply("t", [{"op": "supersede", "target_id": "spicy",
                       "card": {"id": "spicy-v2", "summary": "现在能吃微辣", "content": "胃好了，微辣可以。",
                                "bucket": "偏好与边界"}}],
                owner="alice", idempotency_key="supersede-1")
    ids = _mounted(store).search(ALICE, "辣").record_ids
    assert "spicy-v2" in ids and "spicy" not in ids


def test_mount_narrowing_is_permission_checked():
    garden = _mounted(_seeded())
    with pytest.raises(MountPermissionError):
        garden.search(ALICE, "辣", mount="family-shared")


# --------------------------------------------------------------------------- #
# JSON Lines：records.search 声明并接通
# --------------------------------------------------------------------------- #

def test_wire_search_is_declared_reachable_and_matches_schema():
    jsonschema = pytest.importorskip("jsonschema")
    service = Service(_mounted(_seeded()), model_available=False)
    manifest = service.handle({"id": "m", "method": "manifest.get"})["result"]
    assert manifest["capabilities"]["search"] is True
    assert "records.search" in manifest["operations"]

    scope = {"tenant_id": "t", "memory_owner_id": "alice"}
    hit = service.handle({"id": "1", "method": "records.search",
                          "params": {"scope": scope, "query": "JIRA-4821"}})
    assert hit["ok"], hit
    assert hit["result"]["record_ids"] == ["jira"]
    miss = service.handle({"id": "2", "method": "records.search",
                           "params": {"scope": scope, "query": "我有没有去过冰岛"}})
    assert miss["ok"] and miss["result"]["record_ids"] == [] and miss["result"]["hits"] == []

    all_schemas = schemas()
    resolver_schema = {**all_schemas["SearchResult"], "definitions": all_schemas}
    for result in (hit["result"], miss["result"]):
        jsonschema.validate(json.loads(json.dumps(result)), resolver_schema)

    other = service.handle({"id": "3", "method": "records.search", "params": {
        "scope": {"tenant_id": "t", "memory_owner_id": "bob"}, "query": "JIRA-4821"}})
    assert other["ok"] and other["result"]["record_ids"] == []


def test_wire_search_requires_a_query():
    service = Service(_mounted(_seeded()), model_available=False)
    out = service.handle({"id": "1", "method": "records.search",
                          "params": {"scope": {"tenant_id": "t", "memory_owner_id": "alice"}}})
    assert out["ok"] is False and out["error"]["code"] == "invalid_request"


def test_mounted_garden_passes_the_tokenizer_through():
    class Ws:
        name = "ws-mounted"

        def tokenize(self, text):
            return str(text).casefold().split()

    garden = MountedGarden(model=_NoModel(), store=_seeded(), tokenizer=Ws())
    out = garden.search(ALICE, "aurora")
    assert out.record_ids == ["aurora"] and out.ranking.endswith("+tok:ws-mounted")
