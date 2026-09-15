"""同步守卫：从宿主同步内核时，别把宿主的私有内容一起带进来。

**这条是踩出来的。** 2026-08-20 从 io 同步三个模块，把一条注释里的真实用户 id
（`usr_...`）又带了回来 —— 那条 id 在上一轮已经清过一次，同步直接覆盖了修复，
而且推到 GitHub 之后才发现。

同步是覆盖式的，所以「清理过一次」不等于「以后都干净」。这条测试就是那个以后。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 宿主私有、不该出现在通用库里的东西。
BANNED = {
    r"usr_[0-9a-f]{8,}": "真实用户 id",
    r"_memory_inner_from_action": "io 的内部函数名",
    r"io_cli": "io 的内部工具名",
    r"\bADMIN_KEY\b": "凭据名",
    # 私下评审文档的章节号（读者拿不到那份文档），以及宿主私有仓库里的文件路径。
    r"(?i)seven(?:floor)?\s*(?:\d{4}-\d{2}-\d{2}\s*)?§": "私有评审文档引用",
    r"backend/memory_bm25\.py": "宿主私有仓库路径",
    r"_MEMORY_CARDS_LIMIT|_DREAM_CARDS_MAX_CHARS": "宿主内部常量名",
}


def _sources():
    for p in (ROOT / "src").rglob("*.py"):
        yield p
    for name in ("README.md",):
        f = ROOT / name
        if f.exists():
            yield f
    for p in (ROOT / "examples").rglob("*.py"):
        yield p


def test_no_host_private_content_in_the_package():
    hits = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for pattern, what in BANNED.items():
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{path.relative_to(ROOT)}:{line} {what} -> {m.group(0)}")
    assert not hits, "宿主私有内容漏进包里了：\n" + "\n".join(hits)
