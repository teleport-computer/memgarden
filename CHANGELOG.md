# Changelog

发布记录以 GitHub Release / PyPI 为准；本文件记录尚未发布的改动，发版时并入对应版本。

## Unreleased

### Added

- **关联读取** `memgarden.related.one_hop(sources, candidates, *, cap=6)` 与
  `MountedGarden.related(scope, ids, *, cap=6, include_archived=False, include_superseded=False)`。
  取回卡时给出一跳邻居：`anchor` / `supersedes`（源卡上的显式链接）优先，其次同线索的卡；
  归档、删除的卡不出现，被取代的卡只沿显式链接出现并标 `status="superseded"`。
  v1 与宿主 io 读侧实现逐项一致，由 144 组黄金用例锁定（`tests/fixtures/related_one_hop_golden.json`）。
  `MountedGarden.related` 自己做 owner / 挂载点 / 生命周期过滤，并把参考 Store 的
  `superseded_by` 取代链当作新卡的 `supersedes` 链接。不做反向查找和多跳。
  `one_hop` 的 `cap` 必须是非负整数（io 原实现不校验）。

### Fixed

- **Dream 提示词恢复卡片正文**。自 GardenComponent 顶层接口（6e8e9b6，08-29）起，整理提示词里每张卡只有
  `- [id] 摘要`，模型做 thicken / merge 时看不到正文、只能照标题重写，旧正文随旧卡退休。
  现在由 `prompts.dream.render_dream_cards` 带正文渲染（id / bucket / threads / occurred_at /
  summary / retrieval_cues / content），`MaintenanceRequest` 新增 `cards_limit=60`、
  `cards_budget_chars=60000`、`card_body_chars=5000`、`card_summary_chars=2000`。
  总预算按整张卡累加；截断的卡标 `TRUNCATED`，提示词要求不改写它们。
  trace 新增 `cards_rendered` / `cards_truncated` / `cards_omitted` / `truncated_card_ids`；
  `known_ids` 自动并入实际渲染的卡。解析、重试和守卫不变。
  **提示词文本有变化**（Step 1 措辞、新增 TRUNCATED 规则），依赖整份提示词快照的宿主需要更新快照。
