"""每条接入面声明的能力必须和它**实际注册的方法**对得上（Seven 2026-09-14 §7.1）。

三条入口各查两个方向：

  · 声明的每组方法都真实存在（删了方法 / 改了名 → 红）
  · 实际存在的每个方法都归到某项能力或基础设施清单（加了方法没声明 → 红）

另外钉住当前的能力矩阵本身：哪条入口接了什么是**有意的产品边界**，变了要改这里
和 docs/INTEGRATION-AND-DATA.md 那张表，不能悄悄变。
"""
from __future__ import annotations

import re

import pytest

from memgarden import MountedGarden, Service
from memgarden.adapters.dsh import PLUGIN
from memgarden.schema import _CAPABILITY_BACKING, WIRE_OPERATIONS, manifest
from memgarden.stores.memory import InMemoryStore
from memgarden.surfaces import (
    CAPABILITIES, DSH_BACKING, SDK_BACKING, SDK_INFRASTRUCTURE, SURFACES,
    WIRE_INFRASTRUCTURE, available_methods, backing, surface_capabilities,
)

#: 当前矩阵。改它等于改产品边界。
EXPECTED = {
    #                 sdk    jsonl  dsh
    "capture":        (True, True, True),
    "turn_context":   (True, True, True),
    "search":         (True, True, False),
    "related":        (True, True, False),
    "maintenance":    (True, True, True),
    "model_tools":    (True, True, True),
    "curated_write":  (True, True, False),
    "browse":         (True, True, False),
    "export":         (True, True, False),
    "delete":         (True, True, False),
    "promote":        (True, True, False),
    "migrate":        (True, True, False),
    "history_import": (True, True, False),
    "import_session": (True, True, False),
}


def test_capability_matrix_is_snapshotted():
    got = surface_capabilities()
    assert set(got) == set(SURFACES)
    assert tuple(CAPABILITIES) == tuple(EXPECTED)
    for name, row in EXPECTED.items():
        assert tuple(got[s][name] for s in SURFACES) == row, name


def _lane_methods(lanes) -> set[str]:
    return {m for alternatives in lanes.values() for lane in alternatives for m in lane}


@pytest.mark.parametrize("surface", SURFACES)
def test_every_declared_method_exists(surface):
    methods = available_methods(surface)
    for name, alternatives in backing(surface).items():
        assert name in CAPABILITIES, f"{surface} 声明了未知能力 {name}"
        for lane in alternatives:
            missing = [m for m in lane if m not in methods]
            assert not missing, f"{surface}.{name} 声明的方法不存在: {missing}"


def test_every_public_sdk_method_is_declared():
    undeclared = available_methods("sdk") - _lane_methods(SDK_BACKING) - SDK_INFRASTRUCTURE
    assert not undeclared, f"MountedGarden 新增了公开方法却没归到任何能力: {sorted(undeclared)}"


def test_every_wire_method_is_declared_and_the_service_registers_exactly_those():
    service = Service(MountedGarden(model=None, store=InMemoryStore()),
                      model_available=False)
    assert set(service._methods) == set(WIRE_OPERATIONS)
    jsonl = backing("jsonl")
    # tools 是 model_tools 的历史别名，同一组方法。
    assert _CAPABILITY_BACKING["tools"] == _CAPABILITY_BACKING["model_tools"]
    assert set(_CAPABILITY_BACKING) - {"tools"} == set(jsonl)
    undeclared = set(WIRE_OPERATIONS) - _lane_methods(jsonl) - WIRE_INFRASTRUCTURE
    assert not undeclared, f"wire 新增了方法却没归到任何能力: {sorted(undeclared)}"


def test_jsonl_declaration_is_what_manifest_get_reports_on_a_full_service():
    class _Model:
        def complete(self, prompt, *, purpose=""):  # pragma: no cover
            return "{}"

    static = manifest()["capabilities"]
    live = Service(MountedGarden(model=_Model(), store=InMemoryStore())).handle(
        {"id": "m", "method": "manifest.get"})["result"]["capabilities"]
    declared = surface_capabilities()["jsonl"]
    for name in CAPABILITIES:
        assert static[name] == live[name] == declared[name], name


def test_dsh_declaration_is_exactly_what_the_plugin_calls():
    called = set(re.findall(r"client\.request\(\s*['\"]([a-z_.]+)['\"]",
                            PLUGIN.read_text("utf-8")))
    assert called == set(available_methods("dsh"))
    assert called <= set(WIRE_OPERATIONS), f"插件调了服务没有的方法: {called - set(WIRE_OPERATIONS)}"
    undeclared = called - _lane_methods(DSH_BACKING) - WIRE_INFRASTRUCTURE
    assert not undeclared, f"插件调了方法却没声明成能力: {sorted(undeclared)}"
    # 插件里没有别的调用形状（例如把方法名先放进变量）逃过上面的扫描。
    assert "client.request(" in PLUGIN.read_text("utf-8")
    assert not re.search(r"client\.request\(\s*[^'\"\s]", PLUGIN.read_text("utf-8"))


def test_dsh_capability_needs_the_whole_lane_to_exist_on_the_wire(monkeypatch):
    import memgarden.surfaces as surfaces

    real = surfaces.available_methods

    def fake(surface):
        if surface == "jsonl":
            return real("jsonl") - {"capture.cancel"}
        return real(surface)

    monkeypatch.setattr(surfaces, "available_methods", fake)
    caps = surfaces.surface_capabilities()
    assert caps["dsh"]["capture"] is False and caps["jsonl"]["capture"] is True


def test_unknown_surface_is_refused():
    with pytest.raises(ValueError):
        backing("mcp")
    with pytest.raises(ValueError):
        available_methods("mcp")
