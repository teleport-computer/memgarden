"""DSH 完整验收 —— sevenfloor §8.2 的场景，一次跑完。

覆盖五组：

    A  自动落卡 + 跨会话自动召回（模型不主动调工具）
    B  模型主动调 memgarden_memory_write
    C  多 agent 隔离：另一个 agent 读不到别人的私有记忆
    D  失败路径：子进程不存在 / 会话不存在 / 无模型错误 / manifest
    E  modelless service 下 Maintenance/Dream 仍由 DSH 模型驱动

跑法：

    export DEEPSEEK_API_KEY=...
    python e2e/dsh_acceptance.py

⚠️ 会真实调用模型，每跑一次有成本。
除 npm DSH 外还需要从同一官方 commit 源码运行 deepseek_harness
Python SDK；详见 Adapter README 的验证方式。
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                       # adapters/dsh-memgarden
RESULTS: list[tuple[bool, str, str]] = []
DSH_VERSION = "0.1.2-alpha.4"
DSH_COMMIT = "4e84901e6471b79ec0338099867ebb4606d12bb5"
TOOL_PROBE = "MG_TOOL_PROBE_20260908"
_HARNESS_CLASS: type | None = None


def _acceptance_model() -> str:
    # Historical default is retained; overrides must be explicit in evidence.
    model = os.environ.get("MEMGARDEN_ACCEPTANCE_MODEL", "deepseek-v4-flash").strip()
    if not model:
        raise ValueError("MEMGARDEN_ACCEPTANCE_MODEL must not be blank")
    return model


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #

class Env:
    """一套独立的 DSH home + 花园库。每组用例各起一套，互不干扰。"""

    def __init__(self, tenant: str = "u1", *, owner: str | None = None,
                 garden: pathlib.Path | None = None,
                 bad_bin: str | None = None) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="dsh-acc-"))
        self.home = self.dir / "home"
        self.garden = garden or self.dir / "garden.db"
        self.workspace = self.dir / "ws"
        self.workspace.mkdir(parents=True)
        self.log = self.dir / "mg.log"
        self.state_dir = self.dir / "state"
        self.tenant = tenant
        self.owner = owner or tenant
        self.dsh_bin = _dsh_bin()
        self._init_profile(bad_bin or _memgarden_bin())

    def _init_profile(self, bin_path: str) -> None:
        subprocess.run(
            [str(self.dsh_bin), "--profile", "sdk-minimal", "--dump-default-config"],
            env={**os.environ, "DSH_HOME": str(self.home)},
            stdout=subprocess.DEVNULL, check=True,
        )
        plugin_link = self.home / "profiles" / "sdk-minimal" / "node_modules" / "dsh-memgarden"
        plugin_link.parent.mkdir(parents=True, exist_ok=True)
        if plugin_link.exists() or plugin_link.is_symlink():
            plugin_link.unlink()
        # 走产品路：用随包发布的 Adapter + CLI 装，不再手工连 symlink。
        subprocess.run([_memgarden_bin(), 'install-dsh',
                        '--dsh-home', str(self.home),
                        '--tenant', self.tenant, '--owner', self.owner,
                        '--bin', bin_path,
                        '--storage', str(self.garden),
                        '--state-dir', str(self.state_dir)],
                       check=True, stdout=subprocess.DEVNULL)

        # 验收必须验「真安装出来的配置」。以前这里 install 后又
        # 手写覆盖 cordis.patch.yml，恰好把 memoryOwner / stateDir 丢了：
        # 脚本看似在验 Adapter，实际插件会因没 owner 直接不启用。
        patch = self.home / "profiles" / "sdk-minimal" / "cordis.patch.yml"
        installed = patch.read_text(encoding="utf-8")
        assert f"memoryOwner: '{self.owner}'" in installed
        assert f"stateDir: '{self.state_dir}'" in installed

    def harness(self) -> Any:
        os.environ["MEMGARDEN_DEBUG_LOG"] = str(self.log)
        if _HARNESS_CLASS is None:
            raise RuntimeError("deepseek_harness SDK 尚未通过环境校验")
        return _HARNESS_CLASS(
            provider="deepseek-official", model=_acceptance_model(),
            max_tokens=4096, cwd=str(self.workspace),
            dsh_home=str(self.home), dsh_bin=str(self.dsh_bin),
            profile="sdk-minimal",
        )

    def cards(self) -> list[dict]:
        if not self.garden.exists():
            return []
        conn = sqlite3.connect(self.garden)
        try:
            return [json.loads(d) for (d,) in conn.execute(
                "SELECT doc FROM cards WHERE tenant=? AND owner=?",
                (self.tenant, self.owner),
            )]
        finally:
            conn.close()

    def maintenance_state(self) -> dict:
        if not self.garden.exists():
            return {}
        conn = sqlite3.connect(self.garden)
        try:
            row = conn.execute(
                "SELECT signature, seed_card_count, revision, updated_at, schema_version "
                "FROM maintenance_state WHERE tenant=? AND owner=? AND mount=?",
                (self.tenant, self.owner, "agent-private"),
            ).fetchone()
            if row is None:
                return {}
            keys = ("signature", "seed_card_count", "revision", "updated_at",
                    "schema_version")
            return dict(zip(keys, row))
        finally:
            conn.close()

    def logs(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def seed_cards(self, count: int) -> None:
        """用公开 wire API 预置卡，不直接改 SQLite 内部表。"""
        scope = {
            "tenant_id": self.tenant,
            "memory_owner_id": self.owner,
            "actor": {"user_id": self.tenant, "agent_id": "acceptance-seed"},
            "allowed_mounts": ["agent-private"],
        }
        requests = [
            {"id": str(i), "method": "records.write", "params": {
                "scope": scope,
                # 故意放入高度重叠的原始卡：真正的 Dream 应把它们
                # 收敛成新卡并留下 supersede 链，不只是打一行“整理结果”日志。
                "text": f"验收事实 {i}：对方每个周末早上八点都会去公园跑步。",
                "bucket": "general",
                "idempotency_key": f"acceptance-seed-{i}",
            }}
            for i in range(count)
        ]
        proc = subprocess.run(
            [_memgarden_bin(), "serve", "--storage", f"sqlite:///{self.garden}"],
            input="".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests),
            capture_output=True, text=True, timeout=60,
        )
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert proc.returncode == 0 and len(replies) == count
        assert all(r.get("ok") and not (r.get("result") or {}).get("error")
                   for r in replies), replies

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _dsh_bin() -> pathlib.Path:
    found = os.environ.get("DSH_BIN") or shutil.which("dsh")
    if found:
        return pathlib.Path(found)
    guess = HERE / ".." / ".." / ".." / "node_modules" / ".bin" / "dsh"
    if guess.exists():
        return guess.resolve()
    print("找不到 dsh —— 先 npm install @deepseek-ai/dsh@0.1.2-alpha.4")
    sys.exit(2)


def _checkout_commit(module_file: str) -> str:
    """Return the git commit for an editable official SDK checkout, if any."""
    resolved = pathlib.Path(module_file).resolve()
    for parent in resolved.parents:
        if not (parent / "python" / "sdk" / "src" / "deepseek_harness").is_dir():
            continue
        proc = subprocess.run(
            ["git", "-C", str(parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return ""


def _sdk_compatibility_error(version: str, source_commit: str) -> str:
    """Exact alpha.4 evidence requires the SDK source from the same commit."""
    if source_commit == DSH_COMMIT:
        return ""
    where = f"source commit={source_commit}" if source_commit else "无可验证的源码 commit"
    return (
        f"deepseek-harness-sdk {version} 不能证明与 DSH {DSH_VERSION} "
        f"同源（{where}）。请按 Adapter README 从官方 {DSH_COMMIT} "
        "checkout 的 python/sdk 环境运行验收。"
    )


def _load_harness_class() -> type:
    try:
        module = importlib.import_module("deepseek_harness")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "找不到 deepseek_harness。PyPI 当前没有与 DSH "
            f"{DSH_VERSION} 精确对应的 SDK 发行版；请按 Adapter README "
            f"从官方 commit {DSH_COMMIT} 的 python/sdk 环境运行。"
        ) from exc
    try:
        version = importlib.metadata.version("deepseek-harness-sdk")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    source_commit = _checkout_commit(str(getattr(module, "__file__", "")))
    mismatch = _sdk_compatibility_error(version, source_commit)
    if mismatch:
        raise RuntimeError(mismatch)
    harness = getattr(module, "DeepSeekHarness", None)
    if not isinstance(harness, type):
        raise RuntimeError("deepseek_harness.DeepSeekHarness 不存在")
    return harness


def _dsh_version_error(binary: pathlib.Path) -> str:
    try:
        proc = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"无法执行 dsh --version: {exc}"
    got = (proc.stdout or proc.stderr).strip().splitlines()
    actual = got[-1].strip() if got else ""
    if proc.returncode != 0 or actual != DSH_VERSION:
        return f"需要 dsh {DSH_VERSION}，当前是 {actual or '无版本输出'}"
    return ""


def _dsh_closure_error(binary: pathlib.Path) -> str:
    """Reject a published alpha.4 launcher with a mixed rc dependency closure."""
    # A launcher built from the same exact official checkout is already pinned
    # by source.  This is the preferred path documented in the README.
    if _checkout_commit(str(binary)) == DSH_COMMIT:
        return ""

    resolved = binary.resolve()
    node_modules = next(
        (parent for parent in resolved.parents if parent.name == "node_modules"),
        None,
    )
    if node_modules is None:
        return (
            "无法证明 dsh 依赖闭包版本；请使用官方 exact commit "
            "的源码 launcher，或提供可检查的 npm node_modules 安装"
        )

    manifests = sorted(node_modules.glob("**/@deepseek-ai/dsh*/package.json"))
    seen = []
    wrong = []
    unreadable = []
    for manifest in manifests:
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(str(manifest))
            continue
        name = str(package.get("name") or "")
        version = str(package.get("version") or "")
        if name == "@deepseek-ai/dsh" or name.startswith("@deepseek-ai/dsh-"):
            seen.append(name)
            if version != DSH_VERSION:
                wrong.append(f"{name}@{version or '?'}")
    if "@deepseek-ai/dsh" not in seen:
        return "npm 安装不完整：找不到 @deepseek-ai/dsh package manifest"
    if unreadable:
        return f"npm 安装损坏：无法读取 {unreadable[0]}"
    if wrong:
        sample = ", ".join(wrong[:5])
        suffix = f" 等 {len(wrong)} 个" if len(wrong) > 5 else ""
        return (
            f"DSH 依赖闭包混入了非 {DSH_VERSION} 版本：{sample}{suffix}。"
            "顶层 npm alpha.4 使用 caret 依赖，单独固定顶层包不足以复现该基线。"
        )
    return ""


def _memgarden_bin() -> str:
    found = os.environ.get("MEMGARDEN_BIN") or shutil.which("memgarden")
    if found:
        return found
    print("找不到 memgarden —— 先 pip install memgarden")
    sys.exit(2)


def _has_tool_call(events: list[dict], name: str) -> bool:
    return any(
        event.get("type") == "tool/call"
        and isinstance(event.get("data"), dict)
        and event["data"].get("name") == name
        for event in events
        if isinstance(event, dict)
    )


def _recall_is_proven(logs: str, reply: str, events: list[dict]) -> bool:
    counts = [int(value) for value in re.findall(r"召回 (\d+) 条", logs)]
    return bool(
        counts and counts[-1] > 0 and "胃疼" in reply
        and not _has_tool_call(events, "memgarden_memory_search")
    )


def _tool_call_is_proven(events: list[dict], cards: list[dict], marker: str) -> bool:
    called = _has_tool_call(events, "memgarden_memory_write")
    persisted = any(
        card.get("source") == "model_tool"
        and marker in f"{card.get('summary', '')}\n{card.get('content', '')}"
        for card in cards
    )
    return called and persisted


def _tool_write_diagnostic(events: list[dict], cards: list[dict], marker: str) -> str:
    """Show which half of the evidence is missing without dumping model text."""
    return json.dumps({
        "event_count": len(events),
        "tool_calls": [str(event.get("data", {}).get("name", ""))[:120]
                       for event in events if isinstance(event, dict)
                       and event.get("type") == "tool/call"
                       and isinstance(event.get("data"), dict)][:10],
        "card_count": len(cards),
        "marker_cards": [{"source": str(card.get("source", ""))[:80],
                          "summary_has_marker": marker in card.get("summary", ""),
                          "content_has_marker": marker in card.get("content", "")}
                         for card in cards if marker in
                         f"{card.get('summary', '')}\n{card.get('content', '')}"][:10],
    }, ensure_ascii=False, separators=(",", ":"))


def _maintenance_is_proven(logs: str, cards: list[dict], ledger: dict) -> bool:
    result_lines = [line for line in logs.splitlines() if "整理结果" in line]
    successful_receipt = bool(
        result_lines and "written=true" in result_lines[-1]
        and "error=-" in result_lines[-1]
        and "整理失败" not in logs
    )
    dream_ids = {
        str(card.get("id") or "") for card in cards
        if card.get("source") == "memory_dream"
    }
    durable_chain = bool(dream_ids) and any(
        card.get("archived") is True
        and str(card.get("superseded_by") or "") in dream_ids
        for card in cards
    )
    durable_ledger = bool(
        ledger.get("signature") and int(ledger.get("seed_card_count") or 0) >= 10
    )
    return successful_receipt and durable_chain and durable_ledger


def _maintenance_diagnostic(logs: str, cards: list[dict], ledger: dict) -> str:
    """Bounded failure evidence without card summary/content bodies."""
    relevant = [
        line[:240] for line in logs.splitlines()
        if any(word in line for word in ("整理", "maintenance", "dispose", "capture 失败"))
    ][-12:]
    card_state = [{
        "id": str(card.get("id") or ""),
        "source": str(card.get("source") or ""),
        "archived": card.get("archived") is True,
        "superseded_by": str(card.get("superseded_by") or ""),
    } for card in cards]
    return json.dumps({
        "logs": relevant,
        "ledger": ledger,
        "cards": card_state,
    }, ensure_ascii=False, separators=(",", ":"))[:4000]


# --------------------------------------------------------------------------- #
# A. 自动落卡 + 跨会话召回
# --------------------------------------------------------------------------- #

def group_a() -> None:
    print("\nA. 自动落卡 + 跨会话自动召回")
    env = Env(tenant="alice")
    try:
        with env.harness() as h:
            h.run("我不吃辣，一吃就胃疼。简短回一句就行。", session_id="A")

        cards = env.cards()
        captured = [card for card in cards
                    if card.get("source") == "conversation_capture"
                    and "辣" in f"{card.get('summary', '')}\n{card.get('content', '')}"
                    and "胃疼" in f"{card.get('summary', '')}\n{card.get('content', '')}"]
        check(bool(captured), "轮末自动落卡且来源正确", f"{len(cards)} 张")

        # 🔴 模型调用必须走 DSH：服务端没有 --model，能落卡就说明是宿主调的
        check("--model" not in env.logs(), "模型调用归 DSH（服务没有模型配置）")

        with env.harness() as h:
            r = h.run("根据你自动想起的长期记忆，我吃辣会有什么具体身体反应？"
                      "只回答这个反应。", session_id="B")
            reply = r.final_response or ""
        check(_recall_is_proven(env.logs(), reply, r.events),
              "全新会话实际召回注入并用到独特细节", reply[:80])
    finally:
        env.cleanup()


# --------------------------------------------------------------------------- #
# B. 模型主动调工具
# --------------------------------------------------------------------------- #

def group_b() -> None:
    print("\nB. 模型主动调 memory_write 工具")
    env = Env(tenant="bob")
    try:
        with env.harness() as h:
            result = h.run("请务必调用 memgarden_memory_write 工具，"
                           f"把验收标记「{TOOL_PROBE}」原样写入 summary 和 "
                           "content；工具成功后只回答“完成”。", session_id="W")
        check("注册了" in env.logs(), "工具注册进了 DSH 的 Tool Registry",
              next((l for l in env.logs().splitlines() if "注册了" in l), ""))
        cards = env.cards()
        check(_tool_call_is_proven(result.events, cards, TOOL_PROBE),
              "memory_write 有 tool/call 事件且以 model_tool 来源落库",
              _tool_write_diagnostic(result.events, cards, TOOL_PROBE))
    finally:
        env.cleanup()


# --------------------------------------------------------------------------- #
# C. 多 agent 隔离
# --------------------------------------------------------------------------- #

def group_c() -> None:
    print("\nC. 同租户、跨 owner 隔离")
    a = Env(tenant="shared-tenant", owner="owner-a")
    b = Env(tenant="shared-tenant", owner="owner-b", garden=a.garden)
    try:
        with a.harness() as h:
            h.run("我对花生过敏。简短回一句。", session_id="A")
        check(bool(a.cards()), "A 记下了自己的事", f"{len(a.cards())} 张")

        # B 用**同一 tenant、同一 SQLite 文件**，只换 owner。
        # 这才是 memory_owner_id 新增后要证明的隔离边界；旧脚本换的
        # 是 tenant，只能证明原本就有的 tenant 隔离。
        with b.harness() as h:
            r = h.run("我有什么忌口吗？一句话。", session_id="B")
            reply = r.final_response or ""
        check("花生" not in reply,
              "同租户的另一个 owner 读不到（同一个库）", reply[:40])
        check("召回 0 条" in b.logs(), "召回结果确实是空的")
    finally:
        a.cleanup()
        b.cleanup()


# --------------------------------------------------------------------------- #
# D. 失败路径
# --------------------------------------------------------------------------- #

def group_d() -> None:
    print("\nD. 失败路径")

    # D1 子进程不存在 —— 对话必须照常，只是没有记忆
    env = Env(tenant="d1", bad_bin="/nonexistent/memgarden")
    try:
        with env.harness() as h:
            r = h.run("你好，简短回一句。", session_id="D1")
        check(bool((r.final_response or "").strip()),
              "服务起不来时对话仍能进行", (r.final_response or "")[:30])
        check(not env.cards(), "没有假装记住了什么")
    except Exception as e:      # noqa: BLE001
        check(False, "服务起不来时对话仍能进行", f"抛异常了: {e}")
    finally:
        env.cleanup()

    # D2 会话 id 不存在 —— 必须报得清楚，而不是含糊的 internal_error
    out = _rpc({"id": "1", "method": "capture.feed",
                "params": {"session_id": "不存在的", "reply": "x"}})
    code = (out.get("error") or {}).get("code", "")
    msg = (out.get("error") or {}).get("message", "")
    # 断**错误码**，不断消息文字 —— 消息是人话、会改、还可能被翻译；
    # 宿主的分支逻辑靠的也是码。
    check(code == "unknown_session",
          "喂一个不存在的会话 → unknown_session", f"{code}: {msg[:50]}")

    # D3 没配模型时，需要模型的方法要给出说得清的错
    out = _rpc({"id": "1", "method": "capture.run",
                "params": {"scope": {
                               "tenant_id": "t", "memory_owner_id": "owner",
                               "actor": {"user_id": "t", "agent_id": "dsh"},
                               "allowed_mounts": ["agent-private"],
                           }, "window": "x",
                           "locale": "zh-Hans"}})
    code = (out.get("error") or {}).get("code", "")
    check(code == "model_not_configured",
          "没配模型 → model_not_configured", code)

    # D4 不需要模型的方法照常可用
    out = _rpc({"id": "1", "method": "manifest.get", "params": {}})
    check(out.get("ok") is True, "不需要模型的方法不受影响")


# --------------------------------------------------------------------------- #
# E. modelless service 下的自动整理
# --------------------------------------------------------------------------- #

def group_e() -> None:
    print("\nE. Maintenance/Dream 也由 DSH 模型驱动")
    env = Env(tenant="dream-tenant", owner="dream-owner")
    try:
        # 默认阈值是 10 张 seed card。用 wire 写入而不是直接改表，
        # 确保验收不依赖 SQLite 内部结构。本验收夹具特意写入
        # 高度重复的卡，所以预期真正产生 dream + supersede 链；
        # 这不意味着一般 Maintenance 的合法 no-op 应被视为失败。
        env.seed_cards(10)
        with env.harness() as h:
            h.run("请简短回答：好的。", session_id="M")
        logs = env.logs()
        check("该整理了" in logs, "达到阈值后确实进入整理")
        current_cards = env.cards()
        ledger = env.maintenance_state()
        check(_maintenance_is_proven(logs, current_cards, ledger),
              "Maintenance 成功，且整理账本与 supersede 卡链持久化",
              _maintenance_diagnostic(logs, current_cards, ledger))
    finally:
        env.cleanup()


def _rpc(request: dict) -> dict:
    """对一个临时服务发一条请求。服务不配模型。"""
    db = pathlib.Path(tempfile.mkdtemp()) / "g.db"
    proc = subprocess.run(
        [_memgarden_bin(), "serve", "--storage", f"sqlite:///{db}"],
        input=json.dumps(request) + "\n",
        capture_output=True, text=True, timeout=60,
    )
    line = (proc.stdout or "").strip().splitlines()
    return json.loads(line[0]) if line else {"ok": False, "error": {"message": proc.stderr[:200]}}


# --------------------------------------------------------------------------- #

_GROUPS = {"A": group_a, "B": group_b, "C": group_c, "D": group_d, "E": group_e}


def _selected_groups(argv: list[str] | None = None) -> list:
    parser = argparse.ArgumentParser(description="MemGarden pinned DSH acceptance")
    parser.add_argument(
        "--group", action="append", choices=tuple(_GROUPS), dest="groups",
        help="只跑指定组；可重复传入，默认跑 A–E 全部",
    )
    selected = parser.parse_args(argv).groups or list(_GROUPS)
    return [_GROUPS[name] for name in selected]


def main(argv: list[str] | None = None) -> int:
    global _HARNESS_CLASS
    # 先交给 argparse：`--help` 应当在没有 key / SDK / DSH 的机器上也能看。
    groups = _selected_groups(argv)
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("需要 DEEPSEEK_API_KEY")
        return 2

    try:
        _acceptance_model()
    except ValueError as exc:
        print(f"验收环境不满足：{exc}")
        return 2

    try:
        _HARNESS_CLASS = _load_harness_class()
    except RuntimeError as exc:
        print(f"验收环境不满足：{exc}")
        return 2
    binary = _dsh_bin()
    version_error = _dsh_version_error(binary)
    if version_error:
        print(f"验收环境不满足：{version_error}")
        return 2
    closure_error = _dsh_closure_error(binary)
    if closure_error:
        print(f"验收环境不满足：{closure_error}")
        return 2
    service_binary = pathlib.Path(_memgarden_bin())
    if not service_binary.is_file():
        print(f"验收环境不满足：memgarden 可执行文件不存在: {service_binary}")
        return 2

    print("=" * 66)
    print("DSH 验收 —— dsh 0.1.2-alpha.4 + memgarden; model=" + _acceptance_model())
    print("=" * 66)

    for group in groups:
        try:
            if not binary.exists():
                raise FileNotFoundError(
                    f"pinned DSH executable disappeared after preflight: {binary}; "
                    "do not place the npm installation inside TMPDIR and wait for "
                    "the package install to finish before acceptance"
                )
            if not service_binary.exists():
                raise FileNotFoundError(
                    "memgarden executable disappeared after preflight: "
                    f"{service_binary}; do not rebuild its environment during acceptance"
                )
            group()
        except Exception as exc:      # noqa: BLE001
            check(False, f"{group.__name__} 整组异常", repr(exc)[:120])
            # 验收只使用本文件里的合成内容；留下完整栈便于定位环境问题。
            traceback.print_exc()

    print("\n" + "=" * 66)
    failed = [r for r in RESULTS if not r[0]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for _, name, detail in failed:
        print(f"  FAIL {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
