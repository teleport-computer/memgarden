"""历史导入「已有记忆索引」挑卡器评测：内置词面重叠（MG-8 初版） vs ``retrieval.rank``。

一批导入材料几千字、混着好几个话题。写卡模型只有看到相关旧卡已经在了，才会 merge 而不是再写一张。
这里用本目录的合成花园和带标注的查询拼出导入批次：随机取 ``k`` 条查询（单句集 + 多轮窗口集），
用与花园无关的闲聊行填到目标字数；这批的「应进索引」= 这些查询 ``must`` 的并集。
每批按 ``importing.select_index_cards``（60 个名额，四分之一留给重要度最高的卡）挑一次，量：

    card-recall       应进索引的卡进了的比例（按批平均）
    batches-complete  应进索引的卡全都进了的批次占比

    python evals/retrieval/import_index.py
    python evals/retrieval/import_index.py --tokenizer path/to/tok.py:TOK   # 宿主分词器（仓库外）

词面重叠的参考实现照抄自 MG-8 初版（``feat/import-session`` 9a302af），只用来对比。
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import pathlib
import random
import re
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
for path in (ROOT / "src", HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402
from memgarden.importing import bm25_index_ranker, select_index_cards  # noqa: E402

CHATTER = """用户：在吗
AI：在的，今天怎么样？
用户：还行吧，就是有点累
AI：累的话早点休息，别硬撑。
用户：嗯，晚点再说
AI：好，有事随时叫我。
用户：刚刚下楼买了杯奶茶
AI：什么口味的？
用户：随便点的，还挺好喝
AI：那不错，偶尔犒劳一下自己挺好的。
用户：今天天气好热
AI：记得多喝水，出门带把伞挡太阳。
用户：哈哈好
User: morning
AI: Good morning! Anything on your mind today?
User: not really, just checking in
AI: Happy to chat whenever you like.
用户：你觉得我是不是太拖延了
AI：每个人都会有拖延的时候，关键是找到让自己开始的小步子。
用户：说得对，那我先去忙了
AI：去吧，加油。""".splitlines()

# -- MG-8 初版的词面重叠（只做对照） -------------------------------------------------
_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_+#.-]*")


def _overlap_tokens(text: str) -> set[str]:
    lowered = str(text or "").casefold()
    tokens = {w for w in _ASCII_WORD.findall(lowered) if len(w) >= 2}
    run: list[str] = []
    for ch in lowered + " ":
        if "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿":
            run.append(ch)
            continue
        if len(run) == 1:
            tokens.add(run[0])
        else:
            tokens.update(run[i] + run[i + 1] for i in range(len(run) - 1))
        run = []
    return tokens


def overlap_ranker(text, cards):
    query = _overlap_tokens(text)
    scored = []
    for card in cards:
        fields = [card.get("summary"), card.get("content"), card.get("bucket"),
                  *(card.get("threads") or []), *(card.get("retrieval_cues") or [])]
        tokens = _overlap_tokens(" ".join(str(f) for f in fields if f))
        hit = len(query & tokens)
        if hit:
            scored.append((hit / math.sqrt(len(tokens)), -float(card.get("importance") or 0),
                           str(card["id"])))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    return [cid for _s, _i, cid in scored]


def _load_tokenizer(spec: str | None):
    if not spec:
        return None
    path, _, attr = spec.rpartition(":")
    module_spec = importlib.util.spec_from_file_location("external_tokenizer", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, attr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tokenizer", help="path/to/file.py:OBJECT with name + tokenize()")
    ap.add_argument("--batches", type=int, default=300)
    args = ap.parse_args(argv)

    cards = harness.load_garden()
    pool = harness.visible(cards)
    rng = random.Random(7)
    for card in pool:
        card.setdefault("importance", round(rng.random(), 2))
    queries = [q for name in ("single", "multi_turn")
               for q in harness.load_queries(cards, name) if q.get("must")]
    rankers = {"overlap (MG-8 初版)": overlap_ranker,
               "retrieval.rank": bm25_index_ranker(_load_tokenizer(args.tokenizer))}

    print(f"{len(pool)} cards · {len(queries)} labelled queries · index limit 60\n")
    print("| topics / chars | ranker | card-recall | batches-complete | p50 ms |")
    print("|---|---|---|---|---|")
    for k, target in ((1, 1500), (3, 3000), (4, 6000), (8, 6000)):
        rng.seed(k * 100 + target)
        batches = []
        for _ in range(args.batches):
            picked = rng.sample(queries, k)
            parts = [q["query"] for q in picked]
            while sum(len(p) for p in parts) < target:
                parts.insert(rng.randrange(len(parts) + 1), rng.choice(CHATTER))
            batches.append(("\n".join(parts), set().union(*(q["must"] for q in picked))))
        for name, ranker in rankers.items():
            recalls, times = [], []
            for text, must in batches:
                started = time.perf_counter()
                ids = {c["id"] for c in select_index_cards(pool, text, limit=60, ranker=ranker)}
                times.append((time.perf_counter() - started) * 1000)
                recalls.append(len(must & ids) / len(must))
            print(f"| {k} / {target} | {name} | {statistics.fmean(recalls):.3f} | "
                  f"{sum(r == 1 for r in recalls) / len(recalls):.2f} | "
                  f"{statistics.median(times):.1f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
