"""③ 语言判定 —— 一个花园该用哪种语言。

## 为什么单独占一层

**这一层是拿事故换来的。** 2026-08-24，宿主 io 的一个中文用户，花园两天内整个
翻成了英文：桶名、卡片摘要、AI 回复，全变了。他没做任何操作。

根因是判据把**字符数**当票数：

    中文桶「工作」        2 字符
    英文桶「Our relationship」 17 字符
    → 一个英文桶顶八个中文桶

于是「6 个中文桶 + 3 个英文桶」判成英文花园，AI 开始用英文写新桶，新桶又反过来
加重英文那边的字符数 —— **输出变成了下一轮的输入**，一个自我强化的循环。

这类 bug 单测抓不到，因为单测只问「函数按写的那样跑了吗」，而这个函数**就是按
写的那样跑的**。要抓它，得有一份「什么样的输入该得什么样的结论」的语料 ——
也就是这一层。

## 判据可以插

宿主的证据来源各不相同（有的有客户端 locale，有的只有历史消息）。语料只约束
**算法**：给定这些桶，结论该是什么。宿主换自己的实现进来跑同一份语料：

    from evals.garden_language import run
    run(decider=my_own_decider)     # decider(buckets, fallbacks) -> {"locale","basis"}

跑法：

    python evals/garden_language.py
"""
from __future__ import annotations

import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT / "packages" / "agent-protocol-core" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from memgarden.contract import run_garden_language_contract  # noqa: E402
from memgarden.garden_language import decide_garden_language  # noqa: E402

def _default_decider(evidence: dict) -> dict:
    return decide_garden_language(
        explicit=evidence.get("explicit"),
        written=evidence.get("written") or "",
        locale=evidence.get("locale"),
    )


def run(decider=_default_decider) -> int:
    """语料**来自包内**（memgarden.contract），不是这个目录 —— 宿主装了包就能跑
    同一份，不必 clone 内核仓库。见 contract/__init__.py 里的理由。"""
    _, fails = run_garden_language_contract(decider)
    if fails:
        print(f"  失败：{', '.join(fails)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
