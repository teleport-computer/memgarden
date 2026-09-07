"""要求真实模型证据时，缺少凭据不能得到成功验收结论。"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("args, exit_code, marker", [
    (["evals/capture.py"], 0, "SKIP:"),
    (["evals/capture.py", "--require-key"], 1, "ERROR:"),
    (["evals/run.py", "--with-model"], 1, "未通过"),
])
def test_missing_model_key_has_truthful_exit_status(args, exit_code, marker):
    environment = {**os.environ, "DEEPSEEK_API_KEY": ""}
    result = subprocess.run(
        [sys.executable, *args],
        cwd=Path(__file__).resolve().parents[1],
        env=environment, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert marker in result.stdout
    assert "✅ 全部通过" not in result.stdout
