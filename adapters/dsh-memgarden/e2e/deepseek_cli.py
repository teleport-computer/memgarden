"""memgarden serve 的 --model 命令：从 stdin 读提示词，调 DeepSeek，打印回复。

⚠️ 这是**临时的**验证手段，不是最终形态。

sevenfloor §5.4 要的是 host-driven session：模型调用归 DSH（它持有 provider、
key、路由、用量、超时、取消、重试），Garden 只决定问什么、怎么解析、要不要重问。
那需要 capture.begin / capture.feed 这条线，服务目前还没有。

这个脚本绕过了 DSH 直接调模型 —— 足以证明「落卡 → 落库 → 召回」这条链路通，
但不能证明模型调用的归属是对的。两者要分开说。
"""
import json
import os
import sys
import urllib.request

KEY = os.environ["DEEPSEEK_API_KEY"]
MODEL = os.environ.get("MEMGARDEN_MODEL", "deepseek-chat")

prompt = sys.stdin.read()

req = urllib.request.Request(
    "https://api.deepseek.com/v1/chat/completions",
    data=json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 2048,
    }).encode("utf-8"),
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=120) as resp:
    body = json.load(resp)

sys.stdout.write(body["choices"][0]["message"]["content"])
