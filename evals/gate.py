"""④ 落库闸 —— 该拦的拦住，该放的放过。

## 为什么它值一层 eval

写卡之前有一道体检：摘要和正文都得是**真内容**（不是占位符、不是省略号、不是
模型协议标记漏出来的碎片）。这道闸有两个方向的失败，代价完全不同：

    拦松了   垃圾卡进库 —— 用户在记忆列表里看到「[摘要]」「...」
    拦紧了   **真记忆被吃掉** —— 用户跟 AI 说的事,凭空没了,而且不报错

第二种是静默的，用户只会觉得「AI 记性不好」，永远不会报给你。所以这份语料里
**「该放过」的条目和「该拦住」的一样多**，其中 `leak_one_weak_signal_passes`
专门守「别过度拦截」这一侧。

## 泄漏信号是宿主给的

内核只定「几条证据算数」这条政策（强证据一条即打回，弱证据要两条共现）；
「哪些字符串算证据」由宿主注入 —— 不同宿主接的模型不同，漏出来的标记也不同。
所以语料里每条自带 `signals`，测的是**政策**，不是某个宿主的词表。

跑法：

    python evals/gate.py
"""
from __future__ import annotations

import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT / "packages" / "agent-protocol-core" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from memgarden.text.card_text import card_text_rejection  # noqa: E402
from memgarden.text.leak_signals import GENERIC_SIGNALS, LeakSignals  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def _substring_detector(needle: str):
    """语料里的信号写成字符串，这里包成识别器。

    真实宿主的识别器多半是正则或结构判断，不是子串 —— 但这一层测的是**权衡政策**
    （几条弱证据才算数、硬软字段松紧是否有别），不是某个宿主的词表精度。用最笨的
    子串当替身，正好把政策单独隔出来测。
    """
    tag = needle.strip().strip("<|>").replace(" ", "_")[:24] or "signal"

    def detect(text: str) -> str | None:
        return tag if needle in (text or "") else None

    return detect


def _signals(spec) -> LeakSignals:
    if not spec:
        return GENERIC_SIGNALS
    return LeakSignals(
        strong=tuple(_substring_detector(s) for s in (spec.get("strong") or ())),
        weak=tuple(_substring_detector(s) for s in (spec.get("weak") or ())),
    )


def run() -> int:
    cases = []
    for name in ("gate.jsonl", "gate_leak.jsonl"):
        p = HERE / "corpus" / name
        cases += [json.loads(l) for l in p.read_text("utf-8").splitlines() if l.strip()]

    fails, ate_real = [], []
    for c in cases:
        got = card_text_rejection(summary=c["summary"], content=c["content"],
                                  signals=_signals(c.get("signals")))
        want_reject = c["verdict"] == "reject"

        if want_reject and got is None:
            print(f"  ✗ {c['id']:38} 该拦没拦 —— 垃圾卡会进库")
            fails.append(c["id"])
        elif not want_reject and got is not None:
            print(f"  ✗ {c['id']:38} 🔴 **误杀真卡** ({got})")
            fails.append(c["id"])
            ate_real.append(c["id"])
        elif want_reject and (pre := c.get("reason_prefix")) and not got.startswith(pre):
            print(f"  ✗ {c['id']:38} 拦对了但归因错：{got}（期望 {pre}*）")
            fails.append(c["id"])
        else:
            mark = "拦" if want_reject else "放"
            print(f"  ✓ {c['id']:38} {mark}" + (f"  ({got})" if got else ""))
        if c["id"] in fails[-1:]:
            print(f"      语料写的理由：{c['why']}")

    print(f"\n  {len(cases) - len(fails)}/{len(cases)} 通过")
    if ate_real:
        print(f"  🔴 有 {len(ate_real)} 条**真卡被闸门吃掉**了 —— 这是用户看不见的那种坏，"
              f"比放垃圾卡进去严重：{', '.join(ate_real)}")
    if fails:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
