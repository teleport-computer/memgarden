# 召回与混合检索接入指南

[接入主循环](GETTING-STARTED.md) · [字段与存储](INTEGRATION-AND-DATA.md) · [验证证据](STATUS.md)

Garden 提供排序、选卡算法和策略接口；宿主决定怎样把它接入自己的上下文、搜索索引和 embedding。升级包后，使用默认 `RelevanceStage` 的策略会换成新排序器（见第 1 节）；向量混合检索仍需显式开启。

## 1. 选择入口

自动想起和主动搜索用**同一个排序器** `memgarden.retrieval`：BM25 词法打分 + 停用词 + 覆盖率/强证据门槛。两条路的 trace 带同一个 `version`（如 `memgarden-bm25-v1+tok:mg-default-v1`），宿主据此确认用的是同一把尺子。

| 入口 | 行为 | 如何接入 |
|---|---|---|
| `retrieval.rank` | 按相关性排序；无命中返回空；可设 `limit` 与资源上限 | 主动搜索、宿主自己的索引页；传入已授权候选 |
| `retrieval.select_context` | 自动想起：每张都先过门槛；转折≤3、最近≤2 软配额决定座位，空位按分数补；输出按分数排，默认总数 8 | 低层函数，或包成 SelectionPolicy（见第 3 节） |
| `selection.Chain` + `RelevanceStage` | 可组合；`RelevanceStage` 默认 `scorer="bm25"`（即 `rank`），`RecentStage`/`RoleStage` 不看查询 | 传给 `MountedGarden(selection_policy=...)` |
| `scoring.relevance`（deprecated） | 旧短语/稀有词打分，保留一个版本给未切换的宿主回滚；`RelevanceStage(scorer="legacy")` 同理 | 不要在新接入里使用 |
| `select_hybrid_context_memories_with_trace` | 向量与词法分别过门槛，再用加权 RRF 融合；词法一侧仍是旧打分 | 显式传入宿主向量及参数；不在默认策略中启用 |

调用低层函数前，必须先过滤 tenant/owner/mount 和归档、取代、删除状态；这些纯函数不替你做授权。IDF 按传入的候选算，所以**候选池是谁决定了尺子**：同一个 query、同一批候选，`rank` 与 `select_context` 的顺序一致；候选池不同（自动想起取最近一页、搜索取全量）时分数不可直接比较。

默认 `context_for_turn` 的 block 文本是**记忆摘要**，同时返回 `record_ids`。厚正文保存在卡中；需要全文时，由宿主在同一授权范围读取并按上下文预算组织，不要假定 block 已包含全部细节。

### 分词器、停用词与门槛

- **分词器是插口**：`Tokenizer` 协议只要 `name` 和 `tokenize(text) -> list[str]`，`name` 进版本号。不传时用零依赖的 `DefaultTokenizer`：整段 ASCII 标识符（`jira-4821`、`v2.3.1`、`x100v` 不被切碎，也不会子串命中）、CJK 单字 + 相邻二字（不生成和语法助词相邻的二字）、其余文字按词。包本身仍然零依赖；jieba 之类由宿主注入。
- **停用词**（`DEFAULT_STOPWORDS`）只从查询里去掉，不改变卡片侧统计。
- **门槛**：卡片命中查询 IDF 总量 ≥25%（`min_coverage`），或分数 ≥ 1.25 × 本批候选的最大单词 IDF（`strong_evidence`，给长段粘贴用）。花园里没有的词也算进分母——这正是「没记过」的信号。
- 数值由 `evals/retrieval` 校准，过程与放弃的候选见 [evals/retrieval/README.md](../evals/retrieval/README.md)。只有 53 条合成查询，**不证明线上质量**；换分词器或改门槛后请在自己的语料上重测，上线后看 trace。
- 词法方法拿不到换说法、跨语言；它也不证明答案正确，只证明用词重叠。
- 复现 io 旧 `memory_bm25` 的逐项结果：`rank(..., tokenizer=<jieba 分词器>, stopwords=frozenset(), min_coverage=0)`，版本号会带 `+cfg:` 后缀。

## 2. 三个容易混淆的字段

| 字段 | 当前责任与行为 |
|---|---|
| `retrieval_cues` | Capture/Dream 可生成的搜索线索，保存在卡里；不是新事实。`retrieval.default_search_text` 会把规范化后的 cues 放进匹配文本；旧 `scoring.relevance` 不读 |
| `search_text` | 宿主构造的搜索投影，存在时优先用于词法匹配；缺失时 `retrieval` 用 summary/content/bucket/threads/cues，旧打分只用 summary/content/bucket |
| `role` / `roles` | 持久 Card 是单个 `role` 字符串；选卡器接收 `roles: list[str]`。宿主需要显式映射，例如 `role="turning_point"` → `roles=["turning_point"]`，不会自动迁移或猜测标题 |

