"""十分钟把 Memory Garden 挂到一个陌生 Runtime 上。

真接的时候只换一样：``EchoModel`` 换成你真的调模型。存储用官方的
``SqliteStore``（一个文件），不需要你自己写。

    python examples/mount_in_ten_minutes.py

## 这个示例证明什么

不是「每个 API 都能调通」，而是**整条链路真的闭合**：

    落卡 → 落库 → 想起来 → 模型自己搜 → 模型自己写 → 整理 → 换个进程还在

以前这个示例用一个手写的 `DictStore`，并且每一步的落库都由示例自己做 ——
那证明的是「零件都在」，不是「插件能用」。接入方照着抄，就得把 tenant、
权限、幂等、CAS、整理的写回全部自己实现一遍，而这些写错了不会报错，
只会悄悄丢记忆。

## 你需要提供的只有两样

    ModelPort     一个 `complete(prompt, *, purpose) -> str`
    Scope         这次调用的可信作用域：租户是谁、agent 是谁、能碰哪些 mount
                  🔴 它必须来自你的可信上下文，**绝不能来自模型的工具参数**
"""
from __future__ import annotations

import json
import pathlib
import tempfile

from memgarden import (
    Actor,
    CaptureRequest,
    MaintenanceRequest,
    MountedGarden,
    Scope,
    SqliteStore,
    ToolCall,
)
# 挑卡策略是可换的插口，所以它的零件在自己的模块里 —— 这是唯一需要往下取的东西。
from memgarden.selection import Chain, RecentStage, RoleStage


class EchoModel:
    """假模型：按 purpose 返回预设 JSON。**你要换掉的就是这一个类。**

    真接的时候这里是你的 provider 调用 —— API key 归你，Garden 不碰。
    """

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        if purpose == "dream":
            return json.dumps({"consolidations": [{
                "op": "merge",
                "card_ids": ["m_1", "m_2"],
                "rationale": "这两条讲的是同一件事，合起来更完整。",
                "result": {"bucket": "偏好与边界", "threads": ["饮食"],
                           "summary": "不吃辣，点菜要避开",
                           "content": "对方不吃辣，一吃就胃疼，点菜需避开辣味。"},
            }]}, ensure_ascii=False)
        return json.dumps({"cards": [{
            "action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
            "summary": "不吃辣，一吃就胃疼",
            "content": "对方不吃辣，一吃就胃疼，点菜需要避开辣味。",
        }]}, ensure_ascii=False)


def _garden(db_path: str) -> MountedGarden:
    return MountedGarden(
        model=EchoModel(),
        store=SqliteStore(db_path),
        # 挑卡策略是可换的插口。不传就没有 turn_context 能力 ——
        # capabilities() 会如实声明，而不是假装能挑然后返回空。
        selection_policy=Chain(stages=(
            RoleStage("turning_point", limit=2, order_by="occurred_at"),
            RecentStage(limit=5, order_by="created_at"),
        )),
        min_new_cards_for_maintenance=1,
    )


def main() -> None:
    db = str(pathlib.Path(tempfile.mkdtemp()) / "garden.db")
    garden = _garden(db)

    # 🔴 作用域来自你的可信上下文，不来自模型
    me = Scope(
        tenant_id="user-42",
        actor=Actor(user_id="user-42", agent_id="assistant-1"),
        allowed_mounts=("agent-private",),
    )

    print("这个组件会做什么：")
    for k, v in garden.component.capabilities().as_dict().items():
        print(f"    {k:16} {v}")

    # ---- ① 落卡并落库 ------------------------------------------------- #
    print("\n① 落卡（判断 + 写库一步完成）")
    receipt = garden.capture_and_store(me, CaptureRequest(
        window="用户：我不吃辣，一吃就胃疼\n我：那以后点菜避开",
        # locale 必填，没有默认值 —— 默认成某种语言等于把一套分类法硬塞给
        # 使用者，而他不会知道自己的库里为什么长出了中文桶。
        locale="zh-Hans",
        ai_name="io",
        user_name="老王",
        # 幂等键由你给：你知道「同一个 turn」这个业务边界，Garden 不知道。
        idempotency_key="turn-1",
    ))
    print(f"    写进去了：{receipt.written}，记录 {receipt.record_ids}")

    # 重放同一个 turn —— 不会写第二遍。
    # ⚠️ 看回执的 record_ids 判断不了：幂等命中时它返回的是**上一次的结果**，
    #    看起来一样有 id。要看库里到底有几张。
    before = len(SqliteStore(db).load(me.tenant_id).cards)
    garden.capture_and_store(me, CaptureRequest(
        window="用户：我不吃辣，一吃就胃疼\n我：那以后点菜避开",
        locale="zh-Hans", idempotency_key="turn-1"))
    after = len(SqliteStore(db).load(me.tenant_id).cards)
    print(f"    重放同一个 turn：库里 {before} 张 → {after} 张（幂等，应当不变）")

    # ---- ② 想起来（候选由 Garden 自己从库里取）------------------------ #
    print("\n② 想起来")
    ctx = garden.context_for_turn(me, "晚饭吃什么")
    for block in ctx.blocks:
        print(f"    [{block['stage']}] {block['text']}")

    # ---- ③ 模型自己调工具 --------------------------------------------- #
    print("\n③ 模型自己调工具")
    wrote = garden.invoke_tool(me, ToolCall(
        name="memory_write",
        arguments={"summary": "周末要去看医生",
                   "content": "答应自己这周末一定去看医生。"},
    ))
    print(f"    memory_write → {wrote.ok}，回执 {wrote.mutations}")

    found = garden.invoke_tool(me, ToolCall(
        name="memory_search", arguments={"query": "医生"}))
    print(f"    memory_search → {found.content!r}")

    # ---- ④ 整理（非空，真的改动库）------------------------------------ #
    print("\n④ 整理")
    check = garden.check_maintenance(me)
    print(f"    要不要整理：{check.needed}（{check.reason}）")
    if check.needed:
        tidy = garden.run_and_store_maintenance(
            me, MaintenanceRequest(locale="zh-Hans"))
        print(f"    整理写回：{tidy.written}，"
              f"{tidy.reason or ''}{tidy.error or ''}")
        print(f"    账本（存回你自己的库）：signature="
              f"{str(tidy.trace.get('signature'))[:12]}… "
              f"seed_card_count={tidy.trace.get('seed_card_count')}")

    # ---- ⑤ 换个进程，记忆还在 ----------------------------------------- #
    print("\n⑤ 换个进程再来（重新打开同一个库）")
    reopened = _garden(db)
    again_ctx = reopened.context_for_turn(me, "晚饭吃什么")
    print(f"    仍然想得起来：{[b['text'] for b in again_ctx.blocks]}")

    # ---- ⑥ 别人读不到 ------------------------------------------------- #
    print("\n⑥ 别人读不到")
    someone_else = Scope(tenant_id="user-99", actor=Actor(user_id="user-99"))
    print(f"    另一个用户召回到：{reopened.context_for_turn(someone_else, '晚饭').record_ids}")

    print("\n完。真接的时候只换 EchoModel，其余照抄。")


if __name__ == "__main__":
    main()
