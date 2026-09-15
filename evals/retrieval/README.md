# evals/retrieval —— 召回排序量尺

同一份合成花园、同一组带标注的查询，量不同排序实现的**召回质量、无命中语义和延迟**。
用途是给「自动想起与主动搜索统一成一个排序器」提供前后对比的基线。

它和 `evals/recall.py` 不重复：`recall.py` 是发布闸，守默认选卡策略不退步；
这里主要出数字。唯一的阈值是 `tests/test_retrieval_eval_gate.py`：默认 `retrieval.rank`
在这份语料上的质量线（实测值减余量，改语料标注时同步更新实测值和原因）。

```bash
python evals/retrieval/harness.py                     # 内置 ranker：mg-relevant、mg-scores、mg-bm25
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
- `mg-bm25`：`retrieval.rank`，默认分词器、默认停用词和门槛，即统一后的排序器。

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

## 统一排序器校准（MG-3，2026-09-15）

同一语料、同一机器，结果文件 `results/unified-rank-2026-09-15.json`。`mg-bm25+jieba` 是 `retrieval.rank` 注入 io 的 jieba 分词器（适配器在仓库外），
默认停用词与门槛不变。

| 指标 | 内核现行 mg-relevant | io BM25 + jieba | **mg-bm25（默认分词器）** | mg-bm25 + jieba |
|---|---|---|---|---|
| recall@5 | 0.668 | 0.896 | **0.932** | 0.896 |
| MRR@8 | 0.533 | 0.806 | **0.891** | 0.849 |
| precision@3 | 0.598 | 0.545 | 0.720 | 0.830 |
| 陷阱排在答案前 | 1 | 1 | 0 | 0 |
| 无命中返回空 | 4/5 | 1/5 | 4/5 | 4/5 |
| 有答案却返回空 | 9 | 0 | 1 | 3 |
| p50 @210 / 500 / 1000 卡 (ms) | 17 / 42 / 81 | 7 / 16 / 32 | 2 / 6 / 11 | 7 / 16 / 33 |

怎么定的：

- **停用词**（`DEFAULT_STOPWORDS`）：中英虚词、代词、疑问词、英文缩写残片（`what's` 切出的 `s`）。
  只从查询里去掉。不去停用词时 recall@5 0.974，但无命中查询 5 条里 4 条返回东西。
- **默认分词器不生成和语法助词（的了着吗呢吧啊呀嘛么）相邻的二字**：「的车」「么事」这类跨词噪声
  会抬高查询的 IDF 分母。去掉后指标不变，门槛的稳定区间变宽。
- **门槛**：一张卡要么命中查询 IDF 总量的 ≥25%（覆盖率），要么分数 ≥ 1.25 × 本批候选的最大单词 IDF
  （强证据，给长段粘贴用）。默认分词器下覆盖率 0.15–0.30、强证据 1.1–2.0 结果完全相同；jieba 下
  强证据 1.5 起开始漏答案，所以取 1.25。
- **放弃的候选**：纯分数下限（分数量纲随候选数和分词器变，同一阈值换个花园就失效）；只用覆盖率
  （长段粘贴的正确答案覆盖率只有 0.06–0.14，会被挡掉）；分数 ≥ a·√(查询 IDF 总量)（无命中 4/5 时
  recall@5 只有 0.911）；只生成二字不生成单字（「猫」「辣」这种一字查询搜不到，recall@5 掉到 0.870）。

剩下的失败：

- 换说法、跨语言（q06/q08/q10）拿不到，词法方法的边界。
- q49「我喜欢什么颜色的车」仍返回写着「喜欢」的卡：默认分词器把「喜」「欢」「喜欢」算了三次，
  证据分够得上强证据闸。`tests/test_retrieval_rank.py` 用 strict xfail 记着这条。
- jieba 下 q52「我姐姐叫什么名字」返回 3 张带「名字」的卡。
