"""召回排序评测 —— 同一份花园、同一组查询，比不同的排序实现。

和 ``evals/recall.py`` 的分工：

    recall.py            守**默认选卡策略**（转折/最近/相关三段）不退步，进 CI 发布闸
    retrieval/harness.py 比**排序算法本身**：谁在第几位、没命中时是不是真返回空、多快

后者是给「自动想起与主动搜索统一到一个排序器」这件事准备的量尺：先把现有两套
（内核的相关性打分、宿主侧的 BM25 分词排序）量出基线，再换实现时逐项对比。
它不进发布闸，没有阈值 —— 数字是给人做决定用的，不是给 CI 判红绿的。

## 跑法

    python evals/retrieval/harness.py                          # 内置两个 ranker
    python evals/retrieval/harness.py --ranker path/to/x.py:rank --name my-bm25
    python evals/retrieval/harness.py --json out.json          # 存结果
    python evals/retrieval/harness.py --compare a.json b.json  # 并排对比

外部 ranker 签名（和内核无关，宿主可以把自己的实现接进来量）::

    def rank(query: str, cards: list[dict], k: int) -> list[str]:
        '''cards 是宿主可见的候选（已过生命周期过滤），返回按相关性排好的卡 id。'''

## 指标

    recall@k     must 里的卡进前 k 的比例（按查询平均）
    hit@k        前 k 里至少有一张 must 的查询占比
    MRR@8        第一张 must 卡的名次倒数（前 8 名以外记 0）
    precision@k  前 k 里 must∪should 的占比（分母是实际返回数；返回空的查询不计入）
    violations   must_not 进前 8 的次数 —— 陷阱卡或已被取代的旧卡被说出来
    trap_above_answer  陷阱卡排在第一张 must 之前（或只来了陷阱）的查询数 —— 比 violations 更接近「说错」
    no-hit       期望为空的查询真的返回空的条数
    false-empty  有 must 的查询却一张都没返回
    latency      每条查询取多次运行的中位数，再报 p50 / p95 / max

语料纯虚构，见 README.md。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import statistics
import sys
import time
from typing import Callable

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filler  # noqa: E402
from memgarden.prompts.recall_fields import retrieval_cues  # noqa: E402
from memgarden import retrieval  # noqa: E402
from memgarden.scoring import relevance  # noqa: E402

KS = (1, 3, 5, 8)
MAX_K = max(KS)
Ranker = Callable[[str, list, int], list]


# --------------------------------------------------------------------------- #
# 语料
# --------------------------------------------------------------------------- #

def _jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def load_garden() -> list[dict]:
    cards = _jsonl(HERE / "cards.jsonl") + filler.generate()
    ids = [c["id"] for c in cards]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate card ids: {sorted(dupes)}")
    return cards


#: 查询集。``single``：一句话查询（主动搜索、单轮召回）；``multi_turn``：按 io 自动想起
#: 的方式把最近几条对话拼成查询（见 :func:`chat_window_query`）。
QUERY_SETS = {"single": "queries.jsonl", "multi_turn": "queries_multi_turn.jsonl"}

#: io 自动想起取窗口时认的角色（``backend/enclave/routes/chat.py::_build_context_memories``）。
CHAT_ROLES = frozenset({"user", "human", "assistant", "agent", "openclaw"})
CHAT_WINDOW = 4


def chat_window_query(messages: list[dict]) -> str:
    """照抄 io 的自动想起查询：最近 4 条非空 user/assistant 消息按时间顺序用换行拼起来。

    含上一条 AI 回复；第 5 条及更早的消息掉出窗口。改 io 那边的构造时这里要跟着改，
    否则这组数字量的就不是线上的查询。
    """
    recent = [m["content"] for m in messages
              if m.get("role") in CHAT_ROLES
              and isinstance(m.get("content"), str) and m["content"].strip()][-CHAT_WINDOW:]
    return "\n".join(recent)


def load_queries(cards: list[dict], query_set: str = "single") -> list[dict]:
    queries = _jsonl(HERE / QUERY_SETS[query_set])
    for q in queries:
        if "messages" in q:
            if "query" in q:
                raise ValueError(f"{q['qid']}: give either query or messages, not both")
            q["query"] = chat_window_query(q["messages"])
    known = {c["id"] for c in cards}
    problems = []
    for q in queries:
        if not str(q.get("why") or "").strip():
            problems.append(f"{q['qid']}: missing why")
        for field in ("must", "should", "must_not", "related"):
            for cid in q.get(field) or []:
                if cid not in known:
                    problems.append(f"{q['qid']}: {field} references unknown card {cid}")
        if q.get("expect_empty") and q.get("must"):
            problems.append(f"{q['qid']}: expect_empty with must labels")
        if not q.get("expect_empty") and not q.get("must"):
            problems.append(f"{q['qid']}: no must labels and not expect_empty")
    if problems:
        raise ValueError("corpus problems:\n  " + "\n  ".join(problems))
    return queries


def is_retired(card: dict) -> bool:
    """宿主的生命周期过滤。排序器只该看到仍在流通的卡 —— 两个 ranker 用同一道闸。"""
    return (str(card.get("status") or "active") in {"superseded", "archived", "deleted"}
            or bool(card.get("superseded_by")))


def visible(cards: list[dict]) -> list[dict]:
    return [c for c in cards if not is_retired(c)]


def corpus_fingerprint(cards: list[dict], queries: list[dict]) -> str:
    h = hashlib.sha256()
    for item in cards + queries:
        h.update(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 内置 ranker
# --------------------------------------------------------------------------- #

def garden_card(card: dict) -> dict:
    """宿主交给内核的规范形状：summary/content/bucket + 显式 search_text（含检索线索）。"""
    cues = retrieval_cues(card.get("retrieval_cues"))
    search = " ".join(p for p in (card.get("summary"), card.get("content"), *cues) if p)
    out = {k: card[k] for k in ("id", "summary", "content", "bucket", "threads",
                                "occurred_at", "created_at") if k in card}
    out["roles"] = list(card.get("roles") or [])
    out["search_text"] = search
    return out


def mg_relevant(query: str, cards: list[dict], k: int) -> list[str]:
    """内核现行的自动想起：``select_relevant_context_memories_with_trace``（门槛 0.35 + 软配额）。"""
    picked, _trace = relevance.select_relevant_context_memories_with_trace(
        [garden_card(c) for c in cards], query, cap=k)
    return [str(c["id"]) for c in picked]


def mg_scores(query: str, cards: list[dict], k: int) -> list[str]:
    """诊断用：同一套打分，不设门槛不分桶，纯按分数排。用来区分「分不准」还是「门槛挡掉」。"""
    scored = []
    for c in cards:
        rel = relevance.memory_relevance_details(query, garden_card(c))
        if rel["score"] > 0:
            scored.append((rel["score"], str(c.get("occurred_at") or ""), str(c["id"])))
    scored.sort(reverse=True)
    return [cid for _s, _o, cid in scored[:k]]


def mg_bm25(query: str, cards: list[dict], k: int) -> list[str]:
    """``memgarden.retrieval.rank``：统一排序器，默认分词器和默认参数。"""
    return retrieval.rank(query, cards, limit=k).ids


def mg_select(query: str, cards: list[dict], k: int) -> list[str]:
    """``memgarden.retrieval.select_context``：统一后的自动想起（同一把尺子 + 软配额，cap = k）。

    卡片文本用 ``default_search_text``（和 mg-bm25 一样含 bucket / threads / cues），
    只多带 ``roles`` —— 这样和 mg-bm25 的差异只来自软配额，不来自投影。
    """
    pool = [{**c, "roles": list(c.get("roles") or [])} for c in cards]
    picked, _trace = retrieval.select_context(query, pool, cap=k)
    return [str(c["id"]) for c in picked]


def mg_select_scaled(query: str, cards: list[dict], k: int) -> list[str]:
    """同 ``mg-select``，但打开按查询长度放大的强证据闸（``strong_evidence_terms=8``）。

    给多轮窗口查询量的：默认闸在长查询上几乎全部放行，见 README「多轮窗口」。
    """
    pool = [{**c, "roles": list(c.get("roles") or [])} for c in cards]
    picked, _trace = retrieval.select_context(query, pool, cap=k, strong_evidence_terms=8)
    return [str(c["id"]) for c in picked]


BUILTIN: dict[str, Ranker] = {"mg-relevant": mg_relevant, "mg-scores": mg_scores,
                              "mg-bm25": mg_bm25, "mg-select": mg_select,
                              "mg-select-scaled": mg_select_scaled}


def load_external(spec: str) -> Ranker:
    path, _, attr = spec.rpartition(":")
    if not path:
        raise SystemExit("--ranker expects path/to/file.py:function")
    module_spec = importlib.util.spec_from_file_location("external_ranker", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, attr)


# --------------------------------------------------------------------------- #
# 评测
# --------------------------------------------------------------------------- #

def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def evaluate(ranker: Ranker, cards: list[dict], queries: list[dict], *, repeats: int = 3) -> dict:
    pool = visible(cards)
    per_query, latencies = [], []
    for q in queries:
        runs, result = [], []
        for _ in range(max(1, repeats)):
            started = time.perf_counter()
            result = list(ranker(q["query"], pool, MAX_K))[:MAX_K]
            runs.append((time.perf_counter() - started) * 1000.0)
        latency = statistics.median(runs)
        latencies.append(latency)
        must, should = set(q.get("must") or []), set(q.get("should") or [])
        must_not = set(q.get("must_not") or [])
        row = {"qid": q["qid"], "category": q["category"], "returned": result,
               "latency_ms": round(latency, 3)}
        if must:
            first = next((i for i, cid in enumerate(result, 1) if cid in must), None)
            row["mrr"] = round(1.0 / first, 4) if first else 0.0
            for k in KS:
                top = result[:k]
                row[f"recall@{k}"] = round(len(must & set(top)) / len(must), 4)
                row[f"hit@{k}"] = int(bool(must & set(top)))
                row[f"precision@{k}"] = (round(len((must | should) & set(top)) / len(top), 4)
                                         if top else None)
            row["missed@8"] = sorted(must - set(result))
        row["violations"] = sorted(must_not & set(result))
        first_trap = next((i for i, cid in enumerate(result, 1) if cid in must_not), None)
        first_must = next((i for i, cid in enumerate(result, 1) if cid in must), None)
        row["trap_above_answer"] = int(bool(first_trap) and (not first_must or first_trap < first_must))
        if q.get("expect_empty"):
            row["no_hit_correct"] = int(not result)
        per_query.append(row)
    return {"per_query": per_query, "summary": summarize(per_query, latencies),
            "pool_size": len(pool)}


def _mean(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(values) / len(values), 4) if values else None


def summarize(rows: list[dict], latencies: list[float]) -> dict:
    labelled = [r for r in rows if "mrr" in r]
    empty_q = [r for r in rows if "no_hit_correct" in r]
    out = {
        "queries": len(rows),
        "labelled_queries": len(labelled),
        "mrr@8": _mean(labelled, "mrr"),
        "violations": sum(len(r["violations"]) for r in rows),
        "trap_above_answer": sum(r["trap_above_answer"] for r in rows),
        "no_hit_correct": f"{sum(r['no_hit_correct'] for r in empty_q)}/{len(empty_q)}",
        "false_empty": sum(1 for r in labelled if not r["returned"]),
        "avg_returned": round(sum(len(r["returned"]) for r in rows) / max(1, len(rows)), 2),
        "latency_ms": {"p50": round(_percentile(latencies, 50), 3),
                       "p95": round(_percentile(latencies, 95), 3),
                       "max": round(max(latencies or [0.0]), 3)},
    }
    for k in KS:
        out[f"recall@{k}"] = _mean(labelled, f"recall@{k}")
        out[f"hit@{k}"] = _mean(labelled, f"hit@{k}")
        out[f"precision@{k}"] = _mean(labelled, f"precision@{k}")
    categories = {}
    for cat in dict.fromkeys(r["category"] for r in rows):
        sub = [r for r in labelled if r["category"] == cat]
        if sub:
            categories[cat] = {"n": len(sub), "recall@5": _mean(sub, "recall@5"),
                               "hit@8": _mean(sub, "hit@8"), "mrr@8": _mean(sub, "mrr")}
    out["by_category"] = categories
    return out


def scale_latency(ranker: Ranker, cards: list[dict], queries: list[dict], size: int) -> dict:
    """把可见卡复制到 ``size`` 张（改 id，文本不变），只量延迟。复制会抬高重复度，不看质量。"""
    base = visible(cards)
    pool = []
    i = 0
    while len(pool) < size:
        src = base[i % len(base)]
        pool.append({**src, "id": f"{src['id']}~{i // len(base)}"})
        i += 1
    times = []
    for q in queries:
        started = time.perf_counter()
        ranker(q["query"], pool, MAX_K)
        times.append((time.perf_counter() - started) * 1000.0)
    return {"cards": size, "p50": round(_percentile(times, 50), 2),
            "p95": round(_percentile(times, 95), 2), "total_ms": round(sum(times), 1)}


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def print_compare(results: list[dict]) -> None:
    names = [r["name"] for r in results]
    print("| metric | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    keys = ["recall@1", "recall@3", "recall@5", "recall@8", "hit@8", "mrr@8",
            "precision@3", "precision@8", "violations", "trap_above_answer", "no_hit_correct", "false_empty",
            "avg_returned"]
    for key in keys:
        print(f"| {key} | " + " | ".join(_fmt(r["summary"].get(key)) for r in results) + " |")
    print("| latency p50 / p95 ms | " + " | ".join(
        f"{r['summary']['latency_ms']['p50']:.2f} / {r['summary']['latency_ms']['p95']:.2f}"
        for r in results) + " |")
    sizes = sorted({s["cards"] for r in results for s in r.get("scale", [])})
    for size in sizes:
        cells = []
        for r in results:
            hit = next((s for s in r.get("scale", []) if s["cards"] == size), None)
            cells.append(f"{hit['p50']:.1f} / {hit['p95']:.1f}" if hit else "-")
        print(f"| latency @{size} cards p50 / p95 ms | " + " | ".join(cells) + " |")
    cats = list(dict.fromkeys(c for r in results for c in r["summary"]["by_category"]))
    print("\n| category (n) recall@5 / MRR | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for cat in cats:
        n = next(r["summary"]["by_category"][cat]["n"] for r in results
                 if cat in r["summary"]["by_category"])
        cells = []
        for r in results:
            c = r["summary"]["by_category"].get(cat)
            cells.append(f"{_fmt(c['recall@5'])} / {_fmt(c['mrr@8'])}" if c else "-")
        print(f"| {cat} ({n}) | " + " | ".join(cells) + " |")


def print_misses(result: dict) -> None:
    print(f"\n{result['name']}: per-query problems")
    for row in result["per_query"]:
        notes = []
        if row.get("missed@8"):
            notes.append(f"missed {row['missed@8']}")
        if row["violations"]:
            notes.append(f"VIOLATION {row['violations']}")
        if row.get("no_hit_correct") == 0:
            notes.append(f"expected empty, got {len(row['returned'])}")
        if notes:
            print(f"  [{row['qid']} {row['category']}] " + "; ".join(notes)
                  + f"  -> {row['returned'][:5]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ranker", action="append", default=[],
                    help="builtin name or path/to/file.py:function (repeatable)")
    ap.add_argument("--name", action="append", default=[], help="label for each --ranker")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--scale", default="500,1000", help="extra latency-only pool sizes")
    ap.add_argument("--json", help="write results JSON here")
    ap.add_argument("--misses", action="store_true", help="print per-query misses")
    ap.add_argument("--compare", nargs="+", help="print a table from saved result files")
    ap.add_argument("--set", choices=sorted(QUERY_SETS), default="single",
                    help="query set: single-sentence queries or multi-turn chat windows")
    args = ap.parse_args(argv)

    if args.compare:
        loaded = []
        for path in args.compare:
            data = json.loads(pathlib.Path(path).read_text("utf-8"))
            loaded.extend(data["results"] if "results" in data else [data])
        print_compare(loaded)
        return 0

    cards = load_garden()
    queries = load_queries(cards, args.set)
    specs = args.ranker or list(BUILTIN)
    results = []
    for index, spec in enumerate(specs):
        ranker = BUILTIN.get(spec) or load_external(spec)
        name = args.name[index] if index < len(args.name) else spec
        result = {"name": name, **evaluate(ranker, cards, queries, repeats=args.repeats)}
        result["scale"] = [scale_latency(ranker, cards, queries, int(size))
                           for size in str(args.scale).split(",") if size.strip()]
        results.append(result)

    meta = {"query_set": args.set,
            "corpus_fingerprint": corpus_fingerprint(cards, queries), "cards": len(cards),
            "visible_cards": len(visible(cards)), "queries": len(queries),
            "python": sys.version.split()[0]}
    print(f"garden {meta['cards']} cards ({meta['visible_cards']} visible) · "
          f"{meta['queries']} queries · fingerprint {meta['corpus_fingerprint']}\n")
    print_compare(results)
    if args.misses:
        for result in results:
            print_misses(result)
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=1) + "\n",
            "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
