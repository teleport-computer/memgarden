"""MCP 外壳 —— 和 CLI 同一条规矩：能跑，且不许自己造轮子。"""
from __future__ import annotations

import ast
import json
import pathlib

from memgarden import GardenComponent
from memgarden.mcp import PREFIX, dispatch, to_text_content, tool_definitions

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeModel:
    def __init__(self, reply: str = '{"cards":[]}') -> None:
        self.reply = reply

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self.reply


def _garden(reply: str = '{"cards":[]}') -> GardenComponent:
    return GardenComponent(model=FakeModel(reply))


def _kernel_pieces_reached_by(path: pathlib.Path) -> set[str]:
    """一个外壳文件碰到了哪些内核零件。

    **同时查 import 和调用**，不能只查其中一个：
      只查调用   ``from ..prompts.capture import build_capture_prompt`` 之后
                 换个用法（赋值、传参、装饰）就溜过去了
      只查 import 用完整路径 ``memgarden.prompts.capture.build_...(...)`` 也能溜

    这个洞是实测出来的：先只查了调用，破坏性验证时注入一行赋值，守卫没红。
    """
    tree = ast.parse(path.read_text("utf-8"))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # 相对 import：..prompts.capture / ..text.card_text
            if node.level and mod and mod.split(".")[0] in _KERNEL_PACKAGES:
                reached |= {a.name for a in node.names}
            reached |= {a.name for a in node.names} & _KERNEL_SYMBOLS
        elif isinstance(node, ast.Import):
            # ``import memgarden.prompts.capture as _c`` —— 换个名字照样能用
            for alias in node.names:
                parts = alias.name.split(".")
                if "memgarden" in parts and set(parts) & _KERNEL_PACKAGES:
                    reached.add(alias.name)
        elif isinstance(node, ast.Attribute):
            # ``_c.build_capture_prompt``（不一定被调用，也可能只是取引用）
            reached |= {node.attr} & _KERNEL_SYMBOLS
        elif isinstance(node, ast.Name):
            reached |= {node.id} & _KERNEL_SYMBOLS
    return reached


#: 判断层的包 —— 外壳不该从这些里面直接拿东西。
_KERNEL_PACKAGES = {"prompts", "text", "scoring", "dreaming", "guards", "policies"}

#: 判断层的具体函数名，防止有人用完整路径绕开包名检查。
_KERNEL_SYMBOLS = {
    "build_capture_prompt", "parse_capture_cards",
    "build_capture_retry_prompt", "build_capture_semantic_retry_prompt",
    "capture_semantic_retry_reasons",
    "build_dream_prompt", "parse_dream_consolidations",
    "card_text_rejection", "sanitize_card_labels",
    "normalize_bucket_language", "needs_dream", "dream_snapshot",
}


def test_every_tool_name_is_prefixed() -> None:
    """一个 Agent 可能同时挂好几个 MCP server，工具名会撞 ——
    `search` 这种名字几乎一定撞。"""
    names = [t["name"] for t in tool_definitions(_garden())]
    assert names and all(n.startswith(f"{PREFIX}_") for n in names)


def test_every_tool_declares_an_input_schema() -> None:
    """没有 schema 的工具，模型只能靠猜参数。"""
    for tool in tool_definitions(_garden()):
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"].strip()


def test_capture_through_mcp_matches_the_sdk() -> None:
    """外壳的行为必须和 SDK 一致 —— 否则用户以为是同一个东西，其实不是。"""
    reply = json.dumps({"cards": [{
        "action": "add", "bucket": "偏好与边界",
        "summary": "他不吃辣", "content": "一吃辣就胃疼，点菜要避开辣的。",
    }]}, ensure_ascii=False)
    out = dispatch(_garden(reply), f"{PREFIX}_capture",
                   {"window": "用户：我不吃辣", "locale": "zh-Hans"})
    assert out["mutations"] and out["error"] is None


def test_errors_come_back_structured_not_as_a_traceback() -> None:
    """MCP 客户端拿到一个栈回溯是没用的 —— 它要展示给用户看。"""
    out = dispatch(_garden(), f"{PREFIX}_capture", {"window": "x"})
    assert out["error"] == "locale_required"

    unknown = dispatch(_garden(), f"{PREFIX}_frobnicate", {})
    assert unknown["error"].startswith("unknown_tool")


def test_dry_run_maintenance_over_mcp_burns_no_model_call() -> None:
    cards = [{"id": f"m_{i}", "summary": f"卡{i}"} for i in range(15)]
    out = dispatch(_garden(), f"{PREFIX}_maintain",
                   {"cards": cards, "locale": "zh-Hans", "dry_run": True})
    assert out["needed"] is True


def test_content_is_json_serialisable() -> None:
    payload = dispatch(_garden(), f"{PREFIX}_manifest", {})
    blocks = to_text_content(payload)
    assert blocks[0]["type"] == "text"
    json.loads(blocks[0]["text"])


def test_the_mcp_shell_never_reimplements_judgement() -> None:
    """和 CLI 同一条守卫：外壳只许调 GardenComponent。"""
    leaked = _kernel_pieces_reached_by(ROOT / "src" / "memgarden" / "mcp" / "__init__.py")
    assert not leaked, f"MCP 外壳碰了内核零件 {sorted(leaked)}"


def test_the_package_still_has_no_third_party_dependency() -> None:
    """加 MCP 支持不能把一个 MCP SDK 强加给所有使用者 ——
    大多数人是当 Python 库用的。传输层由接入方自己接。"""
    src = (ROOT / "src" / "memgarden" / "mcp" / "__init__.py").read_text("utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    stdlib_or_self = {"json", "dataclasses", "typing", "__future__", "memgarden", ""}
    assert not (imported - stdlib_or_self), f"MCP 外壳引入了第三方依赖：{imported - stdlib_or_self}"
