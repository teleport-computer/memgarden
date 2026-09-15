"""Memory Garden —— 可挂进任意 Runtime 的记忆编辑内核。

这个包提供两层：``GardenComponent`` 只做判断；``MountedGarden`` 把可替换的
``StoragePort`` 接上，统一执行 load、CAS、幂等、生命周期与整理账本。能力包括：

  · 什么值得记（三个策略档位各一把尺子）
  · 怎么归桶起线索、怎么校验模型输出、怎么去重
  · 这轮该想起哪几张（打分排序）
  · 要不要整理了、整理时怎么合并消矛盾

不在这里的（由宿主提供）：

  模型/provider 与凭证 · 加解密 · 认证和可信 Scope 装配 · 审计 ·
  对话读取 · 定时器/队列 · 生产存储选型

硬指标：**本包只依赖 Python 标准库**，不 import 任何宿主模块。
一旦这条破了，「内核可独立发布 / 记忆库可被替换」就都不成立
（宿主侧应当有一条守卫测试盯着这件事 —— io 用的是 AST 扫描）。

边界与四个插口见 ``README.md``。

## 从这里开始

    from memgarden import GardenComponent, CaptureRequest

    garden = GardenComponent(model=my_model)          # 模型由你提供，key 不给它
    result = garden.capture(CaptureRequest(window=对话, locale="zh-Hans"))
    my_store.apply("tenant", result.mutations, owner="user-42",
                   idempotency_key="turn-1", expected_revision=None)

``STABLE_MODULES`` 列出的子模块（时间戳、文本闸、桶名、整理门槛、召回线索等
宿主工具函数）是**稳定公开合同**，按各自 ``__all__`` 使用。其余模块（``prompts.capture``
/ ``prompts.dream`` / ``scoring`` / ``rendering`` …）是**内部零件**。它们仍然公开、可以直接用（高级用法、
单元测试、想自己重新编排），但**普通接入不需要认识它们** —— 认识了就等于
把编排知识抄进了你的代码，Garden 内部一改你就得跟着改。
"""

from .component import (
    CaptureSession,
    GardenCapabilities,
    GardenComponent,
    MaintenanceSession,
)
from .contracts import (
    SCHEMA_VERSION,
    Actor,
    BrowseItem,
    CuratedWriteRequest,
    ExportRequest,
    ExportResult,
    ImportRequest,
    MigrateRequest,
    MigrateResult,
    PromoteRequest,
    Step,
    CaptureRequest,
    CaptureResult,
    ContextRequest,
    ContextResult,
    SearchRequest,
    SearchResult,
    MaintenanceRequest,
    MaintenanceResult,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from .mounted import (
    MaintenanceCheck,
    MountPermissionError,
    MountedGarden,
    OperationReceipt,
    Scope,
    StorageCapabilityError,
)
from .importing import ImportBatch, ImportBatchResult, ImportProgress, ImportSession
from .ports import ClockPort, ModelPort, SystemClock
from .schema import ERROR_CODES, manifest, schemas
from .service import Service
# 官方参考存储。放在顶层是**刻意的**：接入方需要一个能直接用的 store，
# 逼他们去 `memgarden.stores.sqlite` 挖，等于告诉他们「内部模块可以随便进」。
from .stores.sqlite import SqliteStore

#: 顶层之外、**承诺稳定**的子模块。宿主可以 import 这些模块里 ``__all__`` 列出的名字；
#: 列表之外的子模块（``prompts.capture``、``scoring.*``、``rendering`` …）仍是内部零件，
#: 随时可能改名或删除。宿主侧的「只 import 公开 API」守卫以这份清单为准，
#: ``tests/test_public_api_surface.py`` 对清单和每个模块的 ``__all__`` 做快照。
STABLE_MODULES: tuple[str, ...] = (
    "memgarden.contracts",
    "memgarden.selection",
    "memgarden.timestamps",
    "memgarden.text.card_guard",
    "memgarden.text.card_text",
    "memgarden.text.leak_signals",
    "memgarden.guards.dream_gates",
    "memgarden.prompts.recall_fields",
    "memgarden.prompts.buckets",
    "memgarden.dreaming",
    "memgarden.observability",
    "memgarden.garden_language",
    "memgarden.policies",
    "memgarden.retrieval",
    "memgarden.related",
)

__all__ = [
    "STABLE_MODULES",
    "GardenComponent",
    "MountedGarden",
    "Scope",
    "OperationReceipt",
    "MaintenanceCheck",
    "MountPermissionError",
    "StorageCapabilityError",
    "SqliteStore",
    "Service",
    "manifest",
    "schemas",
    "ERROR_CODES",
    "GardenCapabilities",
    "CaptureSession",
    "MaintenanceSession",
    "ModelPort",
    "ClockPort",
    "SystemClock",
    "Actor",
    "CaptureRequest",
    "CaptureResult",
    "ImportRequest",
    "ImportProgress",
    "ImportSession",
    "ImportBatch",
    "ImportBatchResult",
    "CuratedWriteRequest",
    "ExportRequest",
    "ExportResult",
    "PromoteRequest",
    "MigrateRequest",
    "MigrateResult",
    "BrowseItem",
    "Step",
    "ContextRequest",
    "ContextResult",
    "SearchRequest",
    "SearchResult",
    "MaintenanceRequest",
    "MaintenanceResult",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "SCHEMA_VERSION",
]
