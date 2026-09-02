"""JSON Schema —— 给非 Python 的 Runtime 用（sevenfloor 2026-09-02 §3.8）。

## 为什么需要它

Python 这边靠 dataclass 就够了。但接入方可能是 TypeScript（DeepSeek Harness
就是），它没法读 Python 的 dataclass。让对方照着文档手抄一份类型，等于埋下
一份会漂的副本 —— 而漂了不会报错，只会在某个字段上悄悄对不上。

所以把契约导成机器可读的 JSON Schema：对方可以生成类型、可以做运行时校验、
可以和我们跑同一组 golden fixtures。

## 设计约定

- **union 一律带 discriminator**（``op`` 之于 mutation），不靠「试着解析看哪个成」；
- **版本号语义明确**：``schema_version`` 变了就是不兼容，没变就是兼容；
- **未知字段一律保留、不报错**：新版本加字段时旧调用方不该崩；
- 错误是**结构化 code**，不要求调用方解析人话字符串。

## 不做的事

不定义「所有记忆系统通用」的 Record schema —— 这是 **Garden 自己**的契约。
别的记忆系统有别的结构，硬套一个最小公分母只会让两边都难受。
"""
from __future__ import annotations

import json
from typing import Any

from .contracts import SCHEMA_VERSION
from .records import RECORD_SCHEMA_VERSION

#: mutation 线上格式的版本。**加字段不动它，改语义才动。**
MUTATION_SCHEMA_VERSION = 1

_STR = {"type": "string"}
_OPT_STR = {"type": "string", "default": ""}


def _card() -> dict:
    return {
        "type": "object",
        "required": ["summary", "content"],
        "properties": {
            "id": _OPT_STR,
            "summary": _STR,
            "content": _STR,
            "bucket": _OPT_STR,
            "threads": {"type": "array", "items": _STR, "default": []},
            "mount": _OPT_STR,
        },
        # 未知字段放行 —— 新版本加字段时旧调用方不该崩。
        "additionalProperties": True,
    }


def _mutation() -> dict:
    """typed mutation 的 union。**判别键是 ``op``。**

    每个分支列全自己的必填字段，这样对方的运行时校验能给出「缺哪个字段」这种
    具体错误，而不是「不匹配任何分支」。
    """
    base = {"mount": _OPT_STR, "idempotency_key": _OPT_STR,
            "expected_revision": {"type": ["string", "null"]}}
    return {
        "type": "object",
        "required": ["op"],
        "discriminator": {"propertyName": "op"},
        "oneOf": [
            {"title": "add", "type": "object",
             "required": ["op", "card"],
             "properties": {**base, "op": {"const": "add"}, "card": _card()}},
            {"title": "update", "type": "object",
             "required": ["op", "record_id", "changes"],
             "properties": {**base, "op": {"const": "update"},
                            "record_id": _STR,
                            "changes": {"type": "object"}}},
            {"title": "supersede", "type": "object",
             # target_id 和 target_ids 二选一 —— 见 records.Supersede 的说明：
             # 整理是「N 张旧卡收敛成 1 张」，只有单数表达不了。
             "required": ["op", "card"],
             "properties": {**base, "op": {"const": "supersede"},
                            "target_id": _OPT_STR,
                            "target_ids": {"type": "array", "items": _STR,
                                           "default": []},
                            "card": _card(),
                            "rationale": _OPT_STR,
                            "consolidation_op": {
                                "type": "string",
                                "enum": ["merge", "thicken", "supersede", ""],
                                "default": ""}},
             "anyOf": [{"required": ["target_id"]},
                       {"required": ["target_ids"]}]},
            {"title": "archive", "type": "object",
             "required": ["op", "target_id"],
             "properties": {**base, "op": {"const": "archive"},
                            "target_id": _STR}},
            {"title": "delete", "type": "object",
             "required": ["op", "target_id"],
             "properties": {**base, "op": {"const": "delete"},
                            "target_id": _STR}},
            {"title": "no_op", "type": "object",
             "required": ["op"],
             "properties": {**base, "op": {"const": "no_op"},
                            "reason": _OPT_STR}},
        ],
    }


def _actor() -> dict:
    return {"type": "object",
            "properties": {"user_id": _OPT_STR, "agent_id": _OPT_STR,
                           "session_id": _OPT_STR},
            "additionalProperties": True}


def _scope() -> dict:
    """🔴 这个对象**必须由 Runtime 的可信上下文填**，不能来自模型的工具参数。

    schema 表达不了这条约束，所以写在这里，也写在 :mod:`memgarden.mounted`。
    """
    return {"type": "object",
            "required": ["tenant_id"],
            "properties": {
                "tenant_id": _STR,
                "actor": _actor(),
                "allowed_mounts": {"type": "array", "items": _STR,
                                   "default": ["agent-private"]},
            }}


def _receipt() -> dict:
    return {"type": "object",
            "properties": {
                "written": {"type": "boolean"},
                "record_ids": {"type": "array", "items": _STR},
                "revision": _OPT_STR,
                # 没写入的原因。**不是错误** —— 空结果是合法结果。
                "reason": _OPT_STR,
                "error": {"type": ["string", "null"]},
                "trace": {"type": "object"},
                "schema_version": {"type": "integer"},
            }}


#: 结构化错误码。**调用方按 code 分支，不要去解析后面那句人话。**
ERROR_CODES = (
    "invalid_mutation",             # 结构不合法，进 Store 之前就被拦下
    "storage_lacks_capabilities",   # 存储声明支持不了这批改动
    "revision_conflict",            # 乐观并发失败，需要重读重算
    "idempotency_conflict",         # 同一个键、不同内容
    "mount_not_allowed",            # 越权
    "query_required",
    "summary_and_content_required",
    "unknown_tool",
)


def schemas() -> dict[str, Any]:
    """全部 schema。键名就是契约名。"""
    return {
        "Mutation": _mutation(),
        "Card": _card(),
        "Actor": _actor(),
        "Scope": _scope(),
        "OperationReceipt": _receipt(),
        "ErrorCode": {"type": "string", "enum": list(ERROR_CODES)},
    }


def manifest() -> dict[str, Any]:
    """这个组件是什么、说哪个版本的协议。

    接入方**启动时**就该核对这个 —— 版本不兼容要立刻拒绝启动，而不是跑到
    第一条用户消息才失败。
    """
    return {
        "component_id": "memgarden",
        "protocol_version": f"{SCHEMA_VERSION}",
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "mutation_schema_version": MUTATION_SCHEMA_VERSION,
        "error_codes": list(ERROR_CODES),
        "schemas": sorted(schemas()),
    }


def dump(indent: int = 2) -> str:
    """导出成 JSON 文本 —— CI 里可以把它和签入的副本比对，防止悄悄漂了。"""
    return json.dumps(
        {"manifest": manifest(), "schemas": schemas()},
        ensure_ascii=False, indent=indent, sort_keys=True,
    )
