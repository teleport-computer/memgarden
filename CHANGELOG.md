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
