"""``memgarden`` 命令行 —— 薄壳，不复制任何判断逻辑。

## 它存在的理由

装完一个 Python 包之后，**能不能敲一条命令看到效果**，决定了别人愿不愿意继续读
下去。在这之前 ``pyproject.toml`` 里连 ``[project.scripts]`` 都没有 ——
`pip install memgarden` 之后什么命令都没有。

## 硬约束：只调 GardenComponent，不碰内部模块

CLI 和 MCP 外壳都必须走同一个顶层接口。一旦外壳自己拼提示词、自己解析，
就会出现「CLI 的行为和 SDK 的行为不一样」——而且是悄悄地不一样。
有测试守着这一条。

## 模型从哪来

CLI 不持有任何 key。``--model-cmd`` 指定一条外部命令，提示词从 stdin 进去、
回复从 stdout 出来：

    memgarden capture --window-file chat.txt --locale zh-Hans \\
        --model-cmd "llm -m gpt-4o"

这样接谁都行（llm / ollama / 自己的脚本），而且 key 留在那条命令自己的环境里。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from dataclasses import asdict

from ..component import GardenComponent
from ..contracts import CaptureRequest, ContextRequest, MaintenanceRequest


class SubprocessModel:
    """把提示词交给一条外部命令，拿它的 stdout 当回复。

    走 stdin 而不是命令行参数是必须的：提示词有几千字，还含换行和引号，
    塞进 argv 会被 shell 截断或改写。
    """

    def __init__(self, command: str) -> None:
        self.command = command

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        proc = subprocess.run(
            self.command, shell=True, input=prompt,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"model command failed ({proc.returncode}): {proc.stderr[:400]}"
            )
        return proc.stdout


def _read(path: str | None, inline: str | None) -> str:
    if inline is not None:
        return inline
    if path in (None, "-"):
        return sys.stdin.read()
    return pathlib.Path(path).read_text("utf-8")


def _load_cards(path: str | None) -> list[dict]:
    if not path:
        return []
    raw = pathlib.Path(path).read_text("utf-8")
    if path.endswith(".jsonl"):
        return [json.loads(l) for l in raw.splitlines() if l.strip()]
    data = json.loads(raw)
    return data if isinstance(data, list) else data.get("cards", [])


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------- #

def _cmd_capture(args) -> int:
    garden = GardenComponent(model=SubprocessModel(args.model_cmd))
    result = garden.capture(CaptureRequest(
        window=_read(args.window_file, args.window),
        locale=args.locale,
        buckets=args.buckets or "",
        threads=args.threads or "",
        identity=args.identity or "",
        ai_name=args.ai_name or "",
        user_name=args.user_name or "",
        policy=args.policy,
    ))
    _emit(asdict(result))
    # 「没什么可记」是退出码 0，「解析失败」是 1 —— 脚本要能分辨这两件事，
    # 否则流水线会把失败当成正常，然后推进游标。
    return 1 if result.error else 0


def _cmd_recall(args) -> int:
    from ..selection import Chain, RecentStage, RelevanceStage

    garden = GardenComponent(
        model=SubprocessModel(args.model_cmd or "cat"),
        selection_policy=Chain(stages=(
            RelevanceStage(limit=args.limit, any_score=True),
            RecentStage(limit=2, order_by="created_at"),
        )),
    )
    result = garden.build_context(ContextRequest(
        query=args.query,
        candidates=_load_cards(args.cards),
        limit=args.limit,
    ))
    _emit(asdict(result))
    return 0


def _cmd_maintain(args) -> int:
    garden = GardenComponent(model=SubprocessModel(args.model_cmd or "cat"))
    result = garden.run_maintenance(MaintenanceRequest(
        cards=_load_cards(args.cards),
        locale=args.locale,
        dry_run=args.dry_run,
        last_signature=args.last_signature or "",
        last_seed_card_count=args.last_seed_card_count,
    ))
    _emit(asdict(result))
    return 1 if result.error else 0


def _cmd_manifest(args) -> int:
    """这个组件会做什么 —— 机器可读，给接入方和 CI 用。"""
    from .. import __name__ as pkg  # noqa: F401
    from importlib.metadata import version

    garden = GardenComponent(model=SubprocessModel("cat"))
    try:
        pkg_version = version("memgarden")
    except Exception:
        pkg_version = "unknown"
    _emit({
        "component_id": "memgarden",
        "component_version": pkg_version,
        "capabilities": garden.capabilities().as_dict(),
        "tools": [asdict(t) for t in garden.tools()],
    })
    return 0


def _cmd_tools(args) -> int:
    garden = GardenComponent(model=SubprocessModel("cat"))
    _emit([asdict(t) for t in garden.tools()])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memgarden",
        description="Memory Garden —— AI 记忆的编辑判断力。CLI 只调顶层接口，"
                    "不复制任何判断逻辑。",
    )
    sub = p.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="这段对话里有什么值得记")
    cap.add_argument("--window-file", help="对话文本文件，'-' 表示 stdin")
    cap.add_argument("--window", help="直接给一段对话文本")
    cap.add_argument("--locale", required=True,
                     help="花园语言，如 zh-Hans / en。**必填** —— 默认成某种语言"
                          "等于把一套分类法硬塞给使用者")
    cap.add_argument("--model-cmd", required=True,
                     help="外部模型命令，提示词走 stdin、回复走 stdout")
    cap.add_argument("--buckets", help="已有桶名，一行")
    cap.add_argument("--threads", help="已有线索，一行")
    cap.add_argument("--identity", help="身份卡正文")
    cap.add_argument("--ai-name")
    cap.add_argument("--user-name")
    cap.add_argument("--policy", help="哪把「什么值得记」的尺子")
    cap.set_defaults(func=_cmd_capture)

    rec = sub.add_parser("recall", help="这一轮该想起哪几张")
    rec.add_argument("--query", required=True)
    rec.add_argument("--cards", required=True, help="候选卡片 .json / .jsonl")
    rec.add_argument("--limit", type=int, default=8)
    rec.add_argument("--model-cmd")
    rec.set_defaults(func=_cmd_recall)

    mnt = sub.add_parser("maintain", help="该不该整理；要整理的话怎么合")
    mnt.add_argument("--cards", required=True)
    mnt.add_argument("--locale", default="zh-Hans")
    mnt.add_argument("--model-cmd")
    mnt.add_argument("--dry-run", action="store_true",
                     help="只回答「该整理了吗」，不烧模型调用")
    mnt.add_argument("--last-signature", help="上次整理的签名（宿主持有的账本）")
    mnt.add_argument("--last-seed-card-count", type=int, default=0)
    mnt.set_defaults(func=_cmd_maintain)

    man = sub.add_parser("manifest", help="这个组件会做什么（机器可读）")
    man.set_defaults(func=_cmd_manifest)

    tls = sub.add_parser("tools", help="给模型的工具定义")
    tls.set_defaults(func=_cmd_tools)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
