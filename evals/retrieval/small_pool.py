"""小花园评测：候选池只有几张卡时，门槛会不会把答案挡掉、会不会放进杂卡。

``harness.py`` 量的是 210 张卡的花园。新用户的花园只有 1–20 张，而覆盖率闸的 IDF 按候选池算：
池子越小，「花园里一张都没有的查询词」和「命中的词」的 IDF 差得越离谱，覆盖率被压到闸下。
这里用同一份语料和标注，拼出小候选池来量这件事，并给 ``retrieval.DEFAULT_COVERAGE_POOL_FLOOR``
定值。

三种池子（每个池子大小 × 若干随机种子）：

    answer  一张答案卡 + (N-1) 张与该查询无关的卡。答案卡在结果里 = 找到。
            只统计「在 210 张的完整花园里默认配置找得到」的（查询, 答案卡）对 ——
            换说法这类词法方法本来就拿不到的，不算小花园的锅。
    noise   N 张与该查询无关的卡（排除 must / should / must_not）。返回任何东西 = 误命中。
    trap    陷阱卡（must_not）+ 无关卡填到 N 张。陷阱卡被返回 = 说错。

    python evals/retrieval/small_pool.py
    python evals/retrieval/small_pool.py --floors 1,5,10,20,50 --sizes 1,2,3,5,10,20
    python evals/retrieval/small_pool.py --tokenizer path/to/tok.py:TOK   # 宿主分词器（仓库外）

内置两个分词器：``default``（``DefaultTokenizer``）和 ``cjk-words``，后者是词级分词器的**替身**
（CJK 只出相邻二字、不出单字，ASCII 标识符整段），用来近似 jieba 这类按词切的宿主分词器 ——
它不是 jieba，数字只说明方向。两条路：``search``（``rank`` 默认参数）和 ``recall``
（``select_context``，``strong_evidence=0.5``，即一个宿主为自动想起放宽强证据闸的配置）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import random
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
for path in (ROOT / "src", HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402
from memgarden import retrieval  # noqa: E402


class CjkWords:
    """词级分词器的替身：CJK 只出相邻二字（单字段出单字），ASCII 标识符整段，其余按词。"""

    name = "eval-cjk-words"
    _ascii = re.compile(r"([a-z0-9]+(?:[-_./][a-z0-9]+)*)")
    _segment = re.compile(r"([㐀-䶿一-鿿]+)|([^\W_㐀-䶿一-鿿]+)")

    def tokenize(self, text: str) -> list[str]:
        out: list[str] = []
        for index, part in enumerate(self._ascii.split(str(text or "").casefold())):
            if not part:
                continue
            if index % 2:
                out.append(part)
                continue
            for match in self._segment.finditer(part):
                run, word = match.group(1), match.group(2)
                if word:
                    out.append(word)
                elif len(run) == 1:
                    out.append(run)
                else:
                    out.extend(run[i:i + 2] for i in range(len(run) - 1))
        return out


TOKENIZERS = {"default": None, "cjk-words": CjkWords()}


def _search(query, pool, tokenizer, floor):
    return retrieval.rank(query, pool, tokenizer=tokenizer, limit=8,
                          coverage_pool_floor=floor).ids


def _recall(query, pool, tokenizer, floor):
    picked, _trace = retrieval.select_context(query, pool, tokenizer=tokenizer, cap=8,
                                              strong_evidence=0.5, coverage_pool_floor=floor)
    return [c["id"] for c in picked]


PATHS = {"search": _search, "recall": _recall}


def _load_tokenizer(spec: str):
    path, _, attr = spec.rpartition(":")
    module_spec = importlib.util.spec_from_file_location("external_tokenizer", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, attr)


def run(tokenizer, *, path: str, floors, sizes, seeds: int) -> dict:
    cards = harness.visible(harness.load_garden())
    by_id = {c["id"]: c for c in cards}
    queries = (harness.load_queries(harness.load_garden(), "single")
               + harness.load_queries(harness.load_garden(), "multi_turn"))
    fn = PATHS[path]
    # 基准：完整花园里默认配置找得到的（查询, 答案卡）对。
    pairs = []
    for q in queries:
        if q.get("expect_empty"):
            continue
        found = set(fn(q["query"], cards, tokenizer, retrieval.DEFAULT_COVERAGE_POOL_FLOOR))
        pairs += [(q, cid) for cid in q.get("must") or [] if cid in found and cid in by_id]

    def unrelated(q):
        banned = set(q.get("must") or []) | set(q.get("should") or []) | set(q.get("must_not") or [])
        return [c for c in cards if c["id"] not in banned]

    table: dict = {}
    for floor in floors:
        for size in sizes:
            found = total = noisy = noise_total = trapped = trap_total = 0
            for seed in range(seeds):
                rng = random.Random(f"{seed}:{size}")
                for q, cid in pairs:
                    others = unrelated(q)
                    pool = [by_id[cid]] + rng.sample(others, min(size - 1, len(others)))
                    rng.shuffle(pool)
                    total += 1
                    found += cid in fn(q["query"], pool, tokenizer, floor)
                for q in queries:
                    others = unrelated(q)
                    pool = rng.sample(others, min(size, len(others)))
                    noise_total += 1
                    noisy += bool(fn(q["query"], pool, tokenizer, floor))
                    traps = [by_id[t] for t in q.get("must_not") or [] if t in by_id][:size]
                    if traps:
                        pool = traps + rng.sample(others, max(0, size - len(traps)))
                        trap_total += 1
                        trapped += bool(set(t["id"] for t in traps)
                                        & set(fn(q["query"], pool, tokenizer, floor)))
            table[f"{floor}:{size}"] = {
                "floor": floor, "size": size,
                "answer_found": round(found / total, 3) if total else None,
                "noise_returned": round(noisy / noise_total, 3) if noise_total else None,
                "trap_returned": round(trapped / trap_total, 3) if trap_total else None,
                "n": {"answer": total, "noise": noise_total, "trap": trap_total},
            }
    return {"pairs": len(pairs), "table": table}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--floors", default="1,5,10,20,30,50")
    ap.add_argument("--sizes", default="1,2,3,5,10,20,50")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--paths", default="search,recall")
    ap.add_argument("--tokenizer", help="path/to/file.py:OBJECT with name + tokenize()")
    ap.add_argument("--json", help="write results JSON here")
    args = ap.parse_args(argv)
    floors = [int(x) for x in args.floors.split(",")]
    sizes = [int(x) for x in args.sizes.split(",")]
    tokenizers = dict(TOKENIZERS)
    if args.tokenizer:
        tok = _load_tokenizer(args.tokenizer)
        tokenizers = {getattr(tok, "name", "external"): tok}
    results = {}
    for tname, tok in tokenizers.items():
        for path in args.paths.split(","):
            res = run(tok, path=path, floors=floors, sizes=sizes, seeds=args.seeds)
            results[f"{tname}/{path}"] = res
            print(f"\n## {tname} · {path} · {res['pairs']} (query, answer) pairs\n")
            print("| floor \\ pool size | " + " | ".join(str(s) for s in sizes) + " |")
            print("|---|" + "---|" * len(sizes))
            for label, key in (("answer found", "answer_found"),
                               ("noise returned", "noise_returned"),
                               ("trap returned", "trap_returned")):
                for floor in floors:
                    row = [res["table"][f"{floor}:{s}"][key] for s in sizes]
                    print(f"| {label} · F={floor} | " + " | ".join(
                        "-" if v is None else f"{v:.3f}" for v in row) + " |")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
