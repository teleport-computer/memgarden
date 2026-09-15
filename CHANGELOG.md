# Changelog

发布记录以 Git tag、GitHub Release 和 PyPI 为准；这里按版本记录对接入方可见的变化。

## Unreleased

### Added

- `memgarden.STABLE_MODULES`：顶层之外承诺稳定的子模块清单；这些模块都有 `__all__`，并由 `tests/test_public_api_surface.py` 快照。宿主工具函数（`timestamps`、`text.card_guard`、`text.card_text`、`text.leak_signals`、`guards.dream_gates`、`prompts.recall_fields`、`prompts.buckets`、`dreaming`、`observability`、`garden_language`、`policies`）以及 `contracts`、`selection` 从此是公开合同。
- `memgarden.retrieval.rank(query, candidates, *, tokenizer=None, ...)`：BM25 词法排序，数学逐项移植自 io 的 `memory_bm25`（给同一分词器时分数逐位相同）。分词器是插口（`Tokenizer` 协议，`name` 进排序版本号）；不注入时用零依赖的 `DefaultTokenizer`（整段 ASCII 标识符 + CJK 单字与二字）。返回 `RankResult(hits, version, trace)`，`version` 形如 `memgarden-bm25-v1+tok:mg-default-v1`，trace 内容无关。查询和每张卡在单次调用内只分析一次，不跨调用缓存。可选资源上限超出时抛 `SearchLimitExceeded`。
