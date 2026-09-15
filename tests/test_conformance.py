"""共用写入路径验收场景：参考宿主全绿，且每类场景都真的会红。

参考宿主（MountedGarden + SqliteStore / InMemoryStore）必须**零声明**通过全部场景 ——
它就是这些语义的定义方。下面的「坏宿主」各自只破坏一条语义，用来证明场景不是
摆设：破坏哪条，对应场景就必须报 fail（Seven 2026-09-14 §4.5 / §7.1）。
"""
from __future__ import annotations

import itertools

import pytest

from memgarden import conformance as kit
from memgarden.conformance import Deviation, Outcome, ReferenceHost


@pytest.fixture(params=["sqlite", "memory"])
def factory(request, tmp_path):
    counter = itertools.count()

    def make(cls=ReferenceHost):
        return cls(request.param, path=tmp_path / f"{next(counter)}.db")
    return make


def _by_id(results):
    return {r.scenario: r for r in results}


def test_reference_hosts_pass_every_scenario_without_declarations(factory):
    results = kit.run_all(factory)
    assert [r.scenario for r in results] == list(kit.scenario_ids())
    kit.assert_conformant(results)
    assert {r.status for r in results} == {"pass"}, kit.results_table(results)


def test_scenario_ids_and_clause_prefixes_are_unique_and_stable():
    ids = kit.scenario_ids()
    assert len(ids) == len(set(ids)) == 22
    assert kit.SCENARIO_VERSION == 1


# --------------------------------------------------------------------------- #
# 坏宿主：每个只破坏一条语义
# --------------------------------------------------------------------------- #

class ArchivingDelete(ReferenceHost):
    """「删除」其实是归档 —— 界面说删了，库里还在。"""

    def delete(self, owner, record_id, *, requested_by):
        return self.archive(owner, record_id, reason="pretend delete")


class SearchSeesRetired(ReferenceHost):
    """搜索候选没做生命周期过滤。"""

    def search(self, owner, query):
        return [v["id"] for v in self.history(owner) if query in str(v.get("content"))]


class SharedGarden(ReferenceHost):
    """owner 没落到存储查询里：所有人共用一座花园。"""

    def _scope(self, owner):
        return super()._scope("everyone")

    def inspect(self, owner, record_id):
        return super().inspect("everyone", record_id)

    def observe(self, owner):
        return super().observe("everyone")


class NoIdempotency(ReferenceHost):
    """每次重放都换一个新请求身份。"""

    def add(self, owner, card, *, request_id="", record_id=""):
        fresh = f"{request_id}:{next(self._n)}" if request_id else ""
        return super().add(owner, card, request_id=fresh, record_id=record_id)

    _n = itertools.count()


class FetchTouchesUpdatedAt(ReferenceHost):
    """读卡时顺手把 updated_at 刷新成现在。"""

    def fetch(self, owner, ids, *, include_history=False):
        for rid in ids:
            self.tick()
            view = self.inspect(owner, rid)
            if view and view["status"] == "active":
                super().patch(owner, rid, {"content": view["content"] + " "})
        return super().fetch(owner, ids, include_history=include_history)


class IgnoresRevision(ReferenceHost):
    """丢掉调用方给的并发凭据，总是按最新状态写。"""

    def patch(self, owner, record_id, changes, *, based_on=None):
        return super().patch(owner, record_id, changes, based_on=None)

    def supersede(self, owner, target_ids, card, *, based_on=None):
        current = [t for t in target_ids if (self.inspect(owner, t) or {}).get("status") == "active"]
        for old in target_ids:
            if old not in current and self.inspect(owner, old):
                # 退休过的也照样再取代一次。
                current.append(old)
        return super().supersede(owner, current, card, based_on=None)


class FailureLooksEmpty(ReferenceHost):
    """写库失败被吞成「没什么可记」，进度照推。"""

    def commit_capture(self, owner, cards, *, request_id, fail_storage=False):
        out = super().commit_capture(owner, cards, request_id=request_id, fail_storage=fail_storage)
        if not out.ok and out.error == "storage_failed":
            self._progress[owner] = self._progress.get(owner, frozenset()) | {request_id}
            return Outcome(ok=True, reason="nothing_to_keep")
        return out


class TruncatesContent(ReferenceHost):
    def add(self, owner, card, *, request_id="", record_id=""):
        return super().add(owner, {**card, "content": str(card["content"])[:5000]},
                           request_id=request_id, record_id=record_id)


class OverwritingAdd(ReferenceHost):
    """自带 id 撞上已有卡时直接覆盖。"""

    def add(self, owner, card, *, request_id="", record_id=""):
        if record_id and self.inspect(owner, record_id):
            self.delete(owner, record_id, requested_by="overwrite")
        return super().add(owner, card, request_id=request_id, record_id=record_id)


class PatchResetsOccurredAt(ReferenceHost):
    def patch(self, owner, record_id, changes, *, based_on=None):
        return super().patch(owner, record_id, {**changes, "occurred_at": "2030-01-01T00:00:00Z"},
                             based_on=based_on)


