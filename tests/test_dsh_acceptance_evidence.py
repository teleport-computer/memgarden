"""DSH 真实验收的判据本身不能假通过。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "adapters" / "dsh-memgarden" / "e2e" / "dsh_acceptance.py"
SPEC = importlib.util.spec_from_file_location("dsh_acceptance_evidence", SCRIPT)
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def test_acceptance_model_override_is_explicit_and_reaches_harness(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMGARDEN_ACCEPTANCE_MODEL", raising=False)
    assert acceptance._acceptance_model() == "deepseek-v4-flash"
    monkeypatch.setenv("MEMGARDEN_ACCEPTANCE_MODEL", "synthetic-model")
    monkeypatch.setenv("MEMGARDEN_DEBUG_LOG", "")
    monkeypatch.setattr(acceptance, "_HARNESS_CLASS", lambda **kwargs: kwargs)
    env = SimpleNamespace(log=tmp_path / "log", workspace=tmp_path,
                          home=tmp_path, dsh_bin=tmp_path / "dsh")
    assert acceptance.Env.harness(env)["model"] == "synthetic-model"
    monkeypatch.setenv("MEMGARDEN_ACCEPTANCE_MODEL", "  ")
    with pytest.raises(ValueError, match="must not be blank"):
        acceptance._acceptance_model()


def test_generic_dinner_advice_does_not_prove_recall():
    assert not acceptance._recall_is_proven("召回 0 条\n", "建议吃清淡、不辣的食物", [])
    assert not acceptance._recall_is_proven("召回 1 条\n", "建议避免辣味", [])
    assert acceptance._recall_is_proven("召回 1 条\n", "会胃疼", [])
    searched = [{"type": "tool/call", "data": {
        "name": "memgarden_memory_search",
    }}]
    assert not acceptance._recall_is_proven("召回 1 条\n", "会胃疼", searched)


def test_automatic_capture_card_does_not_prove_tool_use():
    events = [{"type": "tool/call", "data": {
        "name": "memgarden_memory_write",
    }}]
    automatic = [{
        "source": "conversation_capture",
        "summary": acceptance.TOOL_PROBE,
        "content": acceptance.TOOL_PROBE,
    }]
    tool_card = [{
        "source": "model_tool",
        "summary": acceptance.TOOL_PROBE,
        "content": acceptance.TOOL_PROBE,
    }]
    assert not acceptance._tool_call_is_proven([], tool_card, acceptance.TOOL_PROBE)
    assert not acceptance._tool_call_is_proven(events, automatic, acceptance.TOOL_PROBE)
    assert acceptance._tool_call_is_proven(events, tool_card, acceptance.TOOL_PROBE)


def test_maintenance_log_alone_does_not_prove_durable_success():
    ledger = {"signature": "sig", "seed_card_count": 10}
    cards = [
        {"id": "old", "archived": True, "superseded_by": "dream"},
        {"id": "dream", "source": "memory_dream"},
    ]
    failed = "[memgarden] 整理结果 written=false reason=- error=parse_failed\n"
    success = "[memgarden] 整理结果 written=true reason=- error=-\n"
    assert not acceptance._maintenance_is_proven(failed, cards, ledger)
    assert not acceptance._maintenance_is_proven(success, cards, {})
    assert not acceptance._maintenance_is_proven(success, [{"id": "old"}], ledger)
    assert acceptance._maintenance_is_proven(success, cards, ledger)


def test_tool_write_diagnostic_distinguishes_call_from_persistence_without_bodies():
    events = [{"type": "tool/call", "data": {
        "name": "memgarden_memory_write", "arguments": "PRIVATE BODY",
    }}] * 30
    cards = [{"source": "conversation_capture", "summary": "MARKER PRIVATE BODY",
              "content": "PRIVATE BODY"}] * 30
    detail = acceptance._tool_write_diagnostic(events, cards, "MARKER")
    assert "PRIVATE BODY" not in detail and "MARKER" not in detail
    assert '"card_count":30' in detail
    assert '"summary_has_marker":true' in detail
    assert '"content_has_marker":false' in detail
    assert "conversation_capture" in detail and "memgarden_memory_write" in detail
    assert len(detail) < 4000


def test_maintenance_failure_diagnostic_is_bounded_without_card_bodies():
    logs = "\n".join([
        "[memgarden] 召回 0 条",
        "[memgarden] 该整理了: threshold",
        "[memgarden] 整理失败: timeout",
    ])
    detail = acceptance._maintenance_diagnostic(
        logs,
        [{"id": "old", "source": "history_import", "summary": "PRIVATE",
          "archived": False}],
        {"signature": "sig", "seed_card_count": 10},
    )
    assert "PRIVATE" not in detail
    assert "整理失败" in detail
    assert '"source":"history_import"' in detail
    assert len(detail) <= 4000


def test_pinned_sdk_requires_same_official_source_commit():
    assert acceptance._sdk_compatibility_error("0.1.2a3", "")
    assert acceptance._sdk_compatibility_error("0.0.0.dev0", "wrong")
    assert not acceptance._sdk_compatibility_error(
        "0.0.0.dev0", acceptance.DSH_COMMIT,
    )


def test_dsh_version_timeout_is_a_diagnostic_failure(monkeypatch):
    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=15)

    monkeypatch.setattr(acceptance.subprocess, "run", time_out)
    error = acceptance._dsh_version_error(Path("/stuck/dsh"))
    assert "无法执行 dsh --version" in error


def test_group_selection_defaults_to_all_and_supports_single_group():
    assert acceptance._selected_groups([]) == list(acceptance._GROUPS.values())
    assert acceptance._selected_groups(["--group", "E"]) == [acceptance.group_e]


def test_help_does_not_require_a_key_or_installed_dsh():
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env["PATH"] = ""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--group" in proc.stdout


def test_published_dsh_requires_a_uniform_alpha4_dependency_closure(tmp_path):
    modules = tmp_path / "node_modules"

    def package(name, version):
        directory = modules / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(
            '{"name": "' + name + '", "version": "' + version + '"}',
            encoding="utf-8",
        )
        return directory

    top = package("@deepseek-ai/dsh", acceptance.DSH_VERSION)
    binary = top / "lib" / "bin.js"
    binary.parent.mkdir()
    binary.write_text("", encoding="utf-8")
    package("@deepseek-ai/dsh-base", "0.1.2-rc.1")
    assert "dsh-base@0.1.2-rc.1" in acceptance._dsh_closure_error(binary)

    (modules / "@deepseek-ai/dsh-base/package.json").write_text(
        '{"name": "@deepseek-ai/dsh-base", '
        '"version": "' + acceptance.DSH_VERSION + '"}',
        encoding="utf-8",
    )
    assert acceptance._dsh_closure_error(binary) == ""

    (modules / "@deepseek-ai/dsh-base/package.json").write_text(
        "{broken", encoding="utf-8",
    )
    assert "npm 安装损坏" in acceptance._dsh_closure_error(binary)
