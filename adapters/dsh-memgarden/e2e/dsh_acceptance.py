"""DSH 完整验收 —— sevenfloor §8.2 的场景，一次跑完。

覆盖四组：

    A  自动落卡 + 跨会话自动召回（模型不主动调工具）
    B  模型主动调 memgarden_memory_search / memory_write
    C  多 agent 隔离：另一个 agent 读不到别人的私有记忆
    D  失败路径：子进程不存在 / 握手不兼容 / 模型返回空 / 会话不存在

跑法：

    export DEEPSEEK_API_KEY=...
    python e2e/dsh_acceptance.py

⚠️ 会真实调用模型，每跑一次有成本。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from deepseek_harness import DeepSeekHarness

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                       # adapters/dsh-memgarden
RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #

class Env:
    """一套独立的 DSH home + 花园库。每组用例各起一套，互不干扰。"""

    def __init__(self, tenant: str = "u1", *, bad_bin: str | None = None) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="dsh-acc-"))
        self.home = self.dir / "home"
        self.garden = self.dir / "garden.db"
        self.workspace = self.dir / "ws"
        self.workspace.mkdir(parents=True)
        self.log = self.dir / "mg.log"
        self.tenant = tenant
        self.dsh_bin = _dsh_bin()
        self._init_profile()
        self._write_patch(bad_bin or _memgarden_bin())

    def _init_profile(self) -> None:
        subprocess.run(
            [str(self.dsh_bin), "--profile", "sdk-minimal", "--dump-default-config"],
            env={**os.environ, "DSH_HOME": str(self.home)},
            stdout=subprocess.DEVNULL, check=True,
        )
        plugin_link = self.home / "profiles" / "sdk-minimal" / "node_modules" / "dsh-memgarden"
        plugin_link.parent.mkdir(parents=True, exist_ok=True)
        if plugin_link.exists() or plugin_link.is_symlink():
            plugin_link.unlink()
        plugin_link.symlink_to(ROOT)

    def _write_patch(self, bin_path: str) -> None:
        patch = self.home / "profiles" / "sdk-minimal" / "cordis.patch.yml"
        patch.write_text(
            "- insert:\n"
            "    - id: memgarden\n"
            "      name: 'dsh-memgarden'\n"
            "      inject: [tools, llm]\n"
            "      config:\n"
            f"        bin: '{bin_path}'\n"
            f"        storage: 'sqlite:///{self.garden}'\n"
            f"        tenant: '{self.tenant}'\n"
            "        locale: 'zh-Hans'\n",
            encoding="utf-8",
        )

    def harness(self) -> DeepSeekHarness:
        os.environ["MEMGARDEN_DEBUG_LOG"] = str(self.log)
        return DeepSeekHarness(
            provider="deepseek-official", model="deepseek-v4-flash",
            max_tokens=4096, cwd=str(self.workspace),
            dsh_home=str(self.home), dsh_bin=str(self.dsh_bin),
            profile="sdk-minimal",
        )

    def cards(self) -> list[dict]:
        if not self.garden.exists():
            return []
        conn = sqlite3.connect(self.garden)
        try:
            return [json.loads(d) for (d,) in conn.execute("SELECT doc FROM cards")]
        finally:
            conn.close()

    def logs(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

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


def _memgarden_bin() -> str:
    found = os.environ.get("MEMGARDEN_BIN") or shutil.which("memgarden")
    if found:
        return found
    print("找不到 memgarden —— 先 pip install memgarden")
    sys.exit(2)


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
        check(len(cards) == 1, "轮末自动落卡", f"{len(cards)} 张")
        if cards:
            check("辣" in (cards[0].get("summary") or ""),
                  "卡的内容是那件事", cards[0].get("summary", ""))

        # 🔴 模型调用必须走 DSH：服务端没有 --model，能落卡就说明是宿主调的
        check("--model" not in env.logs(), "模型调用归 DSH（服务没有模型配置）")

        with env.harness() as h:
            r = h.run("晚饭吃什么？给一个具体建议，一句话。", session_id="B")
            reply = r.final_response or ""
        check(any(w in reply for w in ("辣", "清淡", "温和", "不辣")),
              "全新会话自动召回（模型未主动调工具）", reply[:40])
    finally:
        env.cleanup()


# --------------------------------------------------------------------------- #
# B. 模型主动调工具
# --------------------------------------------------------------------------- #

def group_b() -> None:
    print("\nB. 模型主动调工具")
    env = Env(tenant="bob")
    try:
        with env.harness() as h:
            h.run("请调用 memgarden_memory_write 工具，"
                  "把「周末要去看医生」记下来。", session_id="W")
        check("注册了" in env.logs(), "工具注册进了 DSH 的 Tool Registry",
              next((l for l in env.logs().splitlines() if "注册了" in l), ""))
        wrote = [c for c in env.cards() if "医生" in (c.get("summary") or "")]
        check(bool(wrote), "memory_write 真的落了库",
              wrote[0].get("summary", "") if wrote else "没找到")
    finally:
        env.cleanup()


# --------------------------------------------------------------------------- #
# C. 多 agent 隔离
# --------------------------------------------------------------------------- #

def group_c() -> None:
    print("\nC. 多 agent 隔离")
    a = Env(tenant="tenant-a")
    b = Env(tenant="tenant-b")
    try:
        with a.harness() as h:
            h.run("我对花生过敏。简短回一句。", session_id="A")
        check(bool(a.cards()), "A 记下了自己的事", f"{len(a.cards())} 张")

        # B 用**同一个花园库**，但 tenant 不同 —— 必须读不到 A 的
        b.garden.unlink(missing_ok=True)
        shutil.copy(a.garden, b.garden)
        with b.harness() as h:
            r = h.run("我有什么忌口吗？一句话。", session_id="B")
            reply = r.final_response or ""
        check("花生" not in reply, "另一个租户读不到（同一个库）", reply[:40])
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
                "params": {"scope": {"tenant_id": "t"}, "window": "x",
                           "locale": "zh-Hans"}})
    code = (out.get("error") or {}).get("code", "")
    check(code == "model_not_configured",
          "没配模型 → model_not_configured", code)

    # D4 不需要模型的方法照常可用
    out = _rpc({"id": "1", "method": "manifest.get", "params": {}})
    check(out.get("ok") is True, "不需要模型的方法不受影响")


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

def main() -> int:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("需要 DEEPSEEK_API_KEY")
        return 2

    print("=" * 66)
    print("DSH 验收 —— dsh 0.1.2-alpha.4 + memgarden")
    print("=" * 66)

    for group in (group_a, group_b, group_c, group_d):
        try:
            group()
        except Exception as exc:      # noqa: BLE001
            check(False, f"{group.__name__} 整组异常", repr(exc)[:120])

    print("\n" + "=" * 66)
    failed = [r for r in RESULTS if not r[0]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for _, name, detail in failed:
        print(f"  FAIL {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
