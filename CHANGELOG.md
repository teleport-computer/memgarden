# Changelog

发布记录以 GitHub Release / PyPI 为准；本文件记录尚未发布的改动，发版时（见 [RELEASING](docs/RELEASING.md)）并入对应版本。

## Unreleased

### 历史导入：宿主驱动的分批会话（MG-8）

- 新增 `GardenComponent.import_session(request, *, progress, existing_cards, owner_key, index_ranker)`，返回 `ImportSession`：内核负责切批、提示词、解析与重问、跨批去重、单批/整次上限和 `ImportProgress` 推进；宿主调模型、写库，并用 `commit(outcome, record_ids=...)` / `fail(outcome, error)` 回报结果。给自己持有模型 key、加密和执行器的宿主（如 io）用。顶层新增导出 `ImportSession`、`ImportBatch`、`ImportBatchResult`。
- `MountedGarden.import_history` 改为跑在同一个 `ImportSession` 上（每个写卡批次前重读 Store，CAS 写回）。默认请求下续传指纹与此前逐字节一致，进行中的导入可以继续续传。
- `ImportRequest` 新增字段（均有默认值，默认时行为不变）：`batches`（宿主预切批次，可带 `label` / `occurred_from` / `occurred_to`）、`strategy`（`single_pass` 默认 / `two_pass`）、`batch_chars`、`write_batch_candidates`、`max_total_cards`、`fallback_occurred_at`、`naming_rule`、`identity`。wire `history.import` 接受前六项；`ImportProgress` 新增 `strategy`、`cards_added`、`candidates`、`candidates_cursor`。
- `two_pass`：每批先抽候选事实（`prompts/history_import.py`），读完材料后分组交给 Capture 提示词统一写卡。⚠️ 这时 `ImportProgress.candidates` 含用户内容，宿主要按记忆正文等级保存进度；候选总数上限 4000。
- 导入批次的「已有记忆索引」改为按本批文字挑相关旧卡（四分之一名额留给重要度最高的卡），此前只取重要度前 60，大导入的后续批次看不到前面写的卡。宿主可注入 `index_ranker`。
- 跨批桶名确定性收敛（大小写/空白、通用桶的双语斜杠写法）。
- 修复：`history_import` 档单段式提示词里拼了两段式「候选阶段、去重在后面」的开场，与要求输出最终卡的模板矛盾。单段式改用 `HISTORY_IMPORT_CARD_OPENING_RUBRIC`；`HISTORY_IMPORT_OPENING_RUBRIC` 原文保留（两段式抽候选和宿主 io 的 fact_map 在用）。**这会改变 `history_import` 档的单段式提示词**，新增提示词快照测试守着。
- 兼容注意：`MountedGarden.import_history` 不再经过 `capture_and_store`；monkeypatch 该方法来伪造导入回执的调用方需要改为注入模型。
