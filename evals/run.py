"""记忆系统的发布闸 —— 一条命令跑完三层。

    python evals/run.py                    # 只跑不花钱的两层
    python evals/run.py --with-model       # 加上真模型那层（发布前必跑）

## 三层各守什么

    recall.py    挑卡质量。确定性、毫秒级、可进 CI。
                 守的是「卡在库里但召不回」这一类 —— 半年里最贵的两次事故都是它。

    capture.py   落卡质量。**调真模型、花钱、慢。**
                 守的是提示词行为 —— 单测一个都抓不到，因为单测把 agent stub 掉了。

⚠️ 还有第三条住在宿主 io 的仓库里：**花园语言判定**（evals/language.py）。
   它要读身份卡、历史记忆、客户端 locale —— 那些是宿主的数据，内核碰不到，
   所以逻辑和 eval 都跟着 io 走。发布记忆相关改动时那条也要跑。

## 为什么要分层

前两层进 CI，每次改动都跑，用来挡回归；第三层发布前跑，用来挡「模型实际行为变了」。
把它们混成一层，要么 CI 变慢变贵，要么发布前那层被省掉。

## 为什么阈值不对称

召回退步容忍两个百分点的抖动；**违反禁忌一次都不容忍**。
漏召回是"没帮上忙"，把不该说的说出来是另一回事。
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _run(name: str, args: list[str]) -> tuple[str, int, str]:
    r = subprocess.run([sys.executable, str(HERE / name), *args],
                       capture_output=True, text=True)
    return name, r.returncode, r.stdout + r.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-model", action="store_true", help="加跑真模型那层（要 API key）")
    ap.add_argument("--baseline", help="挑卡 eval 的基线 JSON")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    args = ap.parse_args()

    jobs = [("recall.py", ["--baseline", args.baseline] if args.baseline else [])]
    if args.with_model:
        jobs.append(("capture.py", ["--provider", args.provider, "--model", args.model]))

    failed = []
    for name, extra in jobs:
        title = {"recall.py": "① 挑卡质量",
                 "capture.py": "② 落卡质量（真模型）"}[name]
        print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")
        _, code, out = _run(name, extra)
        print(out.rstrip())
        if code:
            failed.append(title)

    print(f"\n{'=' * 62}")
    if failed:
        print("❌ 未通过：" + "、".join(failed))
        print("   发布闸不放行。")
        return 1
    print("✅ 全部通过")
    print("   注：花园语言判定那条在 io 仓库（evals/language.py），需单独跑。")
    if not args.with_model:
        print("   ⚠️ 没跑真模型那层 —— 提示词行为未验。发布前请加 --with-model。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
