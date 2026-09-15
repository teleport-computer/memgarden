# evals/retrieval —— 召回排序量尺

同一份合成花园、同一组带标注的查询，量不同排序实现的**召回质量、无命中语义和延迟**。
用途是给「自动想起与主动搜索统一成一个排序器」提供前后对比的基线。

它和 `evals/recall.py` 不重复：`recall.py` 是发布闸，守默认选卡策略不退步；
这里没有阈值、不进 CI，只出数字。

```bash
python evals/retrieval/harness.py                     # 内置 ranker：mg-relevant、mg-scores
python evals/retrieval/harness.py --misses            # 逐条列出漏召回 / 陷阱 / 无命中失败
python evals/retrieval/harness.py --ranker my_rank.py:rank --name my-ranker --json out.json
python evals/retrieval/harness.py --compare evals/retrieval/results/baseline-2026-09-15.json
```

外部 ranker 只需一个函数 `rank(query, cards, k) -> list[card_id]`，`cards` 是已过宿主生命周期
过滤的候选。宿主自己的实现（带第三方分词器也行）放在**本仓库之外**接进来量，
内核依旧零依赖。

## 语料

| 文件 | 内容 |
|---|---|
| `cards.jsonl` | 76 张手写卡：中/英/混写，工单号、版本号、航班号、型号，取代链（`status: superseded` + `supersedes`），线程，陷阱卡（同名歌手 Aurora、妈妈的膝盖、同事的马拉松、青苹果） |
| `filler.py` | 按固定种子生成 140 张日常流水卡作背景噪声；生成时自查不含任何查询答案词 |
| `queries.jsonl` | 53 条查询，12 类：`exact_entity` `paraphrase` `paraphrase_xlang` `cjk_nospace` `en_id` `mixed` `short` `long_paste` `distractor` `superseded` `history` `thread` `no_hit`。每条带 `must` / `should` / `must_not` / `why`；无命中类带 `expect_empty`；`history` 类带 `related`（应经关联读取拿到的旧卡，暂不计分） |

**全部虚构，不含真实用户数据。**被取代的卡由 harness 按宿主规则过滤掉，两类 ranker 看到的是同一个候选池。

改语料的纪律同 `evals/README.md`：每条断言写 `why`；一条红了先判断是产品定义变了还是实现退步了，
不要为了变绿改标注。

## 内置 ranker

- `mg-relevant`：`scoring.relevance.select_relevant_context_memories_with_trace`，即当前自动想起
  （相关性门槛 0.35 + 转折/最近软配额，cap 8）。
- `mg-scores`：同一套打分去掉门槛和分桶、纯按分数排。诊断用，区分「分不准」和「被门槛挡掉」。

## 基线（2026-09-15）

`results/baseline-2026-09-15.json`。第三列是一个宿主侧 BM25 实现（jieba 0.42.1 精确模式 +
整段 ASCII 标识符 token，k1=1.2 b=0.75，无停用词，score>0 即返回），通过外部 ranker 接入；
Python 3.14，Apple Silicon 单核。

| 指标 | mg-relevant | mg-scores | host-bm25-jieba |
|---|---|---|---|
| recall@5 | 0.668 | 0.825 | 0.896 |
| MRR@8 | 0.533 | 0.685 | 0.806 |
| precision@3 | 0.598 | 0.475 | 0.545 |
| 无命中返回空 | 4/5 | 2/5 | 1/5 |
| 有答案却返回空 | 9 | 1 | 0 |
| p50 延迟 @210 / 500 / 1000 卡 (ms) | 18 / 44 / 84 | 19 / 48 / 83 | 8 / 19 / 40 |

读法：

- 内核打分对编号/短 token 偏弱（`N2`、`PR #317`、`X100V` 单个稀有词只算弱证据，被 0.35 门槛挡掉），
  「有答案却返回空」9 条主要来自这里。
- BM25 召回最好，但没有停用词和分数下限：「我」「什么」这类词就能让不相关短卡进结果，
  无命中查询 5 条里 4 条返回了东西。
- 两边都拿不到换说法和跨语言的查询（paraphrase / paraphrase_xlang），这是词法匹配的边界，不是 bug。
- 内核打分的延迟主要花在卡片侧（短语集合重复计算、查询短语 × 卡片实体两两归一化），
  不在查询侧重复分析。
