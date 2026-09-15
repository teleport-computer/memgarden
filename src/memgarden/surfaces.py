"""每个接入面**各自**接通了哪些记忆能力。

## 为什么要分面声明

同一个能力名在三条入口上不是一回事：

    SDK         ``MountedGarden`` 上的方法（Python 宿主直接调）
    JSON Lines  ``memgarden serve`` 的方法表（非 Python Runtime 调）
    DSH         随包发布的 DeepSeek Harness Adapter 实际调用的 wire 方法

「SDK 有关联读取」不等于「DSH 用户拿到了关联读取」。以前只有 JSON Lines 的
``manifest.get`` 有一份能力表，于是很容易拿一条入口的完成度去说另外两条。

## 声明怎么保证不漂

每项能力都写成「由哪几组方法撑着」（外层 OR、内层 AND，和
:data:`memgarden.schema._CAPABILITY_BACKING` 同一个约定）。声明的真假由
**实际存在的方法**算出来，不手写 True/False：

    SDK         方法在 ``MountedGarden`` 上真的可调
    JSON Lines  方法在 ``Service`` 的方法表里（同 ``manifest.get``）
    DSH         ``plugin.mjs`` 里真的有 ``client.request('<method>'`` 这一行

``tests/test_surfaces.py`` 反向再核一遍：每条入口上**所有**公开方法都必须归到
某项能力或明确的基础设施清单里 —— 加了方法忘了声明、声明了方法却删了，都会红。

这里给的是**静态**接线情况。连上一个具体服务后，``manifest.get`` 还会按模型与
Store 的实际能力再关掉一部分（例如无模型服务的 ``history_import``）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

__all__ = [
    "CAPABILITIES",
    "SURFACES",
    "SDK_BACKING",
    "SDK_INFRASTRUCTURE",
    "DSH_BACKING",
    "WIRE_INFRASTRUCTURE",
    "backing",
    "available_methods",
    "surface_capabilities",
]

#: 规范能力名。和 ``manifest.get`` 的 ``capabilities`` 键同名（``tools`` 是
#: ``model_tools`` 的历史别名，不另列）。
CAPABILITIES: tuple[str, ...] = (
    "capture",          # 对话后判断什么值得记并写入
    "turn_context",     # 每轮自动想起
    "search",           # 主动搜索：只返回真实命中
    "related",          # 取回卡时的一跳关联
    "maintenance",      # 整理（Dream）
    "model_tools",      # 给对话模型的 memory_search / memory_write
    "curated_write",    # 用户明说要记
    "browse",
    "export",
    "delete",           # 用户删除 = 真删
    "promote",          # 换挂载点
    "migrate",          # 老卡字段升级
    "history_import",   # 一次调用跑完（服务侧模型）
    "import_session",   # 宿主驱动的分批导入（宿主调模型）
)

SURFACES: tuple[str, ...] = ("sdk", "jsonl", "dsh")

#: SDK：``MountedGarden`` 的方法。
SDK_BACKING: dict[str, tuple[tuple[str, ...], ...]] = {
    "capture": (("capture_and_store",),
                ("prepare_capture", "store_capture_result")),
    "turn_context": (("context_for_turn",),),
    "search": (("search",),),
    "related": (("related",),),
    "maintenance": (("check_maintenance", "run_and_store_maintenance"),
                    ("check_maintenance", "prepare_maintenance",
                     "store_maintenance_result")),
    "model_tools": (("tools", "invoke_tool"),),
    "curated_write": (("write_one",),),
    "browse": (("browse",),),
    "export": (("export",),),
    "delete": (("delete_record",),),
    "promote": (("promote",),),
    "migrate": (("migrate_and_store",),),
    "history_import": (("import_history",),),
    "import_session": (("import_session", "prepare_import_batch",
                        "store_import_batch"),),
}

#: SDK 上不代表一项能力、但确实公开的方法。
SDK_INFRASTRUCTURE: frozenset[str] = frozenset({"maintenance_ledger"})

#: DSH Adapter 调用的 wire 方法。**只列插件里真的调了的**。
#: 主动搜索、用户写卡在 DSH 上走的是模型工具（``model_tools``），不是
#: ``records.search`` / ``records.write`` —— 所以那两项在 DSH 上是 False。
DSH_BACKING: dict[str, tuple[tuple[str, ...], ...]] = {
    "capture": (("capture.begin", "capture.feed", "capture.cancel"),),
    "turn_context": (("context.get",),),
    "maintenance": (("maintenance.check", "maintenance.begin",
                     "maintenance.feed", "maintenance.cancel"),),
    "model_tools": (("tool.list", "tool.invoke"),),
}

#: wire 上不代表一项能力的方法（握手、自描述）。
WIRE_INFRASTRUCTURE: frozenset[str] = frozenset(
    {"manifest.get", "schema.get", "health.get"})

_DSH_PLUGIN = Path(__file__).resolve().parent / "adapters" / "dsh" / "plugin.mjs"
_REQUEST_CALL = re.compile(r"""client\.request\(\s*['"]([a-z_]+\.[a-z_]+)['"]""")


def backing(surface: str) -> dict[str, tuple[tuple[str, ...], ...]]:
    """某条入口上每项能力由哪几组方法撑着。没列的能力 = 没接。"""
    from .schema import _CAPABILITY_BACKING

    if surface == "sdk":
        return dict(SDK_BACKING)
    if surface == "jsonl":
        return {name: _CAPABILITY_BACKING[name] for name in CAPABILITIES
                if name in _CAPABILITY_BACKING}
    if surface == "dsh":
        return dict(DSH_BACKING)
    raise ValueError(f"未知的接入面 {surface!r}；可选 {', '.join(SURFACES)}")


def available_methods(surface: str) -> frozenset[str]:
    """这条入口上**实际存在**的方法名。声明由它算，测试也拿它对账。"""
    if surface == "sdk":
        from .mounted import MountedGarden

        return frozenset(
            name for name in dir(MountedGarden)
            if not name.startswith("_") and callable(getattr(MountedGarden, name)))
    if surface == "jsonl":
        from .schema import WIRE_OPERATIONS

        return frozenset(WIRE_OPERATIONS)
    if surface == "dsh":
        return frozenset(_REQUEST_CALL.findall(_DSH_PLUGIN.read_text("utf-8")))
    raise ValueError(f"未知的接入面 {surface!r}；可选 {', '.join(SURFACES)}")


def _supported(lanes: tuple[tuple[str, ...], ...] | None,
               methods: frozenset[str]) -> bool:
    return bool(lanes) and any(all(m in methods for m in lane) for lane in lanes)


def surface_capabilities() -> dict[str, dict[str, bool]]:
    """``{"sdk": {...}, "jsonl": {...}, "dsh": {...}}``，每项能力是否接通。

    DSH 这一项另要求它用到的 wire 方法在 JSON Lines 上确实存在 —— Adapter
    调一个服务没有的方法，只会在运行时拿到 ``unknown_method``。
    """
    out: dict[str, dict[str, bool]] = {}
    wire = available_methods("jsonl")
    for surface in SURFACES:
        methods = available_methods(surface)
        if surface == "dsh":
            methods = methods & wire
        lanes: Mapping[str, tuple[tuple[str, ...], ...]] = backing(surface)
        out[surface] = {name: _supported(lanes.get(name), methods)
                        for name in CAPABILITIES}
    return out
