"""十分钟把 Memory Garden 挂进一个陌生的 Agent Runtime。

这个例子**不 import 任何内部模块** —— 只用 ``from memgarden import ...`` 顶层那几个。
这是刻意的：接入方需要认识的东西越少，Garden 内部越能自由改。

跑：

    python examples/mount_in_ten_minutes.py          # 假模型，不花钱，不联网

真接的时候只换两样：``EchoModel`` 换成你真的调模型，``DictStore`` 换成你的库。
"""
from __future__ import annotations

import json

from memgarden import (
    CaptureRequest,
    ContextRequest,
    GardenComponent,
    MaintenanceRequest,
)
from memgarden.selection import Chain, RecentStage, RoleStage


# --------------------------------------------------------------------------- #
# 你要提供的两样东西
# --------------------------------------------------------------------------- #

class EchoModel:
    """① 模型。**你的 API key 在你手里，Garden 拿不到。**

    真实实现里这就是一次 provider 调用。``purpose`` 告诉你这次是干嘛的
    （capture / dream），想给不同用途分不同模型就在这儿分流。
    """

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        print(f"    [模型被调用：{purpose}，提示词 {len(prompt)} 字]")
        if purpose == "dream":
            return json.dumps({"consolidations": []})
        return json.dumps({"cards": [{
            "action": "add",
            "bucket": "偏好与边界",
            "threads": ["饮食"],
            "summary": "他不吃辣",
            "content": "一吃辣就胃疼，所以点菜会避开辣的。",
            "importance": 0.6,
            "pulse": 0.3,
        }]}, ensure_ascii=False)


class DictStore:
    """② 存储。这里用字典演示；真实里是你的 Postgres / SQLite / 别的什么。

    **Garden 不碰它。** 它只告诉你「该这么改」，落库、加密、权限都是你的事。
    """

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self._n = 0

    def apply(self, mutations: list[dict]) -> list[str]:
        written = []
        for m in mutations:
            if m["op"] == "add":
                self._n += 1
                cid = f"m_{self._n}"
                self.cards[cid] = {"id": cid, **m["card"]}
                written.append(cid)
            elif m["op"] == "supersede":
                old = m.get("target_id", "")
                self._n += 1
                cid = f"m_{self._n}"
                self.cards[cid] = {"id": cid, **m["card"]}
                if old in self.cards:
                    self.cards[old]["superseded_by"] = cid
                written.append(cid)
        return written

    def all(self) -> list[dict]:
        return [c for c in self.cards.values() if not c.get("superseded_by")]


# --------------------------------------------------------------------------- #
# 挂载
# --------------------------------------------------------------------------- #

def main() -> None:
    store = DictStore()

    garden = GardenComponent(
        model=EchoModel(),
        # 挑卡策略是可换的插口。不传就没有 turn_context 能力 —— 会在
        # capabilities() 里如实声明，而不是假装能挑然后返回空。
        selection_policy=Chain(stages=(
            RoleStage("turning_point", limit=2, order_by="occurred_at"),
            RecentStage(limit=3, order_by="created_at"),
        )),
    )

    print("这个组件会做什么：")
    for k, v in garden.capabilities().as_dict().items():
        print(f"    {k:16} {v}")

    # ---- ① 落卡：这段对话里有什么值得记 ------------------------------- #
    print("\n① 落卡")
    result = garden.capture(CaptureRequest(
        window="用户：我不吃辣，一吃就胃疼\n我：那以后点菜避开",
        # locale 必填，没有默认值 —— 默认成某种语言等于把一套分类法
        # 硬塞给使用者，而他不会知道自己的库里为什么长出了中文桶。
        locale="zh-Hans",
        ai_name="io",
        user_name="老王",
    ))
    print(f"    产出 {len(result.mutations)} 条改动指令，重问 {result.retried} 次")
    print(f"    观测：{result.trace}")

    # Garden 不写库 —— 这一步是你的。
    ids = store.apply(result.mutations)
    print(f"    落库（这一步在你这边）：{ids}")

    # ---- ② 想起来：这一轮该带哪几张记忆 ------------------------------- #
    print("\n② 想起来")
    ctx = garden.build_context(ContextRequest(
        query="晚饭吃什么",
        # 候选由你给：生命周期过滤、权限过滤都在你那边做完。
        # Garden 不认识你的 is_archived，也没资格替你判断谁能看什么。
        candidates=store.all(),
        limit=3,
    ))
    print(f"    挑中 {ctx.record_ids}")
    for b in ctx.blocks:
        print(f"    [{b['stage']}] {b['text']}")

    # ---- ③ 整理：该不该合并一遍 --------------------------------------- #
    print("\n③ 整理")
    check = garden.run_maintenance(MaintenanceRequest(
        cards=store.all(), locale="zh-Hans",
        dry_run=True,      # 只问「该整理了吗」，不烧模型调用
    ))
    print(f"    该整理了吗：{check.needed}（{check.trace['reason']}）")
    print("    ↑ 调度什么时候跑、失败怎么退避，是你的事；Garden 只回答该不该")

    # ---- ④ 给模型的工具 ------------------------------------------------ #
    print("\n④ 给模型的工具")
    for tool in garden.tools():
        print(f"    {tool.name}: {tool.description}")

    print("\n完。全程只 import 了 memgarden 顶层的几个名字，")
    print("没有碰 prompts / scoring / selection 里面任何一个函数。")


if __name__ == "__main__":
    main()
