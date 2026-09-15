# Changelog

发布记录以 Git tag、GitHub Release 和 PyPI 为准；这里按版本记录对接入方可见的变化。

## Unreleased

### Added

- `memgarden.STABLE_MODULES`：顶层之外承诺稳定的子模块清单；这些模块都有 `__all__`，并由 `tests/test_public_api_surface.py` 快照。宿主工具函数（`timestamps`、`text.card_guard`、`text.card_text`、`text.leak_signals`、`guards.dream_gates`、`prompts.recall_fields`、`prompts.buckets`、`dreaming`、`observability`、`garden_language`、`policies`）以及 `contracts`、`selection` 从此是公开合同。
