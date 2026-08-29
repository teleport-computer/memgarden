"""一个记得住事的命令行 agent。

**跟 Garden 打交道的代码不到 30 行**，全在 `Memory` 这个类里 ——
其余都是这个 agent 自己的事（怎么聊天、怎么存、怎么调模型）。

    python agent.py           # 需要 OPENROUTER_API_KEY 或 DEEPSEEK_API_KEY
    python agent.py --fake    # 不用 key，走假模型
"""
from __future__ import annotations

import argparse
import sys

# ⚠️ 只 import 顶层。整个文件不碰 memgarden.prompts / scoring / selection.*
#    唯一的例外是 selection 里的挑卡段 —— 那是公开插口，换挑卡策略本来就要用。
from memgarden import (
    CaptureRequest,
    ContextRequest,
    GardenComponent,
    MaintenanceRequest,
)
from memgarden.selection import Chain, RecentStage, RelevanceStage

from model import FakeModel, HttpModel
from store import JsonStore

LOCALE = "zh-Hans"      # 这个 demo 说中文。真实项目按用户来。
AI_NAME = "io"
USER_NAME = "你"


class Memory:
    """这个 agent 的记忆层。**全部和 Garden 的交互都在这里。**"""

    def __init__(self, model, store: JsonStore) -> None:
        self.store = store
        self.garden = GardenComponent(
            model=model,                       # 模型你给，key 不给它
            selection_policy=Chain(stages=(    # 挑卡策略可换
                RelevanceStage(limit=3, any_score=True),
                RecentStage(limit=2, order_by="created_at"),
            )),
        )

    def remember(self, window: str) -> list[str]:
        """这段对话里有什么值得记 → 落库。"""
        result = self.garden.capture(CaptureRequest(
            window=window, locale=LOCALE, ai_name=AI_NAME, user_name=USER_NAME,
            buckets="、".join(sorted({c.get("bucket", "") for c in self.store.active()} - {""})),
        ))
        if result.error:
            # 「解析失败」不能当成「没什么可记」—— 后者会让这段对话
            # 永远不再被看一眼。这里选择明说，让用户知道。
            print(f"    [记忆没落成：{result.error}]", file=sys.stderr)
            return []
        return self.store.apply(result.mutations)

    def recall(self, query: str) -> list[str]:
        """这一轮该想起哪几张。"""
        ctx = self.garden.build_context(ContextRequest(
            query=query,
            candidates=self.store.active(),   # 生命周期过滤是我这层的事
            limit=4,
        ))
        return [b["text"] for b in ctx.blocks if b["text"]]

    def tidy(self) -> str:
        """该整理就整理一遍。"""
        check = self.garden.run_maintenance(MaintenanceRequest(
            cards=self.store.active(), all_cards=self.store.all(),
            locale=LOCALE, dry_run=True,
            last_signature=self.store.ledger["signature"],
            last_seed_card_count=self.store.ledger["seed_card_count"],
        ))
        if not check.needed:
            return f"暂时不用整理（{check.trace['reason']}）"
        done = self.garden.run_maintenance(MaintenanceRequest(
            cards=self.store.active(), all_cards=self.store.all(),
            locale=LOCALE, ai_name=AI_NAME, user_name=USER_NAME,
        ))
        if done.error:
            return f"整理失败：{done.error}"
        self.store.apply(done.mutations)
        self.store.remember_dream(done.trace["signature"], done.trace["seed_card_count"])
        return f"整理完成，合并了 {len(done.mutations)} 处"


# --------------------------------------------------------------------------- #
# 下面全是这个 agent 自己的事，和 Garden 无关
# --------------------------------------------------------------------------- #

BANNER = """\
记得住事的小 agent。跟它说话，它会记住。
  /memory   看看它记了什么
  /tidy     让它整理一遍
  /quit     退出
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="不用 API key，走假模型")
    ap.add_argument("--store", default="memory.json")
    args = ap.parse_args()

    model = FakeModel() if args.fake else HttpModel()
    store = JsonStore(args.store)
    memory = Memory(model, store)

    print(BANNER)
    if store.active():
        print(f"（已经记得 {len(store.active())} 件事）\n")

    history: list[dict] = []
    turns_since_capture = 0

    while True:
        try:
            said = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not said:
            continue
        if said == "/quit":
            break
        if said == "/memory":
            cards = store.active()
            if not cards:
                print("    （还什么都不记得）\n")
                continue
            by_bucket: dict[str, list[dict]] = {}
            for c in cards:
                by_bucket.setdefault(c.get("bucket") or "未分类", []).append(c)
            for bucket, items in by_bucket.items():
                print(f"    【{bucket}】")
                for c in items:
                    print(f"      · {c.get('summary','')}")
            print()
            continue
        if said == "/tidy":
            print(f"    {memory.tidy()}\n")
            continue

        # ① 想起来：这一轮该带哪几张记忆
        recalled = memory.recall(said)
        if recalled:
            print(f"    [想起：{'；'.join(recalled)}]")

        # ② 回话
        system = "你是一个朋友般的助手，说话简短自然。"
        if recalled:
            system += "\n你记得关于对方的这些事：\n" + "\n".join(f"- {r}" for r in recalled)
        history.append({"role": "user", "content": said})
        reply = model.chat([{"role": "system", "content": system}, *history[-10:]])
        history.append({"role": "assistant", "content": reply})
        print(f"{AI_NAME} > {reply}\n")

        # ③ 攒够几轮就落一次卡
        turns_since_capture += 1
        if turns_since_capture >= 2:
            window = "\n".join(
                f"{USER_NAME}：{m['content']}" if m["role"] == "user"
                else f"{AI_NAME}：{m['content']}"
                for m in history[-4:]
            )
            written = memory.remember(window)
            if written:
                print(f"    [记住了 {len(written)} 件事]\n")
            turns_since_capture = 0

    print("再见。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
