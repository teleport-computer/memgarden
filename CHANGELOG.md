# Changelog

发布记录以 Git tag、GitHub Release 和 PyPI 为准；这里按版本记录对接入方可见的变化。

## Unreleased

### Added

- `memgarden.STABLE_MODULES`：顶层之外承诺稳定的子模块清单；这些模块都有 `__all__`，并由 `tests/test_public_api_surface.py` 快照。宿主工具函数（`timestamps`、`text.card_guard`、`text.card_text`、`text.leak_signals`、`guards.dream_gates`、`prompts.recall_fields`、`prompts.buckets`、`dreaming`、`observability`、`garden_language`、`policies`）以及 `contracts`、`selection` 从此是公开合同。
- `memgarden.retrieval.rank(query, candidates, *, tokenizer=None, ...)`：BM25 词法排序，数学逐项移植自 io 的 `memory_bm25`（给同一分词器时分数逐位相同）。分词器是插口（`Tokenizer` 协议，`name` 进排序版本号）；不注入时用零依赖的 `DefaultTokenizer`（整段 ASCII 标识符 + CJK 单字与二字）。返回 `RankResult(hits, version, trace)`，`version` 形如 `memgarden-bm25-v1+tok:mg-default-v1`，trace 内容无关。查询和每张卡在单次调用内只分析一次，不跨调用缓存。可选资源上限超出时抛 `SearchLimitExceeded`。
- `retrieval.rank` 默认带停用词（`DEFAULT_STOPWORDS`，只从查询里去掉）和覆盖率/强证据门槛（`min_coverage=0.25`、`strong_evidence=1.25`），无命中返回空。数值由 `evals/retrieval` 校准，校准过程和放弃的候选见 `evals/retrieval/README.md`；`tests/test_retrieval_eval_gate.py` 守质量线。复现旧 BM25 语义传 `stopwords=frozenset(), min_coverage=0`（版本号随之带上 `+cfg:` 后缀）。`Hit` 带 `coverage`。
- `retrieval.select_context(query, candidates, *, tokenizer=None, cap=8, quotas=DEFAULT_QUOTAS, ...)`：自动想起。和 `rank` 同一次打分、同一道门槛、同一个版本号；转折点/最近软配额语义同 `select_relevant_context_memories_with_trace`（每张都要先过门槛，空位按分数补）。返回 `(卡片副本, trace)`，trace 内容无关，能直接喂 `observability.injection_record`。
- **关联读取** `memgarden.related.one_hop(sources, candidates, *, cap=6)` 与
  `MountedGarden.related(scope, ids, *, cap=6, include_archived=False, include_superseded=False)`。
  取回卡时给出一跳邻居：`anchor` / `supersedes`（源卡上的显式链接）优先，其次同线索的卡；
  归档、删除的卡不出现，被取代的卡只沿显式链接出现并标 `status="superseded"`。
  v1 与宿主 io 读侧实现逐项一致，由 144 组黄金用例锁定（`tests/fixtures/related_one_hop_golden.json`）。
  `MountedGarden.related` 自己做 owner / 挂载点 / 生命周期过滤，并把参考 Store 的
  `superseded_by` 取代链当作新卡的 `supersedes` 链接。不做反向查找和多跳。
  `one_hop` 的 `cap` 必须是非负整数（io 原实现不校验）。

### Changed

- `selection.RelevanceStage` 新增 `scorer`，**默认 `"bm25"`**（即 `retrieval.rank`）；`strong_min` / `medium_min` / `excluded_reasons` / `any_score` 只对 `scorer="legacy"` 生效。默认 CLI / DSH 服务壳的挑卡策略随之换尺子。回滚：`RelevanceStage(..., scorer="legacy")`。
- `select_context` 与旧 `select_relevant_context_memories_with_trace` 的有意差异：打分换成 BM25 + 门槛；返回列表按分数排（旧的按段排，配额决定的座位不变）；分数并列时 id 升序（旧的降序）；没有 id 的卡不进 IDF 统计。评测（`evals/retrieval`，默认分词器）：recall@5 0.668 → 0.932、MRR 0.533 → 0.891、无命中返回空 4/5 → 4/5、有答案却返回空 9 → 1、p50 @1000 卡 81 ms → 11 ms。
- `evals/baseline.json`（① recall.py 发布闸）按新打分重生成：recall 1.000 → 0.889，违反禁忌与零召回仍为 0，原因见 `evals/README.md`「基线变更记录」。
- `observability` 认识 `below_gate`、`over_cap` 两个拒绝理由。

### Deprecated

- `memgarden.scoring.relevance`（含 `select_relevant_context_memories_with_trace`、`select_context_memories(_with_trace)`、`memory_relevance_details`）：保留一个版本、行为不变，自动想起请改用 `retrieval.select_context`。