不要把检索投影覆盖回原始记忆正文，也不要把 cues 当成事实证据。`importance_level` 1–5 在解析时映射到既有 `importance` 0.2/0.4/0.6/0.8/1.0，存储仍是 0–1；不是新增一套长期数值字段。

生成器将 cues 规范化为最多5条、每条120个 Python 字符；这是模型生成的辅助线索预算，不是正文存储上限。正式 Card/schema 的可选字符串数组不对既有或外部卡片增加这两个硬限制。

## 3. 接到 MountedGarden

[可运行示例](../examples/retrieval_runtime.py) 提供 `RelevantPolicy.select`：

1. 接收 MountedGarden 已按 Scope 过滤的候选。
2. 在候选副本中把 role 映射为 roles；搜索文本由 `default_search_text` 读取，不必另做投影。
3. 调用 `retrieval.select_context`。
4. 返回 `SelectionResult(Pick(...))`，只给出候选 ID 和选择依据；Garden 使用原始卡片生成上下文。

```bash
uv run python examples/retrieval_runtime.py
```

示例还检查另一个 owner 召回不到这些记忆。直接用自定义 Store 的宿主同样需要遵守权限和生命周期过滤。

## 3a. 主动搜索

用户或模型**明确要找**某件事时走搜索，不走自动想起：

| | 自动想起（`context_for_turn` / `select_context`） | 主动搜索（`search` / `records.search` / `memory_search`） |
|---|---|---|
| 查询 | 宿主拼（例如最近几条消息） | 用户或模型给的一句话 |
| 背景卡 | 挑卡策略可带（`RecentStage`、`RoleStage`） | **不带**，不经过 selection_policy |
| 无命中 | 可以为空，也可以只有策略里的背景卡 | **空**；本版没有 `suggestions` 字段 |
| 返回 | `record_ids` + `blocks` + trace | `record_ids` + `hits[{id, score, matched, coverage}]` + `ranking` + trace |

- `GardenComponent.search(SearchRequest)`：候选由宿主给（已过权限与生命周期过滤），默认 `limit=20`。分词器在构造组件时注入：`GardenComponent(model=..., tokenizer=my_tokenizer)`；`MountedGarden(..., tokenizer=...)` 同样透传。
- `MountedGarden.search(scope, query, *, limit=20, mount=None)`：候选按 Scope 从库里取，只含当前有效的卡；另一个 owner、未授权 mount、真删或被取代的卡都搜不到。
- `memory_search` 工具（SDK 与 `tool.invoke`）走同一个 `search`，只返回命中卡的摘要文本，无命中时 `content` 为空串。
- JSON Lines：`records.search`，manifest 声明 `capabilities.search`；Store 不支持 owner 分区时和其它读路径一起关闭。
- `hits[].matched` 是命中的查询词，属于用户文本片段；`trace` 只有计数和版本。宿主把结果落日志前自行裁剪。
- 两条路的 `ranking` / `trace.version` 相同即可确认是同一把尺子。不要要求两条路 top-k 相同：查询不同、候选池不同、自动想起还有配额。

## 4. Hybrid 的分工

宿主负责 embedding 模型、投影文本、向量存储、更新/删除、权限和成本。Garden 只接收：

- 本次已授权的候选卡与查询文本；
- query vector、`card_id -> vector`，以及可选但建议成对提供的模型/投影版本标识；
- 经你的模型和语料校准的 `min_cosine`；它没有默认值；
- `reference_time`：宿主认为的当前时刻，用来界定“最近”。

各向量必须维度相同、分量有限且范数非零。启用向量计算时，若使用版本校验，`vector_model` / `card_vector_models` 必须成对提供，每张实际参与计算的向量都必须有匹配标签；不接受“缺标签当匹配”。不提供版本校验时，维度相同不等于模型相同，宿主自行保证来源一致。

Hybrid 的词法一侧目前仍是旧打分：默认门槛为0.35且至少 medium 证据；向量使用宿主的余弦门槛。通过任意一侧即可成为候选，每侧只给自己合格的卡排名。融合使用 `w_v/(k+r_v) + w_l/(k+r_l)`；缺席一侧贡献0，权重0禁用该侧。默认 k=20、权重2:1是算法初始值，**不是在你的数据上验证过的最佳参数**。

