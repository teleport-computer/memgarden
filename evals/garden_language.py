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

from memgarden.garden_language import decide_garden_language  # noqa: E402

CORPUS = pathlib.Path(__file__).resolve().parent / "corpus" / "gardens.jsonl"


def _basis_ok(got: str | None, want: str | None) -> bool:
    """依据对不对。

    ⚠️ `fallback_N` 里的 **N 不做严格比对**。语料里的槽位序号是内核自己的排法；
    宿主的证据链完全可能是另一套顺序（io 就是 identity → locale → 归档语言）。
    强行比对序号，测的就变成"你的取证顺序跟内核一样吗"—— 那不是这条 eval 该管的事，
    而且会逼宿主写一层假的序号映射来凑绿。

    真正要守住的是**档位**：这次结论是从桶来的、从兜底信号来的、还是纯默认值。
    尤其是 `existing_buckets` 那档必须精确对上 —— 事故就出在那一档。
    """
    if not want:
        return True
    if want.startswith("fallback"):
        return bool(got and got.startswith("fallback"))
    return got == want


def _default_decider(buckets, fallbacks):
    return decide_garden_language(buckets, fallbacks=fallbacks)


def run(decider=_default_decider, *, corpus: pathlib.Path = CORPUS) -> int:
    cases = [json.loads(l) for l in corpus.read_text("utf-8").splitlines() if l.strip()]
    fails, incidents = [], 0

    for c in cases:
        got = decider(c.get("buckets", ""), tuple(c.get("fallbacks") or ()))
        locale_ok = got.get("locale") == c["expect"]
        basis_ok = _basis_ok(got.get("basis"), c.get("expect_basis"))
        tag = "  ⚠事故" if c.get("incident") else ""

        if locale_ok and basis_ok:
            print(f"  ✓ {c['id']:38} → {got.get('locale'):8}{tag}")
        else:
            print(f"  ✗ {c['id']:38} → {got.get('locale')!r} (期望 {c['expect']!r})"
                  f" 依据={got.get('basis')!r}{tag}")
            print(f"      语料写的理由：{c['why']}")
            fails.append(c["id"])
            if c.get("incident"):
                incidents += 1

    print(f"\n  {len(cases) - len(fails)}/{len(cases)} 通过")
    if incidents:
        print(f"  🔴 其中 {incidents} 条是**曾经真的发生过的事故**，回归了。")
    if fails:
        print(f"  失败：{', '.join(fails)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
