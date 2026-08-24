"""记忆系统的发布闸 —— 一条命令跑完所有层。

    python evals/run.py                 # 不花钱的四层，CI 每次都跑
    python evals/run.py --with-model    # 加上真模型那层，发布前跑

## 各层守什么

    ① recall.py           挑卡：卡在库里，问到了能不能召回来
    ② gate.py             落库闸：该拦的拦住，**该放的放过**
    ③ garden_language.py  语言判定：这个花园该用哪种语言
    ④ 上面三层的语料完整性（本文件自查，见 _check_corpus）
    ⑤ capture.py          落卡：真模型写出来的卡对不对 —— **花钱、慢**

## 为什么分「花钱」和「不花钱」两档

前四层进 CI，每次改动都跑，用来挡回归 —— 它们是确定性的，毫秒级。
第五层发布前跑，用来挡「模型实际行为变了」 —— 这类问题**单测一条都抓不到**，
因为单测把 agent stub 掉了，测的是「我们的代码按写的那样跑了吗」，
而不是「模型写出来的东西对不对」。

把两档混成一档，结果只会是二选一：要么 CI 变慢变贵，要么发布前那层被省掉。

## 为什么阈值不对称

召回退步容忍两个百分点的抖动；**违反禁忌、误杀真卡，一次都不容忍**。
漏召回是「没帮上忙」；把不该说的说出来、或者把用户真说过的事悄悄吃掉，是另一回事。

## 宿主还要额外跑一层

花园语言的**算法**在内核（③ 测的就是它），但**证据从哪来**是宿主的事 ——
身份卡、历史消息、客户端 locale，内核碰不到。宿主应当把自己的取证逻辑接上
③ 的语料跑一遍：

    from evals.garden_language import run
    run(decider=my_decider)     # decider(buckets, fallbacks) -> {"locale", "basis"}
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

FREE = [
    ("recall.py", "① 挑卡质量"),
    ("gate.py", "② 落库闸（拦得住 / 放得过）"),
    ("garden_language.py", "③ 花园语言判定"),
]
PAID = [("capture.py", "⑤ 落卡质量（真模型）")]


def _check_corpus() -> list[str]:
    """④ 语料自查 —— 语料本身坏了，上面几层会「全绿地」测个寂寞。

    最阴的一种失败：语料文件被清空或写坏，每一层都 0 条用例、0 条失败、退出码 0。
    看起来比任何时候都健康。
    """
    problems = []
    # 断言文件 vs 数据文件：只有「写了期望结果」的文件才要求逐条说明理由。
    # cards.jsonl / conversations.jsonl 是**素材**，它们的理由写在引用它们的查询里。
    ASSERTING = {"queries.jsonl", "gardens.jsonl", "gate.jsonl", "gate_leak.jsonl"}
    expected = {"cards.jsonl": 10, "queries.jsonl": 5, "gardens.jsonl": 10,
                "gate.jsonl": 5, "gate_leak.jsonl": 3, "conversations.jsonl": 3}
    for name, floor in expected.items():
        p = HERE / "corpus" / name
        if not p.exists():
            problems.append(f"{name} 不见了")
            continue
        rows = []
        for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                problems.append(f"{name}:{i} 不是合法 JSON（{e.msg}）")
        if len(rows) < floor:
            problems.append(f"{name} 只有 {len(rows)} 条，低于下限 {floor} —— 语料被删过？")
        # 各文件的主键名不同（id / qid / cid），取第一个存在的。
        ids = [r.get("id") or r.get("qid") or r.get("cid") for r in rows]
        if None in ids:
            problems.append(f"{name} 有条目没有主键")
        elif len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            problems.append(f"{name} 有重复主键：{dupes}")
        if name in ASSERTING:
            # 「为什么这条该这样」是断言语料里最重要的一栏：没有它，后来的人看到一条
            # 失败只能猜是代码改坏了还是语料写错了，最后多半选择改语料让它变绿。
            blank = [i for i, r in zip(ids, rows) if not str(r.get("why") or "").strip()]
            if blank:
                problems.append(f"{name} 有 {len(blank)} 条没写 why：{blank[:3]}")
    return problems


def _run(name: str, extra: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(HERE / name), *extra],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-model", action="store_true", help="加跑真模型那层（要 API key）")
    ap.add_argument("--baseline", help="挑卡 eval 的基线 JSON")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    args = ap.parse_args()

    jobs = list(FREE)
    if args.with_model:
        jobs += PAID

    failed, skipped = [], []
    for name, title in jobs:
        extra = []
        if name == "recall.py" and args.baseline:
            extra = ["--baseline", args.baseline]
        if name == "capture.py":
            extra = ["--provider", args.provider, "--model", args.model]

        print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")
        rc, out = _run(name, extra)
        print(out.rstrip())
        if rc == 0:
            pass
        elif rc == 77:  # capture.py 用 77 表示「没有 key，跳过」
            skipped.append(title)
        else:
            failed.append(title)

    print(f"\n{'=' * 64}\n④ 语料自查\n{'=' * 64}")
    problems = _check_corpus()
    for p in problems:
        print(f"  ✗ {p}")
    if not problems:
        print("  ✓ 语料完整，每条都写了「为什么」")
    else:
        failed.append("④ 语料自查")

    print(f"\n{'=' * 64}")
    if not args.with_model:
        print("⚠️  没跑真模型那层。发布前请跑 --with-model —— "
              "提示词行为的问题只有它能抓到。")
    for s in skipped:
        print(f"⚠️  {s} 被跳过（没有 API key）。这**不等于通过**。")
    if failed:
        print("🔴 未通过：" + "、".join(failed))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