@pytest.mark.parametrize("broken, scenarios", [
    (ArchivingDelete, {"delete.hard", "delete.after_supersede"}),
    (SearchSeesRetired, {"supersede.links_history", "archive.retires"}),
    (SharedGarden, {"owner.isolation"}),
    (NoIdempotency, {"idempotency.replay", "idempotency.key_reuse"}),
    (FetchTouchesUpdatedAt, {"reads.no_side_effects"}),
    (IgnoresRevision, {"conflict.stale_patch", "supersede.concurrent_same_target"}),
    (FailureLooksEmpty, {"capture.write_failure"}),
    (TruncatesContent, {"content.length"}),
    (OverwritingAdd, {"add.supplied_id_never_overwrites"}),
    (PatchResetsOccurredAt, {"patch.preserves_provenance"}),
])
def test_each_broken_semantic_turns_its_scenario_red(factory, broken, scenarios):
    results = _by_id(kit.run_all(lambda: factory(broken), only=scenarios))
    red = {sid for sid, r in results.items() if r.status == "fail"}
    assert red == scenarios, {sid: r.failures for sid, r in results.items()}


# --------------------------------------------------------------------------- #
# 声明的三种结局
# --------------------------------------------------------------------------- #

def test_declared_failure_is_reported_as_deviation_with_evidence(factory):
    declared = {"patch.preserves_provenance/occurred_at": Deviation("by_design", "host resets")}
    [result] = kit.run_all(lambda: factory(PatchResetsOccurredAt), deviations=declared,
                           only={"patch.preserves_provenance"})
    assert result.status == "deviation"
    assert [f.clause for f in result.failures] == ["patch.preserves_provenance/occurred_at"]
    assert "2030-01-01" in result.failures[0].evidence
    kit.assert_conformant([result])


def test_bug_declaration_is_reported_as_bug(factory):
    declared = {"content.length/stored_whole": Deviation("bug", "truncates at 5000")}
    [result] = kit.run_all(lambda: factory(TruncatesContent), deviations=declared,
                           only={"content.length"})
    assert result.status == "bug"


def test_undeclared_extra_failure_still_fails(factory):
    declared = {"delete.hard/storage_gone": Deviation("by_design", "soft delete")}
    [result] = kit.run_all(lambda: factory(ArchivingDelete), deviations=declared,
                           only={"delete.hard"})
    assert result.status == "fail"
    assert any("undeclared failure" in p for p in result.problems)
    with pytest.raises(AssertionError, match="delete.hard"):
        kit.assert_conformant([result])


def test_stale_declaration_fails(factory):
    declared = {"content.length/stored_whole": Deviation("bug", "no longer true")}
    [result] = kit.run_all(factory, deviations=declared, only={"content.length"})
    assert result.status == "fail"
    assert result.problems == ("stale deviation (clause now passes): content.length/stored_whole",)


def test_misspelled_declaration_is_rejected(factory):
    with pytest.raises(ValueError, match="do not name a clause"):
        kit.run_all(factory, deviations={"delete.hardd/storage_gone": Deviation("bug", "x")})
    with pytest.raises(ValueError, match="do not name a clause"):
        kit.run_all(factory, deviations={"delete.hard": Deviation("bug", "x")})


def test_adapter_exception_is_evidence_not_a_crash(factory):
    class Explodes(ReferenceHost):
        def index(self, owner):
            raise RuntimeError("index offline")

    [result] = kit.run_all(lambda: factory(Explodes), only={"add.roundtrip"})
    assert result.status == "fail"
    assert result.failures[-1].clause == "add.roundtrip/raised"
    assert "index offline" in result.failures[-1].evidence


def test_results_table_names_every_scenario(factory):
    table = kit.results_table(kit.run_all(factory), host="reference")
    for sid in kit.scenario_ids():
        assert f"| {sid} | pass |" in table


def test_declarations_after_a_fatal_failure_are_not_judged_stale(factory):
    """致命条款失败后，后面没跑到的条款的声明不算过期（发布前复审）。"""
    class NoArchive(ReferenceHost):
        def archive(self, owner, record_id, *, reason=""):
            return Outcome(ok=False, error="unsupported")

    declared = {
        "archive.retires/ok": Deviation("by_design", "no archive"),
        "archive.retires/status": Deviation("by_design", "no archive"),
        "archive.retires/history_visible": Deviation("by_design", "no archive"),
    }
    [result] = kit.run_all(lambda: factory(NoArchive), deviations=declared, only={"archive.retires"})
    assert result.problems == ()
    assert result.status == "deviation"
    # 真跑过、真通过的条款，声明仍然判过期。
    passing = {"archive.retires/add": Deviation("bug", "stale")}
    [result] = kit.run_all(factory, deviations=passing, only={"archive.retires"})
    assert result.problems == ("stale deviation (clause now passes): archive.retires/add",)


def test_second_supersede_may_refuse_with_not_found_like_supersede_after_delete(factory):
    class NotFoundForRetiredTarget(ReferenceHost):
        def supersede(self, owner, target_ids, card, *, based_on=None):
            if any((self.inspect(owner, t) or {}).get("status") == "superseded" for t in target_ids):
                return Outcome(ok=False, error="not_found")
            return super().supersede(owner, target_ids, card, based_on=based_on)

    [result] = kit.run_all(lambda: factory(NotFoundForRetiredTarget),
                           only={"supersede.concurrent_same_target"})
    assert result.status == "pass", result
