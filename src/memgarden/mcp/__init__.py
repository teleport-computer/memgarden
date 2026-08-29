"""MCP 外壳 —— 把 GardenComponent 映射成 MCP 工具。

## 为什么要它

MCP 是「不写 Python 也能接」的那条路。别人的 Agent 只要能说 MCP，
就能用上这套记忆判断，不必理解这个包的结构、也不必用 Python。

## 和 CLI 同一条硬约束：只调顶层接口

外壳不许自己拼提示词、自己解析。一旦造了第二套，就会出现
「MCP 的行为和 SDK 不一样」——而且是悄悄地不一样。有测试守着。

## 为什么不直接依赖某个 MCP 框架

这个包的硬指标是**只依赖标准库**。装一个 MCP SDK 进来，等于给所有使用者
强加一个他们可能不需要的依赖（大多数人是当 Python 库用的）。

所以这里只产出**工具定义和分发逻辑**，真正的传输层（stdio / SSE）由接入方
用自己已有的 MCP 框架接上去 —— 十几行的事。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable

from ..component import GardenComponent
from ..contracts import CaptureRequest, ContextRequest, MaintenanceRequest, ToolCall

#: MCP 工具名前缀。带前缀是因为一个 Agent 可能同时挂好几个 MCP server，
#: 工具名会撞 —— `search` 这种名字几乎一定撞。
PREFIX = "memgarden"


def tool_definitions(garden: GardenComponent) -> list[dict]:
    """这个组件对外暴露的 MCP 工具。

    比 ``garden.tools()`` 多几个：那几个是**给对话模型用的**（模型自己决定
    要不要调），这里还包括**给接入方的运维动作**（落卡、整理），
    因为 MCP 客户端往往就是那个 Runtime 本身。
    """
    out = [
        {
            "name": f"{PREFIX}_capture",
            "description": "判断一段对话里有什么值得记住，返回改动指令（不写库）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "window": {"type": "string", "description": "渲染好的对话文本"},
                    "locale": {"type": "string",
                               "description": "花园语言，如 zh-Hans / en。必填。"},
                    "buckets": {"type": "string"},
                    "identity": {"type": "string"},
                },
                "required": ["window", "locale"],
            },
        },
        {
            "name": f"{PREFIX}_recall",
            "description": "在候选记忆里挑出这一轮该想起的几张，并说明每张是被哪一段选中的。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "candidates": {"type": "array", "items": {"type": "object"}},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query", "candidates"],
            },
        },
        {
            "name": f"{PREFIX}_maintain",
            "description": "判断该不该整理一遍记忆；需要的话返回合并指令。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cards": {"type": "array", "items": {"type": "object"}},
                    "locale": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                    "last_signature": {"type": "string"},
                    "last_seed_card_count": {"type": "integer"},
                },
                "required": ["cards", "locale"],
            },
        },
        {
            "name": f"{PREFIX}_manifest",
            "description": "这个记忆组件会做什么（能力声明）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    # 模型可调的那几个（memory_search / memory_write）原样带上前缀暴露。
    for t in garden.tools():
        out.append({
            "name": f"{PREFIX}_{t.name}",
            "description": t.description,
            "inputSchema": t.parameters,
        })
    return out


def dispatch(garden: GardenComponent, name: str, arguments: dict) -> dict:
    """执行一次 MCP 工具调用。返回可直接 JSON 序列化的结果。

    **不抛异常给传输层** —— MCP 客户端拿到一个栈回溯是没用的，
    它需要一个结构化的、能展示给用户看的错误。
    """
    args = arguments or {}
    short = name[len(PREFIX) + 1:] if name.startswith(PREFIX + "_") else name

    try:
        if short == "capture":
            return asdict(garden.capture(CaptureRequest(
                window=str(args.get("window") or ""),
                locale=str(args.get("locale") or ""),
                buckets=str(args.get("buckets") or ""),
                identity=str(args.get("identity") or ""),
            )))
        if short == "recall":
            return asdict(garden.build_context(ContextRequest(
                query=str(args.get("query") or ""),
                candidates=list(args.get("candidates") or []),
                limit=int(args.get("limit") or 8),
            )))
        if short == "maintain":
            return asdict(garden.run_maintenance(MaintenanceRequest(
                cards=list(args.get("cards") or []),
                locale=str(args.get("locale") or ""),
                dry_run=bool(args.get("dry_run", True)),
                last_signature=str(args.get("last_signature") or ""),
                last_seed_card_count=int(args.get("last_seed_card_count") or 0),
            )))
        if short == "manifest":
            return {"capabilities": garden.capabilities().as_dict(),
                    "tools": [asdict(t) for t in garden.tools()]}
        # 剩下的是模型可调的工具，转交组件。
        return asdict(garden.invoke_tool(ToolCall(name=short, arguments=args)))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def to_text_content(payload: dict) -> list[dict]:
    """把结果包成 MCP 的 content 数组。传输层直接用。"""
    return [{"type": "text",
             "text": json.dumps(payload, ensure_ascii=False, indent=2, default=str)}]


__all__ = ["PREFIX", "tool_definitions", "dispatch", "to_text_content"]
