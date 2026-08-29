"""模型 —— **key 在这里，Garden 拿不到。**

Garden 只知道「给你一段提示词，还我一段回复」。用哪家、怎么重试、
怎么限流，全是这个文件的事。
"""
from __future__ import annotations

import json
import os
import urllib.request


class HttpModel:
    """任何 OpenAI 兼容的端点。"""

    def __init__(self) -> None:
        if os.environ.get("OPENROUTER_API_KEY"):
            self.url = "https://openrouter.ai/api/v1/chat/completions"
            self.key = os.environ["OPENROUTER_API_KEY"]
            self.model = os.environ.get("DEMO_MODEL", "deepseek/deepseek-chat")
        elif os.environ.get("DEEPSEEK_API_KEY"):
            self.url = "https://api.deepseek.com/chat/completions"
            self.key = os.environ["DEEPSEEK_API_KEY"]
            self.model = os.environ.get("DEMO_MODEL", "deepseek-chat")
        else:
            raise RuntimeError(
                "没有 API key。设 OPENROUTER_API_KEY 或 DEEPSEEK_API_KEY，"
                "或者用 --fake 跑假模型。"
            )

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        """Garden 只要求这一个方法。``purpose`` 是 capture / dream，
        想给不同用途分不同模型就在这儿分流。"""
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    def chat(self, messages: list[dict]) -> str:
        """聊天回复。和 Garden 无关 —— 这是 agent 自己的事。"""
        body = json.dumps({"model": self.model, "messages": messages,
                           "temperature": 0.7}).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]


class FakeModel:
    """没有 key 时用它。产出固定，够走通全流程。"""

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        if purpose == "dream":
            return json.dumps({"consolidations": []})
        return json.dumps({"cards": [{
            "action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
            "summary": "他不吃辣", "content": "一吃辣就胃疼，点菜会主动避开辣的。",
            "importance": 0.6, "pulse": 0.2,
        }]}, ensure_ascii=False)

    def chat(self, messages: list[dict]) -> str:
        return "（假模型）好的，我记住了。"
