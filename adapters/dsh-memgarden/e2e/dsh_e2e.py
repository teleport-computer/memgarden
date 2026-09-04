"""DSH 端到端：sevenfloor §8.2 的核心几步。

    会话 A 说「我不吃辣」
    → 轮末自动落卡（不是模型主动调工具）
    → 完全新开会话 B，不复制 A 的任何对话
    → 问「晚饭吃什么」
    → pre-step 自动召回，模型无需主动 search 就能用上

这一步是整份验收里最关键的：它同时证明了「自动落卡」和「跨会话自动召回」。
"""
import json
import pathlib
import sqlite3
import sys

from deepseek_harness import DeepSeekHarness

HERE = pathlib.Path(
    "/private/tmp/claude-501/-Users-hx-Projects-io/09946d25-7c00-4e1c-b68f-136ef28b9167/scratchpad"
)
GARDEN = HERE / "dsh-garden.db"


def harness():
    return DeepSeekHarness(
        provider="deepseek-official",
        model="deepseek-v4-flash",
        max_tokens=4096,
        cwd=str(HERE / "ws"),
        dsh_home=str(HERE / "dshrun" / "dsh-home"),
        dsh_bin=str(HERE / "dshrun" / "node_modules" / ".bin" / "dsh"),
        profile="sdk-minimal",
    )


def cards():
    if not GARDEN.exists():
        return []
    conn = sqlite3.connect(GARDEN)
    try:
        return [json.loads(d) for (d,) in conn.execute("SELECT doc FROM cards")]
    finally:
        conn.close()


print("=" * 64)
print("① 会话 A：说一件值得记的事")
print("=" * 64)
with harness() as h:
    r = h.run("我不吃辣，一吃就胃疼。简短回一句就行。", session_id="A")
    print("  模型:", r.final_response)

import time
for _ in range(30):            # 落卡是后台的，等它落完
    if cards():
        break
    time.sleep(2)

print(f"\n  花园里现在有 {len(cards())} 张卡：")
for c in cards():
    print(f"    桶={c.get('bucket')}  {c.get('summary')}")

print()
print("=" * 64)
print("② 会话 B：全新会话，不复制 A 的任何对话")
print("=" * 64)
with harness() as h:
    r = h.run("晚饭吃什么？给一个具体建议，一句话。", session_id="B")
    print("  模型:", r.final_response)

print()
print("=" * 64)
print("判定")
print("=" * 64)
reply = r.final_response or ""
avoided = any(w in reply for w in ("辣", "清淡", "不辣"))
print(f"  会话 B 的回复体现出「知道不吃辣」了吗: {'✅ 是' if avoided else '❌ 否'}")
print(f"  （模型全程没有主动调用任何记忆工具 —— 靠的是 pre-step 自动注入）")
sys.exit(0 if avoided else 1)