融合 shortlist 默认20张（至少覆盖 cap），软配额在其中分配席位，最终按融合顺序返回。没有向量的卡只参加词法通道；没有查询向量时明确降级为词法。空查询返回空，不用向量暗中注入记忆。

不传 `reference_time` 时以最新候选的创建时间为参照，是确定性的后备行为：一座全旧的花园也可能被视为“最近”。线上建议传入明确的当前时间，不能拿这个后备规则推断真实新鲜度。

## 5. 向量生命周期必须跟着卡片走

| 卡片变化 | 宿主的向量/索引操作 |
|---|---|
| 新建 | 按同一模型和投影版本生成向量，绑定卡 ID |
| 更新正文/cues | 使旧向量失效或重新生成；未准备好的卡暂时只走词法 |
| Dream 取代旧卡 | 不让旧卡进入 active 候选；为新卡建立自己的向量，不挪用旧卡 ID |
| 删除 / 改权限 | 同步移除或隔离索引、缓存；在调用 Garden 前再次按当前权限过滤 |
| 更换 embedding 模型或投影 | 更新版本标识并重建；不要混算不同模型的向量 |

需要通过 SelectionPolicy 接入 hybrid 时，把向量提供能力包在宿主策略对象里，沿用示例的“返回 Pick，不返回自造卡片”方式；不要把一个用户的查询向量放在多用户共享对象的可变全局字段里。

## 6. 验证与观测

`trace` 包含分数、排名、所选 bucket、缺失向量/被门槛挡下的样本。它不回显向量，但可能包含标题和命中词，**不是天然脱敏日志**。传出信任边界前由宿主做字段白名单处理。

发布前至少检查：语义近似但用词不同的命中、编号/专名的精确命中、无关卡排除、空查询、部分向量缺失、角色和最近时间、更新/取代/删除后索引一致。对比旧策略和新策略的同一批人工标注样本，记录参数与模型/投影版本。纯数学与模拟向量测试不等于真实 embedding 质量评测。

早期实现说明：[T510/T513](T510-selection.md)、[T523](T523-hybrid.md)。当前接入方式以上述指南和源码接口为准，当前验收范围只在 [Status](STATUS.md) 维护。

## 7. 关联读取（一跳邻居）

模型或用户取回某几张卡时，`MountedGarden.related(scope, ids, cap=6)` 顺带给出与它们相连的卡，每项 `{id, summary, source_id, relation, status}`，`summary` 折叠空白后最多120字，不含正文。它不是搜索：没有查询文本，只沿卡片上已经存在的关系走一步。

| relation | 来源 | 能否带出历史卡 |
|---|---|---|
| `anchor` | 源卡的 `anchor_memory_ids` | 能，仅当该卡 `status == "superseded"`，并如实标在 `status` |
| `supersedes` | 源卡的 `supersedes`；参考 Store 中另含 `superseded_by` 指向源卡的旧卡 | 同上 |
| `thread` | 源卡与候选卡的 `threads` 有交集 | 不能 |

- 优先级 anchor > supersedes > thread；显式链接排在线索邻居之前，各自按 id 排序，截到 `cap`。同一张卡被多张源卡命中时保留最强关系。
- 归档、删除的卡永不出现；没有 `summary` 的卡不出现。`related` 只读 Scope 的 owner 与挂载点，硬删的卡不在库里自然读不到；不认识的生命周期值不当候选。
- 源卡默认只取 active；`include_archived` / `include_superseded` 可放开源卡范围（不影响候选规则）。
- 不做反向（从旧卡找取代它的新卡）和多跳；这两项需要产品确认后另加。

不使用内置 Store 的宿主可直接调用纯函数 `memgarden.related.one_hop(sources, candidates, cap=6)`。它要求：

1. 候选已按 owner、可见性过滤；函数不做授权。
2. 生命周期写成规范字段 `status`（`active` / `superseded` / `archived` / `deleted`）或 `superseded_by`；宿主自有的归档标记（如 `archived_at`）先翻译成 `status="archived"`，但**已是 `superseded` 的卡保留 `superseded`**，否则历史版本会被当成普通归档而消失。
3. 摘要放在 `summary`；旧标题字段先翻译过来。

v1 行为与宿主 io 读侧的实现逐项一致，由 [黄金用例](../tests/fixtures/related_one_hop_golden.json) 锁定（144 组，含 120 组带种子的随机花园）。
