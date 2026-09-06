"""tag / pyproject / uv.lock 说的版本号必须是同一个。

对不上的后果不是「不好看」：发出去的 wheel 标着 0.16.0，而锁文件说 0.15.0，
事后按锁文件复现问题时，装到的是另一份代码 —— 查了半天的东西根本不是线上跑的。

在 CI 的发版闸上跑；本地也可以直接 `python scripts/check_version_consistency.py`。
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return str(data["project"]["version"])


def _lock() -> str | None:
    lock = ROOT / "uv.lock"
    if not lock.exists():
        return None
    text = lock.read_text("utf-8")
    m = re.search(r'name\s*=\s*"memgarden"\s*\nversion\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def main() -> int:
    declared = _pyproject()
    problems: list[str] = []

    locked = _lock()
    if locked is None:
        problems.append("uv.lock 里找不到 memgarden 这一项")
    elif locked != declared:
        problems.append(
            f"uv.lock 说 {locked}，pyproject 说 {declared} —— "
            "跑一次 `uv lock` 并把锁文件一起提交")

    # tag 只在发版流水线上有；本地跑时没有这个环境变量，跳过即可。
    ref = os.environ.get("GITHUB_REF_NAME") or ""
    tag = ref[1:] if ref.startswith("v") else ref
    if tag and re.fullmatch(r"\d+\.\d+\.\d+", tag) and tag != declared:
        problems.append(f"tag 是 v{tag}，pyproject 说 {declared}")

    if problems:
        print("版本号对不上：")
        for p in problems:
            print("  ·", p)
        return 1
    print(f"版本号一致：{declared}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
