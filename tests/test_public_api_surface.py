"""公开 API 面的快照。

宿主（io 是第一个）要从「直接 import 内部零件」切到「只 import 公开合同」。
那条宿主侧守卫的依据是 ``memgarden.STABLE_MODULES`` 和每个模块的 ``__all__`` ——
所以这两样本身必须被钉住：

  · 删一个名字 → 宿主的 import 在升级后才炸，这里先红
  · 加一个名字 → 等于多承诺一份长期兼容，也要有人显式改快照

改快照是**合法操作**，但必须是有意的：同时更新 CHANGELOG 的 Unreleased 段，
删名字前先确认宿主已经不用（至少走过一个 deprecated 版本）。
"""
from __future__ import annotations

import importlib
import pathlib
import sys

_SRC_PATH = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

import memgarden  # noqa: E402

TOP_LEVEL = {
    "STABLE_MODULES",
    "GardenComponent", "MountedGarden", "Scope", "OperationReceipt",
    "MaintenanceCheck", "MountPermissionError", "StorageCapabilityError",
    "SqliteStore", "Service", "manifest", "schemas", "ERROR_CODES",
    "GardenCapabilities", "CaptureSession", "MaintenanceSession",
    "ModelPort", "ClockPort", "SystemClock", "Actor",
    "CaptureRequest", "CaptureResult", "ImportRequest", "ImportProgress",
    "CuratedWriteRequest", "ExportRequest", "ExportResult", "PromoteRequest",
    "MigrateRequest", "MigrateResult", "BrowseItem", "Step",
    "ContextRequest", "ContextResult", "MaintenanceRequest", "MaintenanceResult",
    "ToolCall", "ToolDefinition", "ToolResult", "SCHEMA_VERSION",
}

STABLE = {
    "memgarden.contracts": {
        "SCHEMA_VERSION", "Mount", "DEFAULT_MOUNT", "Actor", "Step", "StepSink",
        "CaptureRequest", "CaptureResult", "ImportRequest", "CuratedWriteRequest",
        "ExportRequest", "ExportResult", "PromoteRequest", "MigrateRequest",
        "MigrateResult", "ContextRequest", "ContextResult", "MaintenanceRequest",
        "MaintenanceResult", "ToolDefinition", "ToolCall", "ToolResult",
        "BrowseItem", "to_browse_item",
    },
    "memgarden.selection": {
        "Pick", "SelectionResult", "SelectionPolicy", "Stage", "Chain",
        "RoleStage", "RecentStage", "RelevanceStage",
    },
    "memgarden.timestamps": {"parse_ts", "sort_key", "now_iso", "normalize"},
    "memgarden.text.card_guard": {
        "guard_enabled", "hard_field_pollution_reason", "field_pollution_reason",
        "bucket_pollution_reason", "default_bucket_for_text",
    },
    "memgarden.text.card_text": {
        "extract_json_block", "card_text_rejection", "placeholder_reason",
        "sanitize_card_labels", "count_user_token_residuals",
        "is_retryable_parse_error", "build_truncation_retry_prompt",
    },
    "memgarden.text.leak_signals": {"LeakSignals", "GENERIC_SIGNALS", "combine"},
    "memgarden.guards.dream_gates": {
        "known_id_in_text", "result_id_leak", "blast_radius_exceeded",
    },
    "memgarden.prompts.recall_fields": {"retrieval_cues"},
    "memgarden.prompts.buckets": {
        "COMMON_BUCKETS_V1", "COMMON_BUCKETS_GUIDANCE_V1", "MEMORY_WRITE_GUIDANCE_V1",
        "MEMORY_WRITE_RULES_V1", "BUCKET_SETS", "UnknownBucketLocaleError",
        "bucket_list", "common_buckets_guidance", "normalize_bucket_language",
    },
    "memgarden.dreaming": {
        "DREAM_SOURCE", "DreamSnapshot", "DreamLedger", "DreamVerdict",
        "dream_snapshot", "needs_dream", "dream_idempotency_key",
    },
    "memgarden.observability": {
        "query_fingerprint", "injection_record", "injection_summary",
        "assert_content_free",
    },
    "memgarden.garden_language": {
        "split_bucket_names", "count_bucket_languages", "decide_garden_language",
    },
    "memgarden.policies": {
        "CapturePolicy", "CONVERSATION_CAPTURE", "HISTORY_IMPORT", "CURATED_ARCHIVE",
        "POLICIES", "DEFAULT_POLICY", "UnknownPolicyError", "get_policy",
        "language_rule", "RESTRAINT_RULE_QUOTE", "HISTORY_IMPORT_OPENING_RUBRIC",
        "HISTORY_IMPORT_FILTER_RUBRIC", "KEEP_ALL_MAP_SUFFIX", "KEEP_ALL_WRITE_SUFFIX",
    },
    "memgarden.retrieval": {
        "RANKING_VERSION", "DEFAULT_STOPWORDS", "DEFAULT_MIN_COVERAGE",
        "DEFAULT_STRONG_EVIDENCE", "Tokenizer", "DefaultTokenizer",
        "Hit", "RankResult", "SearchLimitExceeded", "default_search_text", "rank",
        "DEFAULT_QUOTAS", "select_context",
    },
}

