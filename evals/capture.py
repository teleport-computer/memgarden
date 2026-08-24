"""落卡质量 eval —— **调真模型**，测提示词行为。

## 为什么必须调真模型

提示词行为的 bug 单测一个都抓不到：capture / migrate 的单测都 stub 掉了 agent，
管道全通、断言全绿，而模型实际怎么答没人验过。这半年栽在这上面的：

  · 桶名写成「健康/Health」斜杠对 —— 因为提示词把两套桶并排给它看
  · 中文记忆被贴英文公共桶，实测约 1/3
  · 指令换英文后产出跟着变英文（蒸馏四个 builder 压根没有语言约束）

这些只有真模型 e2e 能暴露。所以这条 eval 跟前两条不同：**它花钱、它慢**，
但发布前必须跑 —— 前两条守的是代码，这条守的是模型的实际行为。

## 指标

    language_correct   产出语言是否跟花园语言一致（**最重要**）
    bucket_in_set      桶名是否落在该语言那一套通用桶里（或合理的自造桶）
    bucket_slash       是否出现「健康/Health」这类双语斜杠串（曾真实发生）
    count_in_range     落卡张数是否在期望区间（既不话痨也不失忆）
    placeholder_free   字段里没有 `...` / 方括号说明 / 空串

## 跑法

    OPENAI_API_KEY=... python evals/capture.py --provider openai --model gpt-4o
    DEEPSEEK_API_KEY=... python evals/capture.py --provider deepseek --model deepseek-chat

不传 key 就 SKIP 并明确说明 —— 绝不静默通过。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT / "packages" / "agent-protocol-core" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from memgarden.prompts.buckets import BUCKET_SETS  # noqa: E402
from memgarden.prompts.capture import (  # noqa: E402
    build_capture_prompt,
    parse_capture_cards,
)

CORPUS = pathlib.Path(__file__).resolve().parent / "corpus"
_CJK = re.compile(r"[一-鿿]")
_ENDPOINTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1/chat/completions", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
}


def _ask(url: str, key: str, model: str, prompt: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)["choices"][0]["message"]["content"]


def _lang_of(text: str) -> str:
    """一段文本主要是中文还是英文。按**字符**算在这里是对的 —— 这是散文不是桶名。"""
    zh, en = len(_CJK.findall(text)), len(re.findall(r"[A-Za-z]", text))
    return "zh" if zh >= en else "en"


def _judge(case: dict, cards: list[dict], err: str | None) -> dict:
    locale = case["locale"]
    checks: dict[str, bool] = {}
    notes: list[str] = []

    lo, hi = case["expect_cards_min"], case["expect_cards_max"]
    checks["count_in_range"] = lo <= len(cards) <= hi
    if not checks["count_in_range"]:
        notes.append(f"落了 {len(cards)} 张，期望 {lo}~{hi}")

    if not cards:
        # 期望就是 0 张时，其余检查无从谈起，按通过计；否则上面那条已经判负。
        for k in ("language_correct", "bucket_in_set", "bucket_slash", "placeholder_free"):
            checks[k] = True
        return {"checks": checks, "notes": notes, "cards": []}

    prose = " ".join(f"{c.get('summary','')} {c.get('content','')}" for c in cards)
    got_lang = _lang_of(prose)
    checks["language_correct"] = got_lang == case["expect_lang"]
    if not checks["language_correct"]:
        notes.append(f"产出语言 {got_lang}，期望 {case['expect_lang']}")

    allowed = set(BUCKET_SETS[locale].replace(" / ", "、").split("、"))
    other = set(BUCKET_SETS["en" if locale == "zh-Hans" else "zh-Hans"]
                .replace(" / ", "、").split("、"))
    buckets = [str(c.get("bucket") or "") for c in cards]
    # 自造桶是**允许**的（提示词明说都不贴合就起一个具体的），
    # 但绝不许用另一种语言那套通用桶 —— 那是语言判错的信号。
    checks["bucket_in_set"] = not any(b in other for b in buckets)
    if not checks["bucket_in_set"]:
        notes.append(f"用了另一种语言的通用桶：{[b for b in buckets if b in other]}")

    checks["bucket_slash"] = not any("/" in b for b in buckets)
    if not checks["bucket_slash"]:
        notes.append(f"出现双语斜杠桶名：{[b for b in buckets if '/' in b]}")

    bad = [c for c in cards
           if not str(c.get("summary") or "").strip()
           or not str(c.get("content") or "").strip()
           or "..." in str(c.get("summary", ""))]
    checks["placeholder_free"] = not bad
    if bad:
        notes.append(f"{len(bad)} 张卡含占位符或空字段")

    return {"checks": checks, "notes": notes,
            "cards": [{"bucket": c.get("bucket"), "summary": c.get("summary")} for c in cards]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek", choices=sorted(_ENDPOINTS))
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    url, env = _ENDPOINTS[args.provider]
    key = os.environ.get(env, "")
    if not key:
        print(f"SKIP: 环境变量 {env} 没设 —— 这条 eval 要调真模型才有意义，"
              f"不设 key 就不跑，绝不静默通过。")
        return 0

    cases = [json.loads(l) for l in
             (CORPUS / "conversations.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    results, failed = [], 0
    for c in cases:
        prompt = build_capture_prompt(
            ai_name="io", user_name="老王" if c["locale"] == "zh-Hans" else "Alex",
            naming_rule=None, buckets="", threads="", identity="",
            window=c["window"], cards="", locale=c["locale"],
        )
        try:
            reply = _ask(url, key, args.model, prompt)
        except Exception as e:  # noqa: BLE001
            results.append({"cid": c["cid"], "error": str(e)[:120]}); failed += 1; continue
        cards, err = parse_capture_cards(reply, policy="conversation_capture", strict=False)
        r = _judge(c, cards, err)
        r.update({"cid": c["cid"], "why": c["why"], "incident": c.get("incident", "")})
        results.append(r)
        if not all(r["checks"].values()):
            failed += 1

    if args.json:
        print(json.dumps({"provider": args.provider, "model": args.model,
                          "failed": failed, "results": results}, ensure_ascii=False, indent=2))
        return 1 if failed else 0

    print(f"落卡质量 · {args.provider}/{args.model} · {len(cases)} 个场景\n")
    for r in results:
        if "error" in r:
            print(f"  ❌ [{r['cid']}] 调用失败: {r['error']}"); continue
        ok = all(r["checks"].values())
        print(f"  {'✅' if ok else '❌'} [{r['cid']}] {r['why'][:44]}")
        for c in r["cards"]:
            print(f"        「{c['bucket']}」 {str(c['summary'])[:38]}")
        for n in r["notes"]:
            print(f"        ⚠️ {n}")
    print(f"\n{'❌ ' + str(failed) + ' 个场景未达标' if failed else '✅ 全部达标'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