### Added（主动搜索）

- `GardenComponent.search(SearchRequest) -> SearchResult`、`MountedGarden.search(scope, query, *, limit=20, mount=None)`、JSON Lines `records.search`（manifest `capabilities.search`）。只返回 `retrieval.rank` 过了门槛的命中，无命中为空，不经过挑卡策略；`SearchResult.ranking` 与自动想起 trace 的 `version` 相同。`GardenComponent` / `MountedGarden` 接受 `tokenizer=`。

### Fixed

- `MountedGarden.invoke_tool("memory_search")`（含 `tool.invoke` 与 DSH Adapter 注册的工具）以前复用 `context_for_turn`：挑卡策略里有 `RecentStage` 时，搜花园里没有的东西也会返回最近写的几张卡。现在走 `search`，无命中返回空文本；候选只读一次，结果与回填用同一份快照。
- **Dream 提示词恢复卡片正文**。自 GardenComponent 顶层接口（6e8e9b6，08-29）起，整理提示词里每张卡只有
  `- [id] 摘要`，模型做 thicken / merge 时看不到正文、只能照标题重写，旧正文随旧卡退休。
  现在由 `prompts.dream.render_dream_cards` 带正文渲染（id / bucket / threads / occurred_at /
  summary / retrieval_cues / content），`MaintenanceRequest` 新增 `cards_limit=60`、
  `cards_budget_chars=60000`、`card_body_chars=5000`、`card_summary_chars=2000`。
  总预算按整张卡累加；截断的卡标 `TRUNCATED`，提示词要求不改写它们。
  trace 新增 `cards_rendered` / `cards_truncated` / `cards_omitted` / `truncated_card_ids`；
  `known_ids` 自动并入实际渲染的卡。解析、重试和守卫不变。
  **提示词文本有变化**（Step 1 措辞、新增 TRUNCATED 规则），依赖整份提示词快照的宿主需要更新快照。

### Added（历史导入：宿主驱动的分批会话，MG-8）

- 新增 `GardenComponent.import_session(request, *, progress, existing_cards, owner_key, index_ranker)`，返回 `ImportSession`：内核负责切批、提示词、解析与重问、跨批去重、单批/整次上限和 `ImportProgress` 推进；宿主调模型、写库，并用 `commit(outcome, record_ids=...)` / `fail(outcome, error)` 回报结果。给自己持有模型 key、加密和执行器的宿主（如 io）用。顶层新增导出 `ImportSession`、`ImportBatch`、`ImportBatchResult`。
- `MountedGarden.import_history` 改为跑在同一个 `ImportSession` 上（每个写卡批次前重读 Store，CAS 写回）。默认请求下续传指纹与此前逐字节一致，进行中的导入可以继续续传。
- `ImportRequest` 新增字段（均有默认值，默认时行为不变）：`batches`（宿主预切批次，可带 `label` / `occurred_from` / `occurred_to`）、`strategy`（`single_pass` 默认 / `two_pass`）、`batch_chars`、`write_batch_candidates`、`max_total_cards`、`fallback_occurred_at`、`naming_rule`、`identity`。wire `history.import` 接受前六项；`ImportProgress` 新增 `strategy`、`cards_added`、`candidates`、`candidates_cursor`。
- `two_pass`：每批先抽候选事实（`prompts/history_import.py`），读完材料后分组交给 Capture 提示词统一写卡。⚠️ 这时 `ImportProgress.candidates` 含用户内容，宿主要按记忆正文等级保存进度；候选总数上限 4000。
- 导入批次的「已有记忆索引」改为按本批文字挑相关旧卡（四分之一名额留给重要度最高的卡），此前只取重要度前 60，大导入的后续批次看不到前面写的卡。宿主可注入 `index_ranker`。
- 跨批桶名确定性收敛（大小写/空白、通用桶的双语斜杠写法、模型把通用桶清单相邻两项连抄成「工作、目标与成长」时取第一个）。
- 修复：`history_import` 档单段式提示词里拼了两段式「候选阶段、去重在后面」的开场，与要求输出最终卡的模板矛盾。单段式改用 `HISTORY_IMPORT_CARD_OPENING_RUBRIC`；`HISTORY_IMPORT_OPENING_RUBRIC` 原文保留（两段式抽候选和宿主 io 的 fact_map 在用）。**这会改变 `history_import` 档的单段式提示词**，新增提示词快照测试守着。
- 兼容注意：`MountedGarden.import_history` 不再经过 `capture_and_store`；monkeypatch 该方法来伪造导入回执的调用方需要改为注入模型。