#: io（release/memory-overhaul，backend + tools，不含测试）2026-09-15 实际用到的名字。
#: 每一个都必须在公开合同里 —— 否则 io 切到「只 import 公开 API」时无路可走。
#: ``from memgarden import timestamps`` 这种「从包里取子模块」按模块处理。
IO_USES = {
    "memgarden": {"GardenComponent"},
    "memgarden.contracts": {"Step", "CaptureRequest", "MaintenanceRequest", "MigrateRequest"},
    "memgarden.timestamps": {"parse_ts", "sort_key", "now_iso", "normalize"},
    "memgarden.text.card_guard": {
        "guard_enabled", "hard_field_pollution_reason", "field_pollution_reason",
        "bucket_pollution_reason", "default_bucket_for_text",
    },
    "memgarden.text.card_text": {
        "extract_json_block", "card_text_rejection", "placeholder_reason",
        "sanitize_card_labels", "count_user_token_residuals",
        "is_retryable_parse_error", "build_truncation_retry_prompt",
    },
    "memgarden.text.leak_signals": {"GENERIC_SIGNALS", "LeakSignals", "combine"},
    "memgarden.guards.dream_gates": {"blast_radius_exceeded"},
    "memgarden.prompts.buckets": {
        "MEMORY_WRITE_GUIDANCE_V1", "MEMORY_WRITE_RULES_V1", "COMMON_BUCKETS_V1",
        "COMMON_BUCKETS_GUIDANCE_V1", "normalize_bucket_language",
    },
    "memgarden.dreaming": {
        "DreamLedger", "DreamSnapshot", "dream_idempotency_key", "dream_snapshot",
        "needs_dream",
    },
    "memgarden.observability": {"injection_record"},
    "memgarden.garden_language": {
        "count_bucket_languages", "decide_garden_language", "split_bucket_names",
    },
    "memgarden.policies": {
        "CONVERSATION_CAPTURE", "RESTRAINT_RULE_QUOTE", "HISTORY_IMPORT_OPENING_RUBRIC",
        "HISTORY_IMPORT_FILTER_RUBRIC", "KEEP_ALL_MAP_SUFFIX", "KEEP_ALL_WRITE_SUFFIX",
        "language_rule",
    },
}

#: io 仍在用、但**刻意不升格**的内部零件，以及它们的出路。清单只许变短。
IO_INTERNAL_WITH_EXIT = {
    "memgarden.prompts.capture": "io 的 capture_prompt_v1 垫片；落卡走 GardenComponent，垫片删除",
    "memgarden.prompts.dream": "io 的 dream_prompt_v1 垫片；V1 Dream 改走 maintenance_session",
    "memgarden.scoring.relevance": "自动想起改用 memgarden.retrieval.select_context",
    "memgarden.scoring.selector": "io 的 docker e2e 工具；改用 memgarden.retrieval.rank",
}


def test_top_level_all_is_snapshotted():
    assert set(memgarden.__all__) == TOP_LEVEL
    assert len(memgarden.__all__) == len(set(memgarden.__all__)), "顶层 __all__ 有重复项"
    missing = [n for n in memgarden.__all__ if not hasattr(memgarden, n)]
    assert not missing, f"顶层 __all__ 列了但取不到：{missing}"


def test_stable_module_list_is_snapshotted():
    assert set(memgarden.STABLE_MODULES) == set(STABLE)
    assert len(memgarden.STABLE_MODULES) == len(set(memgarden.STABLE_MODULES))


def test_each_stable_module_all_is_snapshotted_and_resolvable():
    for name, expected in STABLE.items():
        module = importlib.import_module(name)
        exported = getattr(module, "__all__", None)
        assert exported is not None, f"{name} 没有 __all__"
        assert set(exported) == expected, (
            f"{name}.__all__ 变了：多了 {sorted(set(exported) - expected)}，"
            f"少了 {sorted(expected - set(exported))}"
        )
        missing = [n for n in exported if not hasattr(module, n)]
        assert not missing, f"{name}.__all__ 列了但取不到：{missing}"


def test_every_name_io_imports_today_is_public():
    for module_name, names in IO_USES.items():
        if module_name == "memgarden":
            public = set(memgarden.__all__)
        else:
            assert module_name in memgarden.STABLE_MODULES, f"{module_name} 不在 STABLE_MODULES"
            public = set(importlib.import_module(module_name).__all__)
        assert names <= public, f"io 用到的 {module_name}.{sorted(names - public)} 不在公开合同里"


def test_internal_modules_io_still_uses_are_not_silently_promoted():
    """出路清单上的模块不许混进 STABLE_MODULES —— 升格要显式改这张表。"""
    assert not set(IO_INTERNAL_WITH_EXIT) & set(memgarden.STABLE_MODULES)
