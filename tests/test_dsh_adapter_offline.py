"""DSH Adapter 的离线回归必须真正穿过 ``plugin.mjs``。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def test_adapter_capture_maintenance_and_outbox_without_a_real_model():
    node = shutil.which("node")
    if not node:
        pytest.skip("DSH Adapter 需要 Node.js；当前环境没有 node")
    memgarden = Path(sys.executable).parent / "memgarden"
    assert memgarden.is_file(), f"当前测试环境没有 memgarden CLI: {memgarden}"
    proc = subprocess.run(
        [node, str(ROOT / "adapters/dsh-memgarden/e2e/adapter_offline.mjs")],
        cwd=ROOT,
        env={**os.environ, "MEMGARDEN_BIN": str(memgarden)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"DSH Adapter 离线回归失败\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
