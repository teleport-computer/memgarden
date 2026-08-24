"""挑卡质量 eval —— 确定性，不调模型，可进 CI。

## 为什么需要它

记忆系统这半年出的 bug 有一个共同形状：**管道是通的，输出是错的**。
单测全绿，只有真实数据能暴露：

  · 新形状的卡算不出摘要 → 整批进不了候选池，用户问「我有一只狗吗」答记忆里没有
  · 翻译层只带 summary/content → 老卡标题退出匹配范围，拿标题里的词召不回
  · 敏感闸把每张卡硬写成不敏感 → 标记敏感的记忆在闲聊里照样被喂给模型

这些都不是接线错误，是**召回质量**问题，而召回质量此前**一个数字都没有**。
「挑得准不准」全靠线上体感 —— 这条 eval 就是把体感换成数字。

## 指标

    recall      must 里的卡有几张真被召回（漏一张就是用户「想不起来」）
    precision   召回的卡里有多少是该来的
    violations  must_not 里的卡被召回了几次（错误注入 / 敏感泄漏）
    zero_recall 该召回却一张都没召回的查询数 —— 最刺眼的失败

违反 must_not 比漏召回更严重：漏了是"没帮上忙"，错了是"把不该说的说出来"。
所以 violations 单独计、单独设闸。

## 跑法

    python evals/recall.py                 # 人读的报告
    python evals/recall.py --json          # 机器读的，给 CI 用
    python evals/recall.py --baseline b.json   # 跟基线比，退步就非零退出
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT / "packages" / "agent-protocol-core" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from memgarden.adapt import FieldMap, to_card  # noqa: E402
from memgarden.selection import (  # noqa: E402
    Chain,
    RecentStage,
    RelevanceStage,
    RoleStage,
)

CORPUS = pathlib.Path(__file__).resolve().parent / "corpus"

#: 语料同时含新旧两种形状 —— 这不是凑数：形状不匹配正是「卡在库里却召不回」
#: 那个事故的根因，eval 语料必须让它有机会重现。
_CANONICAL = FieldMap(
    summary_fields=("summary",),
    text_fields=("summary", "content", "bucket"),
    private_fields=("content",),
)
_LEGACY = FieldMap(
    summary_fields=("title", "description"),
    text_fields=("title", "description", "her_quote", "context", "linked_dimension"),
    private_fields=("description", "her_quote", "context"),
)


def load_cards() -> list[dict]:
    out = []
    for line in (CORPUS / "cards.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        mapping = _LEGACY if raw.get("_shape") == "legacy" else _CANONICAL
        card = to_card(raw, mapping)
        # ⚠️ 模拟宿主的**入口过滤**：算不出可搜索文本的卡会被整批丢弃。
        #
        # 不模拟这道过滤，eval 就抓不到最贵的那次事故 —— 写入端迁到新形状后，
        # 挑卡端仍按老字段算摘要，64 张卡里 45 张算出空串、被入口丢掉，
        # 用户问「我有一只狗吗」答记忆里没有。**卡就在库里，花园界面看得到。**
        # 实测过：不加这一段，把 text_fields 改坏，这条 eval 照样 100% 通过。
        if not str(card.get("search_text") or "").strip():
            continue
        card["id"] = raw["id"]
        card["roles"] = raw.get("roles") or []
        card["is_sensitive"] = bool(raw.get("is_sensitive"))
        card["occurred_at"] = raw.get("occurred_at", "")
        card["created_at"] = raw.get("occurred_at", "")
        out.append(card)
    return out


def load_queries() -> list[dict]:
    return [
        json.loads(l)
        for l in (CORPUS / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


#: io 现在线上用的那套组合。换策略就换这里 —— eval 比的是**策略**，不是内核实现。
POLICY = Chain(stages=(
    RoleStage("turning_point", limit=3, order_by="occurred_at"),
    RecentStage(limit=2, order_by="created_at"),
    RelevanceStage(limit=3, any_score=True),
))


def run(limit: int = 8) -> dict:
    """跑一遍，按**段**归因。

    ⚠️ 归因是这条 eval 的核心，不是装饰。第一版把所有召回一视同仁，结果 8 条查询
    全部"违反禁忌"，而元凶是同一张最新的噪声卡 —— 那是「最近 2 张」那一段的**设计
    行为**（不看查询、永远塞最新的）。把设计当故障，eval 就废了。

    所以分开算：

        relevance_violations  相关性段召回了禁忌卡 —— **真错误**，一次都不容忍
        anchor_cost           转折/最近两段占掉的名额 —— 设计代价，只观测不判罚
        filler                返回了但既不 must 也不 should 的卡 —— 噪声水位
    """
    cards, queries = load_cards(), load_queries()
    per_query = []
    hits = must_total = relevance_violations = zero_recall = 0
    anchor_slots = filler = returned_total = wanted_returned = 0

    for q in queries:
        picks = POLICY.select(cards, q["query"], limit=limit).picks
        by_stage = {p.card_id: p.stage for p in picks}
        picked = set(by_stage)
        # 相关性段挑出来的才算"这次查询认为相关的"；
        # 转折/最近段是无条件打底，不代表相关性判断。
        by_relevance = {cid for cid, st in by_stage.items()
                        if st not in ("turning_point", "recent")}

        must = set(q.get("must") or [])
        must_not = set(q.get("must_not") or [])
        should = set(q.get("should") or [])

        got, missed = must & picked, must - picked
        bad = must_not & by_relevance          # ← 只算相关性段的
        anchored = picked - by_relevance

        must_total += len(must)
        hits += len(got)
        relevance_violations += len(bad)
        anchor_slots += len(anchored)
        returned_total += len(picked)
        wanted_returned += len(picked & (must | should))
        filler += len(picked - must - should - anchored)
        if must and not got:
            zero_recall += 1

        per_query.append({
            "qid": q["qid"], "query": q["query"],
            "recalled": sorted(got), "missed": sorted(missed),
            "violated_by_relevance": sorted(bad),
            "anchored": sorted(anchored), "returned": len(picked),
        })

    n = len(queries) or 1
    return {
        "recall": round(hits / must_total, 3) if must_total else 1.0,
        "relevance_precision": (round(wanted_returned / returned_total, 3)
                                if returned_total else 0.0),
        "relevance_violations": relevance_violations,
        "zero_recall_queries": zero_recall,
        "anchor_slots_per_query": round(anchor_slots / n, 2),
        "filler_per_query": round(filler / n, 2),
        "queries": len(queries),
        "cards": len(cards),
        "per_query": per_query,
    }


def _report(r: dict) -> None:
    print(f"语料 {r['cards']} 张卡 · {r['queries']} 条查询\n")
    print("  判罚项（退步就该红）")
    print(f"    recall                {r['recall']:.1%}   must 里的卡召回了多少")
    print(f"    相关性段违反禁忌       {r['relevance_violations']}      把不该说的说出来 —— 零容忍")
    print(f"    零召回查询             {r['zero_recall_queries']}      该召回却一张都没有")
    print("\n  观测项（只看趋势，不判罚）")
    print(f"    相关性精度             {r['relevance_precision']:.1%}")
    print(f"    打底占用名额/查询       {r['anchor_slots_per_query']}    转折+最近两段的固定成本")
    print(f"    填充卡/查询            {r['filler_per_query']}    既不 must 也不 should 的")
    bad = [q for q in r["per_query"] if q["missed"] or q["violated_by_relevance"]]
    if not bad:
        print("\n  ✅ 每条查询都达标。")
        return
    print("\n  未达标：")
    for q in bad:
        print(f"    [{q['qid']}] {q['query']}")
        if q["missed"]:
            print(f"        漏召回: {q['missed']}")
        if q["violated_by_relevance"]:
            print(f"        ⚠️ 相关性段召回了禁忌卡: {q['violated_by_relevance']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON，给 CI 用")
    ap.add_argument("--baseline", help="跟基线 JSON 比，退步则非零退出")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    r = run(limit=args.limit)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        _report(r)

    if args.baseline:
        base = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
        regressions = []
        # 阈值不对称：召回退步容忍 2 个百分点的抖动，
        # 违反禁忌**一次都不容忍** —— 那是把不该说的说出来。
        if r["recall"] < base["recall"] - 0.02:
            regressions.append(f"recall {base['recall']:.1%} → {r['recall']:.1%}")
        if r["relevance_violations"] > base["relevance_violations"]:
            regressions.append(f"相关性段违反禁忌 {base['relevance_violations']} → {r['relevance_violations']}")
        if r["zero_recall_queries"] > base["zero_recall_queries"]:
            regressions.append(
                f"零召回 {base['zero_recall_queries']} → {r['zero_recall_queries']}")
        if regressions:
            print("\n❌ 相对基线退步：\n  " + "\n  ".join(regressions), file=sys.stderr)
            return 1
        print("\n✅ 未低于基线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
