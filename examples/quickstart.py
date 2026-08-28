"""五分钟跑通：档位 → 提示词 → 解析 → 存储 → 换字段映射。

    uv run --with ./packages/agent-protocol-core --with ./packages/memgarden \
        python packages/memgarden/examples/quickstart.py

全程不联网、不调模型、不需要 API key —— 模型那步用一段假回复代替。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from memgarden.adapt import FieldMap, summary_of, to_card
from memgarden.policies import get_policy
from memgarden.prompts.capture import build_capture_prompt, parse_capture_cards
from memgarden.storage import IdempotencyConflict
from memgarden.stores.sqlite import SqliteStore

# 模型的假回复。真实用法是把 build_capture_prompt 的产物发给任意 LLM。
# ⚠️ 每张卡必须带 `action`（add / merge / supersede）—— 没有 action 的会被当成
# noop 静默丢掉，这是最容易踩的一个坑。
FAKE_REPLY = """{"cards":[
  {"action":"add","type":"fact","summary":"老王不吃辣",
   "content":"吃辣就胃疼，点菜要避开辣菜","bucket":"健康","threads":["饮食"],
   "importance":0.6,"pulse":0.3},
  {"action":"add","type":"fact","summary":"老王在做记忆系统",
   "content":"最近把记忆的判断逻辑抽成了独立的库","bucket":"工作","threads":["项目"],
   "importance":0.6,"pulse":0.3},
  {"action":"add","type":"fact","summary":"第三张",
   "content":"聊天档位最多留两张，所以这张会被裁掉","bucket":"工作","threads":[],
   "importance":0.4,"pulse":0.2}
]}"""


def main() -> None:
    print("① 三个档位 —— 同一套判断，三把尺子")
    for name in ("conversation_capture", "history_import", "curated_archive"):
        policy = get_policy(name)
        cap = policy.max_cards if policy.max_cards is not None else "不限"
        print(f"     {name:22} 每次最多 {cap} 张")

    print("\n② 库告诉你该问模型什么（它自己不调模型）")
    # locale 必填，没有默认值：默认成某种语言，等于把一套桶名硬塞给所有使用者。
    prompt = build_capture_prompt(
        ai_name="io",
        user_name="老王",
        window="用户：我不吃辣，一吃就胃疼\n我：记住了",
        buckets="",
        threads="",
        identity="",
        cards="",
        locale="zh-Hans",
    )
    print(f"     提示词长度 {len(prompt)} 字符（指令是英文，桶名是这个花园的语言）")
    print("     （naming_rule 不传就按 user_name + locale 生成默认的「别叫用户」规则）")

    en = build_capture_prompt(
        ai_name="io", user_name="Alex", window="user: I don't eat spicy food",
        buckets="", threads="", identity="", cards="", locale="en",
    )
    from memgarden.prompts.buckets import BUCKET_SETS
    print("     换成 locale='en' → 桶名整套换英文，且中文那套**不会**同时出现："
          f"{BUCKET_SETS['zh-Hans'] not in en}")

    print("\n③ 解析模型回答，并按档位裁剪")
    # 默认 strict=True：超额不是悄悄砍掉，而是**整批打回**，让你重问模型一次。
    # 这是有意的 —— 让模型自己挑哪两张最值得留，比我们从前两张硬切更好。
    rejected, err = parse_capture_cards(FAKE_REPLY, policy="conversation_capture")
    print(f"     strict=True  → 打回 {len(rejected)} 张，err={err}")
    print("                     （拿到 too_many_cards 就带着这个反馈重问一次模型）")

    # 重问之后模型还是给多了，就用 strict=False 保底：留前 N 张，不让这一轮白跑。
    cards, err = parse_capture_cards(FAKE_REPLY, policy="conversation_capture", strict=False)
    print(f"     strict=False → 保留 {len(cards)} 张（err={err}）")
    for c in cards:
        print(f"       · {c['summary']}  [{c['bucket']}]")

    kept, _ = parse_capture_cards(FAKE_REPLY, policy="curated_archive")
    print(f"     同样 3 张，换成档案档 → 留 {len(kept)} 张（不设上限，用户手打的一条都不能丢）")

    print("\n④ 存进本地 SQLite（数据在你自己机器上，库不加密）")
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "demo.db")
        batch = [{"op": "add", "card": c} for c in cards]
        result = store.apply("user_1", batch, idempotency_key="turn_42")
        print(f"     写入 {len(result.results)} 张，版本号 {result.revision}")

        # 幂等：**同一批内容**重放不会写第二次（网络重试、任务重跑都会这样）
        store.apply("user_1", batch, idempotency_key="turn_42")
        print(f"     同一批内容重放 → 库里仍是 {len(store.load('user_1').cards)} 张")

        # 反过来：同一个 key 送来**不同内容**是冲突，不是重放。
        # 静默返回旧结果会让这批改动凭空消失，而调用方以为写成功了。
        try:
            store.apply("user_1", batch[:1], idempotency_key="turn_42")
            print("     ⚠️ 同 key 不同内容没报错 —— 这批改动会被静默丢掉")
        except IdempotencyConflict:
            print("     同一个 key 换一批内容 → 报冲突（多半是键生成漏了批次标识）")

    print("\n⑤ 接别的记忆库：只换字段映射，不改代码")
    notion = FieldMap(
        summary_fields=("Name",),           # 可公开的摘要从哪来
        text_fields=("Name", "Notes"),      # 参与搜索的全部字段
        private_fields=("Notes",),          # 参与搜索但绝不外泄
    )
    record = {"id": "n1", "Name": "老王不吃辣", "Notes": "吃辣会胃疼"}
    card = to_card(record, notion)
    print(f"     Notion 记录 → 摘要「{card['summary']}」")
    print(f"                    搜索文本「{card['search_text']}」← 摘要和正文都在里面")

    print("\n跑通了：没有 io、没有 API key、没有网络。")


if __name__ == "__main__":
    main()
