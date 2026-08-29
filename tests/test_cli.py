"""CLI —— 薄壳，且必须一直是薄壳。

装完包能敲一条命令看到效果，决定了别人愿不愿意继续读下去。
但外壳一旦自己拼提示词、自己解析，就会出现「CLI 的行为和 SDK 不一样」，
而且是悄悄地不一样 —— 所以这里守两件事：**能跑** 和 **没自己造轮子**。
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CORE = ROOT / "packages" / "agent-protocol-core" / "src"


def _run(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": f"{SRC}:{CORE}", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(
        [sys.executable, "-m", "memgarden.cli", *args],
        input=stdin, capture_output=True, text=True, env=env,
    )


@pytest.fixture
def fake_model(tmp_path: pathlib.Path) -> str:
    """一条假的模型命令：吃掉 stdin，吐一张干净的卡。"""
    script = tmp_path / "model.sh"
    card = json.dumps({"cards": [{
        "action": "add", "bucket": "偏好与边界", "threads": ["饮食"],
        "summary": "他不吃辣", "content": "一吃辣就胃疼，点菜要避开辣的。",
    }]}, ensure_ascii=False)
    script.write_text(f"#!/bin/sh\ncat >/dev/null\ncat <<'EOF'\n{card}\nEOF\n")
    script.chmod(0o755)
    return str(script)


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


# --------------------------------------------------------------- 能跑

def test_manifest_says_what_this_component_does() -> None:
    """接入方和 CI 都要能机器可读地问「这东西会做什么」。"""
    out = _run("manifest")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["component_id"] == "memgarden"
    assert "capture" in data["capabilities"]
    assert [t["name"] for t in data["tools"]]


def test_capture_produces_mutations(fake_model: str) -> None:
    out = _run("capture", "--window-file", "-", "--locale", "zh-Hans",
               "--model-cmd", fake_model, stdin="用户：我不吃辣")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["mutations"]


def test_locale_is_required(fake_model: str) -> None:
    """CLI 层也不给 locale 默认值 —— 和 SDK 同一条规矩。"""
    out = _run("capture", "--window-file", "-", "--model-cmd", fake_model, stdin="x")
    assert out.returncode != 0


def test_recall_reports_which_stage_picked_each_card(tmp_path: pathlib.Path) -> None:
    cards = tmp_path / "cards.jsonl"
    cards.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in [
        {"id": "m_1", "summary": "他不吃辣", "created_at": "2026-08-01",
         "search_text": "他不吃辣"},
        {"id": "m_2", "summary": "养了只柯基", "created_at": "2026-08-20",
         "search_text": "养了只柯基"},
    ]), encoding="utf-8")
    out = _run("recall", "--query", "狗", "--cards", str(cards), "--limit", "2")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["record_ids"]
    assert data["trace"]["by_stage"]


def test_maintain_dry_run_needs_no_model(tmp_path: pathlib.Path) -> None:
    """调度器高频问「该整理了吗」，不该为此要一个模型命令。"""
    cards = tmp_path / "c.json"
    cards.write_text(json.dumps(
        [{"id": f"m_{i}", "summary": f"卡{i}"} for i in range(15)]
    ), encoding="utf-8")
    out = _run("maintain", "--cards", str(cards), "--dry-run")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["needed"] is True


# --------------------------------------------------- 退出码要能分辨两种「没产出」

def test_nothing_worth_keeping_exits_zero(tmp_path: pathlib.Path) -> None:
    m = tmp_path / "empty.sh"
    m.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"cards":[]}\'\n')
    m.chmod(0o755)
    out = _run("capture", "--window-file", "-", "--locale", "zh-Hans",
               "--model-cmd", str(m), stdin="哈哈哈")
    assert out.returncode == 0


def test_a_parse_failure_exits_nonzero(tmp_path: pathlib.Path) -> None:
    """🔴 流水线必须能分辨这两件事。混淆的代价是把失败当成正常、
    推进游标 —— 那批对话永远不会再被看一眼。"""
    m = tmp_path / "bad.sh"
    m.write_text("#!/bin/sh\ncat >/dev/null\necho '这不是 JSON'\n")
    m.chmod(0o755)
    out = _run("capture", "--window-file", "-", "--locale", "zh-Hans",
               "--model-cmd", str(m), stdin="重要的话")
    assert out.returncode == 1


# --------------------------------------------------------------- 是薄壳

def test_the_cli_never_reimplements_judgement() -> None:
    """CLI 只许调 GardenComponent，不许自己拼提示词或解析。

    外壳自己造一套的后果不是重复，是**悄悄地不一样**：
    CLI 跑出来的结果和 SDK 不同，而用户以为它们是同一个东西。
    """
    leaked = _kernel_pieces_reached_by(SRC / "memgarden" / "cli" / "__init__.py")
    assert not leaked, f"CLI 碰了内核零件 {sorted(leaked)} —— 它应该只调 GardenComponent"


def test_the_cli_sends_the_prompt_through_stdin_not_argv(tmp_path: pathlib.Path) -> None:
    """提示词有几千字、含换行和引号 —— 塞进 argv 会被 shell 截断或改写。"""
    m = tmp_path / "echo_len.sh"
    m.write_text('#!/bin/sh\nwc -c >/dev/null\necho \'{"cards":[]}\'\n')
    m.chmod(0o755)
    out = _run("capture", "--window-file", "-", "--locale", "zh-Hans",
               "--model-cmd", str(m), stdin="x" * 5000)
    assert out.returncode == 0, out.stderr
