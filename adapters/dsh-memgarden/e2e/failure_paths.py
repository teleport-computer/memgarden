"""失败路径 —— 不烧模型额度的那些。

`dsh_acceptance.py` 跑的是真模型的正向场景（成本高、跑得慢）。
这里专跑坏情况，全部直接对 `memgarden serve` 说话，秒级、可以随便跑。

    握手      协议版本不兼容 → 宿主必须拒绝挂载,而不是带病继续
    超时      服务卡住不回 → 调用方要自己超时,不能永远挂着
    中途退出  capture 做到一半服务死了 → 报得清,且不留半张卡
    幂等      同一轮重放两次 → 只写一条,不写第二条
    并发      两轮快速连续到达 → 各自落各自的,不互相覆盖
    整理并发  整理与前台读同时跑 → 前台读不到被整理掉的中间态
    错误码    每种坏情况都有稳定的 code,宿主能分支处理
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
import threading
import time

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _bin() -> str:
    found = os.environ.get("MEMGARDEN_BIN") or shutil.which("memgarden")
    if not found:
        print("找不到 memgarden")
        sys.exit(2)
    return found


class Service:
    """一个长驻的 serve 子进程，按行说话。"""

    def __init__(self, *, storage: str | None = None, model: str | None = None) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="mg-fail-"))
        self.db = storage or f"sqlite:///{self.dir / 'g.db'}"
        cmd = [_bin(), "serve", "--storage", self.db]
        if model:
            cmd += ["--model", model]
        self.p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._n = 0

    def call(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        self._n += 1
        self.p.stdin.write(json.dumps(
            {"id": str(self._n), "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        box: list[str] = []
        t = threading.Thread(target=lambda: box.append(self.p.stdout.readline()),
                             daemon=True)
        t.start()
        t.join(timeout)
        if not box:
            return {"ok": False, "error": {"code": "timeout", "message": method}}
        return json.loads(box[0])

    def cards(self) -> list[dict]:
        path = self.db.replace("sqlite:///", "")
        if not pathlib.Path(path).exists():
            return []
        conn = sqlite3.connect(path)
        try:
            return [json.loads(d) for (d,) in conn.execute("SELECT doc FROM cards")]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:      # noqa: BLE001
            self.p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


SCOPE = {"tenant_id": "t1", "actor": {"user_id": "u1", "agent_id": "a1"},
         "allowed_mounts": ["agent-private"]}

# 一个假模型：吐固定的一张卡，不联网、不花钱。
# 用文件而不是内联字符串 —— 内联的引号转义在 shell / Python / JSON 三层里
# 极易写错，而写错的表现是「模型没吐出合法 JSON」，看起来像管线的 bug。
FAKE_MODEL = (
    sys.executable + " "
    + str(pathlib.Path(__file__).resolve().parent / "fixtures" / "fake_model.py")
)


# --------------------------------------------------------------------------- #

def t_handshake() -> None:
    print("\n握手")
    s = Service()
    try:
        m = s.call("manifest.get", {})["result"]
        check(m.get("protocol_version") == "1", "manifest 报出协议版本",
              str(m.get("protocol_version")))
        # 宿主端的检查逻辑就是「不等于自己认识的版本就拒绝挂载」。
        # 这里验的是：这个字段确实存在且稳定 —— 它不存在的话，
        # 宿主根本没有依据去拒绝，只能带病继续。
        for key in ("component_version", "capabilities", "mounts", "operations"):
            check(key in m, f"manifest 有 {key}",
                  str(m.get(key))[:50] if key != "operations" else
                  f"{len(m.get('operations', []))} 个方法")
    finally:
        s.close()


def t_unknown_method() -> None:
    print("\n未知方法")
    s = Service()
    try:
        out = s.call("没有这个方法", {})
        check(out.get("error", {}).get("code") == "unknown_method",
              "未知方法 → unknown_method", str(out.get("error")))
    finally:
        s.close()


def t_bad_input() -> None:
    print("\n坏输入")
    s = Service()
    try:
        out = s.call("context.get", {"scope": {}, "query": "x"})
        code = out.get("error", {}).get("code", "")
        check(not out.get("ok") and code in ("invalid_request", "mount_not_allowed"),
              "scope 缺 tenant_id → 拒绝而不是当成空租户", code)

        # 🔴 最关键的一条：越权挂载点必须被拒（而不是被悄悄忽略 ——
        # 悄悄忽略的话，宿主以为自己把这一轮收窄到了 shared，实际读了全部）
        out = s.call("context.get", {
            "scope": {"tenant_id": "t1", "actor": {"user_id": "u1", "agent_id": "a1"},
                      "allowed_mounts": ["agent-private"]},
            "query": "x", "mount": "shared",
        })
        code = out.get("error", {}).get("code", "")
        check(not out.get("ok") and code == "mount_not_allowed",
              "读没被授权的挂载点 → mount_not_allowed", code)
    finally:
        s.close()


def t_capture_session_lifecycle() -> None:
    print("\ncapture 会话生命周期")
    s = Service()
    try:
        out = s.call("capture.feed", {"session_id": "不存在", "reply": "x"})
        code = out.get("error", {}).get("code", "")
        check(code == "unknown_session",
              "喂不存在的会话 → unknown_session（不是 internal_error）", code)

        out = s.call("capture.run", {"scope": SCOPE, "window": "x", "locale": "zh-Hans"})
        code = out.get("error", {}).get("code", "")
        check(code == "model_not_configured",
              "没配模型时调 capture.run → model_not_configured", code)

        # 开一个会话然后取消 —— 取消后不能还能喂
        b = s.call("capture.begin", {"scope": SCOPE, "window": "用户：我爱喝美式",
                                     "locale": "zh-Hans"})["result"]
        sid = b["session_id"]
        check(b["status"] == "needs_model" and bool(b.get("next_prompt")),
              "begin 返回「该问模型什么」", b["status"])
        s.call("capture.cancel", {"session_id": sid})
        out = s.call("capture.feed", {"session_id": sid, "reply": "{}"})
        check(out.get("error", {}).get("code") == "unknown_session",
              "取消后再喂 → unknown_session", str(out.get("error", {}).get("code")))
        check(not s.cards(), "取消的会话不留半张卡", f"{len(s.cards())} 张")
    finally:
        s.close()


def t_mid_flight_exit() -> None:
    print("\ncapture 中途服务退出")
    s = Service()
    db = s.db
    try:
        b = s.call("capture.begin", {"scope": SCOPE, "window": "用户：我爱喝美式",
                                     "locale": "zh-Hans"})["result"]
        check(b["status"] == "needs_model", "会话开起来了")
        s.p.kill()              # 模型还没回来，服务就没了
        s.p.wait(timeout=10)
        check(not s.cards(), "中途死掉不留半张卡", f"{len(s.cards())} 张")

        # 重启后拿旧 session_id 喂 → 必须是可分辨的 unknown_session，
        # 宿主据此重新 begin，而不是把这一轮记忆丢掉
        s2 = Service(storage=db)
        try:
            out = s2.call("capture.feed", {"session_id": b["session_id"], "reply": "{}"})
            check(out.get("error", {}).get("code") == "unknown_session",
                  "服务重启后旧会话 → unknown_session（宿主知道该重开）",
                  str(out.get("error", {}).get("code")))
        finally:
            s2.close()
    finally:
        shutil.rmtree(s.dir, ignore_errors=True)


def t_idempotency() -> None:
    print("\n幂等：同一轮重放两次")
    s = Service(model=FAKE_MODEL)
    try:
        req = {"scope": SCOPE, "window": "用户：我爱喝美式", "locale": "zh-Hans",
               "idempotency_key": "t1:dsh:7"}
        r1 = s.call("capture.run", req)
        check(r1.get("ok"), "第一次落卡", str(r1.get("result", {}).get("written")))
        n1 = len(s.cards())
        r2 = s.call("capture.run", req)          # 一模一样地重放
        n2 = len(s.cards())
        check(r2.get("ok") and n2 == n1,
              "重放同一个幂等键不写第二条", f"{n1} → {n2} 张")
    finally:
        s.close()


def t_two_turns_back_to_back() -> None:
    print("\n两轮快速连续到达")
    s = Service(model=FAKE_MODEL)
    try:
        outs = []
        threads = [
            threading.Thread(target=lambda i=i: outs.append(s.call("capture.run", {
                "scope": SCOPE, "window": f"用户：第 {i} 件事", "locale": "zh-Hans",
                "idempotency_key": f"t1:dsh:{i}",
            }, timeout=60)))
            for i in (1, 2)
        ]
        # 注意：serve 是单线程按行处理的，这里验的是「两个请求挤在管子里
        # 不会串台」——回复要各自对上各自的 id。
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join(timeout=90)
        check(len(outs) == 2 and all(o.get("ok") for o in outs),
              "两轮都处理了，没有互相吃掉", f"{len(outs)} 个回复")
        ids = {o.get("id") for o in outs}
        check(len(ids) == 2, "回复的 id 各归各的（没串台）", str(sorted(ids)))
    finally:
        s.close()


def t_maintenance_vs_foreground() -> None:
    print("\n整理与前台读并发")
    s = Service(model=FAKE_MODEL)
    try:
        s.call("capture.run", {"scope": SCOPE, "window": "用户：我爱喝美式",
                               "locale": "zh-Hans", "idempotency_key": "k1"})
        before = len(s.cards())
        check(before > 0, "先有一张卡", f"{before} 张")

        errors: list[str] = []

        def read_loop() -> None:
            for _ in range(6):
                out = s.call("context.get", {"scope": SCOPE, "query": "喝什么",
                                             "limit": 5}, timeout=30)
                if not out.get("ok"):
                    errors.append(str(out.get("error")))

        r = threading.Thread(target=read_loop)
        r.start()
        m = s.call("maintenance.check", {"scope": SCOPE}, timeout=60)
        r.join(timeout=60)
        check(not errors, "整理进行时前台读没报错", errors[0] if errors else "")
        check(m.get("ok"), "整理检查本身没崩", str(m.get("error") or "ok"))
        check(len(s.cards()) >= before, "前台读不到「卡被删了一半」的中间态",
              f"{before} → {len(s.cards())} 张")
    finally:
        s.close()


def t_slow_service_timeout() -> None:
    print("\n服务卡住")
    # 一个永远不回话的「服务」：调用方必须自己超时，不能永远挂着
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(300)"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        start = time.time()
        box: list[str] = []
        t = threading.Thread(target=lambda: box.append(p.stdout.readline()), daemon=True)
        t.start()
        t.join(3.0)
        check(not box and time.time() - start < 5,
              "服务不回话时调用方按时超时（不会永远挂着）",
              f"{time.time() - start:.1f}s")
    finally:
        p.kill()


# --------------------------------------------------------------------------- #

def main() -> int:
    print("=" * 66)
    print("失败路径 —— 不需要真模型")
    print("=" * 66)
    for fn in (t_handshake, t_unknown_method, t_bad_input,
               t_capture_session_lifecycle, t_mid_flight_exit, t_idempotency,
               t_two_turns_back_to_back, t_maintenance_vs_foreground,
               t_slow_service_timeout):
        try:
            fn()
        except Exception as exc:      # noqa: BLE001
            check(False, f"{fn.__name__} 整组异常", repr(exc)[:160])

    print("\n" + "=" * 66)
    failed = [r for r in RESULTS if not r[0]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for _, name, detail in failed:
        print(f"  FAIL {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
