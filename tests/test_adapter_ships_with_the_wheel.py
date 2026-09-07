"""Adapter 必须随 Python 包发布，且版本和包一致。

## 为什么要有这条测试

Adapter 和这个包共用同一套 wire 协议。哪天有人把 `.mjs` 从 `artifacts`
里漏掉、或者只改了 `pyproject.toml` 的版本而忘了 Adapter 的 `package.json`，
后果都不是构建失败 —— 是用户装到一个**版本对不上的组合**，
而症状是运行时某一类记忆悄悄记不进去。

所以要在这里当场炸出来。
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from memgarden.adapters.dsh import PACKAGE_JSON, PLUGIN

ROOT = Path(__file__).resolve().parent.parent


def test_the_plugin_file_is_importable_from_the_installed_package():
    """装完包就能拿到插件文件 —— 不需要 clone 仓库。"""
    assert PLUGIN.is_file(), f"插件文件不在包里: {PLUGIN}"
    assert PLUGIN.read_text("utf-8").lstrip().startswith("/**")
    assert "agent/pre-step" in PLUGIN.read_text("utf-8")


def test_the_adapter_version_matches_the_python_package():
    """两个版本号必须一致。不一致就是「同一份代码报了两个版本」。"""
    declared = tomllib.loads(
        (ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    adapter = json.loads(PACKAGE_JSON.read_text("utf-8"))["version"]
    assert adapter == declared, (
        f"Adapter 说 {adapter}，Python 包说 {declared} —— "
        "它们共用同一套 wire 协议，版本必须同步")


def test_the_wheel_build_config_actually_includes_the_adapter():
    """构建配置里要真的带上 .mjs，否则 wheel 里只有 .py。

    hatchling 默认只收 Python 文件；漏了这一行的话，包能装、
    `memgarden install-dsh` 会在拷文件那一步才报「找不到」。
    """
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    artifacts = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    assert any(a.endswith(".mjs") for a in artifacts), artifacts


def test_there_is_no_stray_npm_package_left_behind():
    """旧的 npm 包定义要删干净。

    留着的话，别人会照那个 `package.json` 去 `npm publish`，
    于是又变回「两个版本号」那个我们刚废掉的形态。
    """
    assert not (ROOT / "adapters" / "dsh-memgarden" / "package.json").exists()
    assert not (ROOT / "adapters" / "dsh-memgarden" / "install.mjs").exists()
