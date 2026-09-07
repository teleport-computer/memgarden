"""DeepSeek Harness Adapter —— 文件随包发布，用 ``memgarden install-dsh`` 装。"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 插件本体。DSH 通过 profile 的 node_modules 加载它。
PLUGIN = HERE / "plugin.mjs"
PACKAGE_JSON = HERE / "package.json"
