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
python evals/retrieval/harness.py --set multi_turn        # 多轮窗口查询集（自动想起的查询形状）
```

外部 ranker 只需一个函数 `rank(query, cards, k) -> list[card_id]`，`cards` 是已过宿主生命周期
过滤的候选。宿主自己的实现（带第三方分词器也行）放在**本仓库之外**接进来量，
内核依旧零依赖。

## 语料

| 文件 | 内容 |
|---|---|
| `cards.jsonl` | 76 张手写卡：中/英/混写，工单号、版本号、航班号、型号，取代链（`status: superseded` + `supersedes`），线程，陷阱卡（同名歌手 Aurora、妈妈的膝盖、同事的马拉松、青苹果） |
| `filler.py` | 按固定种子生成 140 张日常流水卡作背景噪声；生成时自查不含任何查询答案词 |
| `queries_multi_turn.jsonl` | 16 个多轮对话窗口，6 类：`mt_anaphora`（当前一句是指代，实体在前文）`mt_switch`（窗口里换了话题）`mt_long_reply`（长篇建议式 AI 回复）`mt_mixed`（中英混写/型号）`mt_trap` `mt_no_hit`。每条给 `messages`，查询由 harness 按 io 的方式拼（见下文「多轮窗口」），标注字段同上 |
| `queries.jsonl` | 53 条查询，12 类：`exact_entity` `paraphrase` `paraphrase_xlang` `cjk_nospace` `en_id` `mixed` `short` `long_paste` `distractor` `superseded` `history` `thread` `no_hit`。每条带 `must` / `should` / `must_not` / `why`；无命中类带 `expect_empty`；`history` 类带 `related`（应经关联读取拿到的旧卡，暂不计分） |

**全部虚构，不含真实用户数据。**被取代的卡由 harness 按宿主规则过滤掉，两类 ranker 看到的是同一个候选池。

改语料的纪律同 `evals/README.md`：每条断言写 `why`；一条红了先判断是产品定义变了还是实现退步了，
不要为了变绿改标注。

## 内置 ranker

- `mg-relevant`：`scoring.relevance.select_relevant_context_memories_with_trace`，即当前自动想起
  （相关性门槛 0.35 + 转折/最近软配额，cap 8）。
- `mg-scores`：同一套打分去掉门槛和分桶、纯按分数排。诊断用，区分「分不准」和「被门槛挡掉」。
- `mg-bm25`：`retrieval.rank`，默认分词器、默认停用词和门槛，即统一后的排序器。
- `mg-select`：`retrieval.select_context`，统一后的自动想起（同一把尺子 + 软配额）。
- `mg-select-scaled`：同上，打开按查询长度放大的强证据闸（`strong_evidence_terms=8`）。

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

## 多轮窗口（自动想起的查询形状，2026-09-15）

上面的 53 条都是「一句话」。io 的自动想起不是拿一句话去查：它把**最近 4 条非空的 user/assistant
消息按时间顺序用换行拼起来**当查询（含上一条 AI 回复，第 5 条及更早的掉出窗口；
io `backend/enclave/routes/chat.py::_build_context_memories`）。`harness.chat_window_query` 照抄这个构造，
`queries_multi_turn.jsonl` 里每条给消息列表，由它拼成查询。改 io 那边的构造时这里要跟着改。

结果文件 `results/multi-turn-2026-09-15.json`。`select_context` cap 8；jieba 列注入 io 的
`memory_bm25.tokenize`（jieba 0.42.1，适配器在仓库外），Python 3.14，Apple Silicon 单核。

| 指标 | 旧 mg-relevant | mg-select（默认闸） | mg-select 放大闸 | mg-select + jieba | 放大闸 + jieba |
|---|---|---|---|---|---|
| recall@5 | 0.962 | 0.962 | 0.923 | 1.000 | 0.923 |
| MRR@8 | 0.515 | 0.923 | 0.923 | 0.923 | 0.923 |
| precision@8 | 0.415 | 0.298 | 0.827 | 0.654 | 0.897 |
| 无命中返回空 | 1/3 | **0/3** | 3/3 | **0/3** | 3/3 |
| 有答案却返回空 | 0 | 0 | 0 | 0 | 0 |
| 每轮平均带回 | 6.25 | **7.88** | 1.81 | **4.25** | 1.31 |
| p50 @210 / 1000 卡 (ms) | 64 / 308 | 3.4 / 16 | 3.2 / 15 | 6.9 / 32 | 6.9 / 32 |

同一个放大闸在单句集上的代价：默认分词器数字不变（recall@5 0.932、有答案却返回空 1）；
jieba 下 recall@5 0.896 → 0.875、有答案却返回空 3 → 4（多出来的是 q33 长告警粘贴，唯一锚点是 JIRA-4821）。

读法：

- **长窗口的问题不是「门槛太严、答案被挡」，是「门槛太松」**：有答案的窗口答案都在前两位
  （有答案却返回空 0），但没什么可想起的闲聊窗口照样带回一整屏杂卡。原因是固定的强证据闸
  （1.25 × 最大单词 IDF ≈ 7.6）：四条消息去停用词后有 50–200 个 token，杂卡靠「楼下」「有点」「特别」这类
  泛词各撞一点，分数累加到 15–25，远过闸；覆盖率这道闸反而正常（杂卡 0.02–0.08）。
- **放大闸**（`strong_evidence_terms=8`：token 数超过 8 时闸乘 √(n/8)）把无命中全挡住、平均带回降到 1–2 张。
  丢掉的两个答案（mt02 的「去六院看膝盖」、mt15 的「慢性胃炎」）是同一窗口里的**第二个**答案，
  放大前也排在杂卡后面。
- **为什么默认没开**：同一个放大会挡掉「长段粘贴里只有一个编号是锚点」的答案
  （`tests/test_retrieval_rank.py` 的长粘贴用例、jieba 单句集 q33）——这正是强证据闸本来要放行的情况。
  而且 8 两侧都窄：同一族参数里 6 让默认分词器单句集多两条返回空，10 让多轮无命中退回 0–1/3。
  自动想起要不要开，是宿主在「闲聊时不乱想起」和「长粘贴只靠编号也能想起」之间的取舍。
- **试过没用的**：只加覆盖率下限（0.03–0.06，多轮无命中最多 2/3 且伤单句集）；相对分数闸（≥ 中位数/均值 × λ，
  无命中最多 1–2/3）；覆盖率或分数的 z 分数（要么挡不住、要么 recall 掉到 0.8 以下）；只累加稀有词的证据（单句集
  recall@5 掉到 0.87 以下）。
- 16 条合成窗口、3 条无命中——**只够说明方向，不够定线上阈值**。`tests/test_retrieval_eval_gate.py` 用 strict xfail
  记着默认闸的无命中缺陷，并守放大闸的质量线；上线后看 trace 里的 `below_gate` / `evidence_scale`。
