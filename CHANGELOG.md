# Changelog

发布记录以 Git tag、GitHub Release 和 PyPI 为准；这里按版本记录对接入方可见的变化。

## Unreleased

### Added

- `memgarden.STABLE_MODULES`：顶层之外承诺稳定的子模块清单；这些模块都有 `__all__`，并由 `tests/test_public_api_surface.py` 快照。宿主工具函数（`timestamps`、`text.card_guard`、`text.card_text`、`text.leak_signals`、`guards.dream_gates`、`prompts.recall_fields`、`prompts.buckets`、`dreaming`、`observability`、`garden_language`、`policies`）以及 `contracts`、`selection` 从此是公开合同。
- `memgarden.retrieval.rank(query, candidates, *, tokenizer=None, ...)`：BM25 词法排序，数学逐项移植自 io 的 `memory_bm25`（给同一分词器时分数逐位相同）。分词器是插口（`Tokenizer` 协议，`name` 进排序版本号）；不注入时用零依赖的 `DefaultTokenizer`（整段 ASCII 标识符 + CJK 单字与二字）。返回 `RankResult(hits, version, trace)`，`version` 形如 `memgarden-bm25-v1+tok:mg-default-v1`，trace 内容无关。查询和每张卡在单次调用内只分析一次，不跨调用缓存。可选资源上限超出时抛 `SearchLimitExceeded`。
- `retrieval.rank` 默认带停用词（`DEFAULT_STOPWORDS`，只从查询里去掉）和覆盖率/强证据门槛（`min_coverage=0.25`、`strong_evidence=1.25`），无命中返回空。数值由 `evals/retrieval` 校准，校准过程和放弃的候选见 `evals/retrieval/README.md`；`tests/test_retrieval_eval_gate.py` 守质量线。复现旧 BM25 语义传 `stopwords=frozenset(), min_coverage=0`（版本号随之带上 `+cfg:` 后缀）。`Hit` 带 `coverage`。
- `retrieval.select_context(query, candidates, *, tokenizer=None, cap=8, quotas=DEFAULT_QUOTAS, ...)`：自动想起。和 `rank` 同一次打分、同一道门槛、同一个版本号；转折点/最近软配额语义同 `select_relevant_context_memories_with_trace`（每张都要先过门槛，空位按分数补）。返回 `(卡片副本, trace)`，trace 内容无关，能直接喂 `observability.injection_record`。

### Changed

- `selection.RelevanceStage` 新增 `scorer`，**默认 `"bm25"`**（即 `retrieval.rank`）；`strong_min` / `medium_min` / `excluded_reasons` / `any_score` 只对 `scorer="legacy"` 生效。默认 CLI / DSH 服务壳的挑卡策略随之换尺子。回滚：`RelevanceStage(..., scorer="legacy")`。
- `select_context` 与旧 `select_relevant_context_memories_with_trace` 的有意差异：打分换成 BM25 + 门槛；返回列表按分数排（旧的按段排，配额决定的座位不变）；分数并列时 id 升序（旧的降序）；没有 id 的卡不进 IDF 统计。评测（`evals/retrieval`，默认分词器）：recall@5 0.668 → 0.932、MRR 0.533 → 0.891、无命中返回空 4/5 → 4/5、有答案却返回空 9 → 1、p50 @1000 卡 81 ms → 11 ms。
- `evals/baseline.json`（① recall.py 发布闸）按新打分重生成：recall 1.000 → 0.889，违反禁忌与零召回仍为 0，原因见 `evals/README.md`「基线变更记录」。
- `observability` 认识 `below_gate`、`over_cap` 两个拒绝理由。

### Deprecated

- `memgarden.scoring.relevance`（含 `select_relevant_context_memories_with_trace`、`select_context_memories(_with_trace)`、`memory_relevance_details`）：保留一个版本、行为不变，自动想起请改用 `retrieval.select_context`。
