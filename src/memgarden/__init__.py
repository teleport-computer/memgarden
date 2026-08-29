"""Memory Garden 内核 —— 记忆的判断力，与宿主环境无关。

这个包只做判断，不做执行：

  · 什么值得记（三个策略档位各一把尺子）
  · 怎么归桶起线索、怎么校验模型输出、怎么去重
  · 这轮该想起哪几张（打分排序）
  · 要不要整理了、整理时怎么合并消矛盾

不在这里的（由调用方提供）：

  加解密与 enclave · 身份装配 · 所有权校验 · gates · 审计 ·
  锁与事务 · 捞聊天记录 · 定时器 · 真正调模型

硬指标：**本包只依赖标准库和同源的 agent-protocol-core**，不 import 任何宿主模块。
一旦这条破了，「内核可独立发布 / 记忆库可被替换」就都不成立
（宿主侧应当有一条守卫测试盯着这件事 —— io 用的是 AST 扫描）。

边界与四个插口见 ``README.md``。

## 从这里开始

    from memgarden import GardenComponent, CaptureRequest

    garden = GardenComponent(model=my_model)          # 模型由你提供，key 不给它
    result = garden.capture(CaptureRequest(window=对话, locale="zh-Hans"))
    my_store.apply(result.mutations)                   # 落库是你的事

``GardenComponent`` 之下的模块（``prompts`` / ``scoring`` / ``selection`` /
``dreaming`` / ``text``）是**内部零件**。它们仍然公开、可以直接用（高级用法、
单元测试、想自己重新编排），但**普通接入不需要认识它们** —— 认识了就等于
把编排知识抄进了你的代码，Garden 内部一改你就得跟着改。
"""

from .component import GardenCapabilities, GardenComponent
from .contracts import (
    SCHEMA_VERSION,
    Actor,
    BrowseItem,
    CuratedWriteRequest,
    ExportRequest,
    ExportResult,
    ImportRequest,
    PromoteRequest,
    Step,
    CaptureRequest,
    CaptureResult,
    ContextRequest,
    ContextResult,
    MaintenanceRequest,
    MaintenanceResult,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from .ports import ClockPort, ModelPort, SystemClock

__all__ = [
    "GardenComponent",
    "GardenCapabilities",
    "ModelPort",
    "ClockPort",
    "SystemClock",
    "Actor",
    "CaptureRequest",
    "CaptureResult",
    "ImportRequest",
    "CuratedWriteRequest",
    "ExportRequest",
    "ExportResult",
    "PromoteRequest",
    "BrowseItem",
    "Step",
    "ContextRequest",
    "ContextResult",
    "MaintenanceRequest",
    "MaintenanceResult",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "SCHEMA_VERSION",
]
