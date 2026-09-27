"""Small real-model Capture/Dream probe. Synthetic data only; not a quality guarantee.

Run the SAME corpus against two source trees with --source-root for an A/B.
Missing credentials, transport failures, parsing failures, or lost values fail.
Passing token checks still requires human review of attribution and chronology.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).parent / "corpus" / "specifics.jsonl"


def ask(url: str, key: str, model: str, prompt: str, *, max_tokens: int,
        route: str | None) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": max_tokens}
    if route:
        body["provider"] = {"only": [route], "allow_fallbacks": False,
                            "require_parameters": True,
                            "max_price": {"prompt": 2, "completion": 5}}
        body["reasoning"] = {"enabled": False}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def judge(case: dict, cards: list[dict], error: str | None) -> dict:
    prose = "\n".join(str(c.get(k, "")) for c in cards for k in ("summary", "content"))
    folded = prose.casefold()
    missing = [group for group in case["required"]
               if not any(term.casefold() in folded for term in group)]
    lo, hi = case["count"]
    # A narrow synthetic-corpus guard, not a general factuality classifier.
    # Include cues: the first live probe invented 2025 only in retrieval_cues.
    year_pattern = r"(?<!\d)(?:19|20)\d{2}(?!\d)"
    source = case.get("window", "") + json.dumps(case.get("cards", []), ensure_ascii=False)
    generated = json.dumps(cards, ensure_ascii=False)
    invented_years = sorted(set(re.findall(year_pattern, generated)) -
                            set(re.findall(year_pattern, source)))
    checks = {"parsed": error is None, "count": lo <= len(cards) <= hi,
              "concrete_values": not missing, "grounded_years": not invented_years}
    return {"checks": checks, "missing": missing, "parse_error": error,
            "invented_years": invented_years}


def build_prompt(case: dict) -> str:
    from memgarden.prompts.capture import build_capture_prompt
    from memgarden.prompts.dream import build_dream_prompt

    common = dict(ai_name="Aster", user_name="Mina", locale=case["locale"])
    if case["lane"] == "capture":
        return build_capture_prompt(**common, buckets="", threads="", identity="",
                                    cards="", window=case["window"],
                                    policy="conversation_capture")
    # Synthetic complete cards; no clipping or storage adapter is involved.
    return build_dream_prompt(**common, cards=json.dumps(case["cards"], ensure_ascii=False),
                              recent_conversations=case["window"])


def evaluate(case: dict, reply: str) -> dict:
    from memgarden.prompts.capture import parse_capture_cards
    from memgarden.prompts.dream import parse_dream_consolidations

    if case["lane"] == "capture":
        cards, error = parse_capture_cards(reply, policy="conversation_capture", strict=True)
        return {**judge(case, cards, error), "cards": cards}
    proposals, questions, error = parse_dream_consolidations(
        reply, strict=True, known_ids=case["target_ids"])
    cards = [p["result"] for p in proposals]
    result = judge(case, cards, error)
    # A no-op or unrelated proposal cannot count as a successful merge probe.
    result["checks"]["targets"] = bool(proposals) and all(
        set(p["card_ids"]) == set(case["target_ids"]) for p in proposals)
    result["checks"]["operations"] = bool(proposals) and all(
        p["op"] in case["ops"] for p in proposals)
    return {**result, "consolidations": proposals, "questions_to_ask": questions}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", choices=("deepseek", "openrouter", "openai"), required=True)
    ap.add_argument("--model", required=True, help="Explicit provider model ID; never silently substituted")
    ap.add_argument("--source-root", type=Path, default=ROOT,
                    help="Repository tree whose prompts/parsers to test; corpus always comes from this runner")
    ap.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    ap.add_argument("--case", action="append", dest="case_ids",
                    help="Explicit supplemental case ID; preserve original failed report separately")
    ap.add_argument("--max-output-tokens", type=int, default=4096)
    ap.add_argument("--openrouter-provider", help="Pin routing, disable fallback/reasoning; cap USD/M input=2 output=5")
    args = ap.parse_args()
    cases = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]
    if args.case_ids:
        if set(args.case_ids) - {c["id"] for c in cases}:
            ap.error("unknown --case ID")
        cases = [c for c in cases if c["id"] in args.case_ids]
    if not 1 <= args.max_output_tokens <= 4096:
        ap.error("--max-output-tokens must be 1..4096 for this small probe")
    if args.openrouter_provider and args.provider != "openrouter":
        ap.error("--openrouter-provider requires --provider openrouter")
    source = args.source_root.resolve()
    if not (source / "src" / "memgarden").is_dir():
        ap.error("--source-root must contain src/memgarden")
    sys.path.insert(0, str(source / "src"))
    # Load the chosen tree before the shared transport imports capture's modules.
    from memgarden.prompts import capture, dream  # noqa: F401

    sys.path.insert(0, str(ROOT))
    from evals.capture import _ENDPOINTS

    url, env = _ENDPOINTS[args.provider]
    key = os.environ.get(env, "")
    if not key:
        print(f"ERROR: {env} not configured; no real-model acceptance evidence.")
        return 1
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                  text=True, timeout=10).strip()
    dirty = bool(subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True, timeout=10).strip())
    results = []
    for repeat in range(args.repeat):
        for case in cases:
            prompt = build_prompt(case)
            result = {"id": case["id"], "repeat": repeat + 1,
                      "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
            try:
                response = ask(url, key, args.model, prompt,
                               max_tokens=args.max_output_tokens, route=args.openrouter_provider)
                choice = response["choices"][0]
                reply = choice["message"]["content"]
                result.update(usage=response.get("usage"), model=response.get("model"),
                              provider=response.get("provider"), finish_reason=choice.get("finish_reason"))
                result.update(evaluate(case, reply))
                result["checks"]["complete"] = choice.get("finish_reason") == "stop"
            except Exception as exc:
                # Do not print response bodies, headers or credentials on failure.
                result.update(checks={"request": False}, error_type=type(exc).__name__)
            results.append(result)
            print(f"{case['id']} repeat {repeat + 1}: "
                  f"{'PASS' if all(result['checks'].values()) else 'FAIL'}", file=sys.stderr)
    failed = sum(not all(r["checks"].values()) for r in results)
    print(json.dumps({"provider": args.provider, "model": args.model, "temperature": 0,
                      "source_sha": sha, "source_dirty": dirty,
                      "max_output_tokens": args.max_output_tokens,
                      "openrouter_provider": args.openrouter_provider,
                      "reasoning_enabled": False if args.openrouter_provider else "provider default",
                      "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
                      "failed": failed, "manual_review_required": True, "results": results},
                     ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
