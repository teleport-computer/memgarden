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
_NONEMPTY_STR = {"type": "string", "minLength": 1}
_OPT_STR = {"type": "string", "default": ""}
_WIRE_ID = {"type": ["string", "integer", "null"]}


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
            "importance": {"type": "number", "default": 0.5},
            "pulse": {"type": "number", "default": 0.0},
            "occurred_at": _OPT_STR,
            "role": _OPT_STR,
            "is_sensitive": {"type": "boolean", "default": False},
            "source": _OPT_STR,
            "source_material_kind": _OPT_STR,
            "source_actor": _actor(),
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
            # record_id 是正式字段名（和 dataclass 一致）；target_id 是老写法，
            # 解析时会被规范化成 record_id，这里只作为兼容 alias 列出。
            {"title": "archive", "type": "object",
             "required": ["op"],
             "properties": {**base, "op": {"const": "archive"},
                            "record_id": _STR, "target_id": _OPT_STR,
                            "reason": _OPT_STR},
             "anyOf": [{"required": ["record_id"]},
                       {"required": ["target_id"]}]},
            {"title": "delete", "type": "object",
             # requested_by 必填：删除必须能追溯到是谁要求的。
             "required": ["op", "requested_by"],
             "properties": {**base, "op": {"const": "delete"},
                            "record_id": _STR, "target_id": _OPT_STR,
                            "requested_by": _STR, "reason": _OPT_STR},
             "anyOf": [{"required": ["record_id"]},
                       {"required": ["target_id"]}]},
            {"title": "promote", "type": "object",
             "required": ["op", "record_id", "to_mount"],
             "properties": {**base, "op": {"const": "promote"},
                            "record_id": _STR, "to_mount": _STR,
                            "reason": _OPT_STR}},
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
            # 🔴 memory_owner_id 是**必填**，schema 必须和运行时一致。
            # 少了它，接入方按 schema 生成的请求在本地校验通过、
            # 到服务端才失败 —— 而 schema 存在的全部意义就是让对方
            # 在本地就知道自己传对没有。
            "required": ["tenant_id", "memory_owner_id"],
            "properties": {
                "tenant_id": _STR,
                # 这座花园的稳定所有者。tenant 是安全边界，owner 是归属人；
                # 只有 tenant 的话，同租户下两个 agent 会互相读到对方的
                # agent-private。
                "memory_owner_id": _STR,
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


def _context_result() -> dict:
    return {
        "type": "object",
        "required": ["record_ids", "blocks"],
        "properties": {
            "record_ids": {"type": "array", "items": _STR},
            "blocks": {"type": "array", "items": {
                "type": "object",
                "required": ["type", "record_ref", "text", "mount", "stage"],
                "properties": {
                    "type": {"const": "memory"}, "record_ref": _STR,
                    "text": _STR, "mount": _STR, "stage": _STR,
                },
                "additionalProperties": True,
            }},
            "trace": {"type": "object"},
            "schema_version": {"type": "integer"},
        },
        "additionalProperties": True,
    }


def _browse_item() -> dict:
    return {
        "type": "object",
        "required": ["record_ref", "display_text", "mount"],
        "properties": {
            "record_ref": _STR, "display_text": _STR, "mount": _STR,
            "occurred_at": _OPT_STR, "updated_at": _OPT_STR,
            "provider": _OPT_STR, "group_label": _OPT_STR,
            "tags": {"type": "array", "items": _STR},
        },
        "additionalProperties": True,
    }


def _record() -> dict:
    return {
        "type": "object",
        "required": ["record_id", "card", "mount", "lifecycle"],
        "properties": {
            "record_id": _STR, "card": {"$ref": "#/schemas/Card"},
            "mount": _STR,
            "lifecycle": {"type": "string", "enum": [
                "active", "archived", "superseded", "deleted"]},
            "revision": _OPT_STR, "created_at": _OPT_STR,
            "updated_at": _OPT_STR, "superseded_by": _OPT_STR,
            "schema_version": {"type": "integer"},
        },
        "additionalProperties": True,
    }


def _export_result() -> dict:
    return {
        "type": "object",
        "required": ["records", "counts"],
        "properties": {
            "records": {"type": "array",
                        "items": {"$ref": "#/schemas/Card"}},
            "counts": {"type": "object"},
            "schema_version": {"type": "integer"},
        },
        "additionalProperties": True,
    }


def _page_result(items: dict) -> dict:
    return {
        "type": "object",
        "required": ["items", "next_cursor", "total"],
        "properties": {
            "items": items, "next_cursor": _STR,
            "total": {"type": "integer"},
            "schema_version": {"type": "integer"},
        },
        "additionalProperties": True,
    }


def _tool_result() -> dict:
    return {
        "type": "object", "required": ["ok"],
        "properties": {
            "ok": {"type": "boolean"}, "content": _OPT_STR,
            "mutations": {"type": "array",
                          "items": {"$ref": "#/schemas/Mutation"}},
            "error": {"type": ["string", "null"]},
            "schema_version": {"type": "integer"},
        },
        "additionalProperties": True,
    }


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
    # -- 服务层（长驻 serve 的分发边界） ------------------------------- #
    "unknown_method",               # 没有这个方法
    "invalid_request",              # 参数不合法
    "invalid_json",                 # stdio 收到的不是合法 JSON
    "scope_required",               # 缺可信 tenant scope
    "memory_owner_required",        # 缺稳定的记忆 owner
    "unknown_session",              # capture 会话不存在/已取消/服务重启过
    "session_capacity",             # 在途 host-driven 会话达到服务容量上限
    "model_not_configured",         # 服务没配模型，但这个方法需要模型
    # 一批改动写了一半 —— 既不是成功也不是失败。调用方要看回执里的
    # applied / failed_at，只重放剩下的那部分。
    "partial_failure",
    "internal_error",               # 兜底：出到这个码就是我们的 bug
)


# --------------------------------------------------------------------------- #
# 方法级契约：每个 method 的 request / response
# --------------------------------------------------------------------------- #
#
# ## 为什么光有 Card/Mutation 的 schema 不够
#
# 陌生 Runtime 拿到「Mutation 长这样」之后，仍然不知道 ``capture.run`` 该传
# 什么、会回什么。他只能读我们的 Python 源码或者靠试 —— 而试出来的理解
# 会在某个可选字段上悄悄跑偏，且不报错。
#
# 所以每个方法都要有 request/response schema，并且**在执行前**按它校验输入：
# 早拒绝的错误消息说得清「哪个字段不对」，晚失败的错误消息只会说
# 「NoneType 没有 strip」。

def _scope_ref() -> dict:
    return {"$ref": "#/schemas/Scope"}


def _error_envelope() -> dict:
    """所有失败响应的统一形状。**code 是稳定的，message 不是。**"""
    return {
        "type": "object",
        "required": ["ok", "error"],
        "properties": {
            "id": _WIRE_ID,
            "ok": {"type": "boolean", "enum": [False]},
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "enum": list(ERROR_CODES)},
                    "message": _STR,
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }


def _ok_envelope(result: dict) -> dict:
    success = {
        "type": "object",
        "required": ["ok", "result"],
        "properties": {"id": _WIRE_ID,
                       "ok": {"type": "boolean", "enum": [True]},
                       "result": result},
        "additionalProperties": True,
    }
    return {"oneOf": [success, {"$ref": "#/schemas/ErrorEnvelope"}]}


def _page() -> dict:
    """分页游标。**浏览和导出必须有界** —— 不分页的 export 在几万张卡的
    花园上会一次性把整座花园塞进一条响应，宿主那边直接 OOM。"""
    return {
        "limit": {"type": "integer", "default": 100, "minimum": 1,
                  "maximum": 1000},
        "cursor": _OPT_STR,
    }


def _import_progress() -> dict:
    """导入进度。宿主**要把它存起来** —— 断点续跑全靠它。"""
    return {
        "type": "object",
        "properties": {
            "cursor": {"type": "integer", "default": 0},
            "total": {"type": "integer", "default": 0},
            "source_digest": _OPT_STR,
            "import_fingerprint": _OPT_STR,
            "batches_done": {"type": "integer", "default": 0},
            "cards_written": {"type": "integer", "default": 0},
            "skipped": {"type": "array", "items": {"type": "object"}},
            "failed": {"type": "array", "items": {"type": "object"}},
            "done": {"type": "boolean"},
            "percent": {"type": "integer"},
            "schema_version": {"type": "integer", "default": 1},
        },
        "additionalProperties": True,
    }


def _maintenance_state() -> dict:
    """整理账本。作用域 (tenant, memory_owner, mount)。

    它和卡改动在**同一次提交**里落地 —— 分开写的两种坏法都很隐蔽，
    见 :mod:`memgarden.storage` 里 ``apply`` 的说明。
    """
    return {
        "type": "object",
        "properties": {
            "mount": _OPT_STR,
            "signature": _OPT_STR,
            "seed_card_count": {"type": "integer", "default": 0},
            "revision": _OPT_STR,
            "updated_at": _OPT_STR,
            "schema_version": {"type": "integer", "default": 1},
        },
        "additionalProperties": True,
    }


def method_schemas() -> dict[str, Any]:
    """每个 Service 方法的 request / response。"""
    receipt = {"$ref": "#/schemas/OperationReceipt"}
    session_state = {"oneOf": [
        {
            "type": "object",
            "required": ["session_id", "status", "next_prompt"],
            "properties": {
                "session_id": _STR, "status": {"const": "needs_model"},
                "next_prompt": _STR, "retrying_after": _OPT_STR,
            },
            "additionalProperties": True,
        },
        {
            "type": "object", "required": ["status", "result"],
            "properties": {"status": {"const": "completed"},
                           "result": receipt},
            "additionalProperties": True,
        },
    ]}
    cancelled = {
        "type": "object", "required": ["cancelled"],
        "properties": {"cancelled": {"type": "boolean"}},
        "additionalProperties": True,
    }
    capture_req = {
        "type": "object",
        "required": ["scope", "window", "locale"],
        "properties": {
            "scope": _scope_ref(),
            "window": _STR,
            "locale": _STR,
            "ai_name": _OPT_STR,
            "user_name": _OPT_STR,
            "idempotency_key": _OPT_STR,
        },
        "additionalProperties": True,
    }
    return {
        "manifest.get": {"request": {"type": "object"},
                         "response": _ok_envelope({"type": "object"})},
        "schema.get": {"request": {"type": "object"},
                       "response": _ok_envelope({"type": "object"})},
        "health.get": {"request": {"type": "object"},
                       "response": _ok_envelope({"type": "object"})},
        "capture.run": {"request": capture_req,
                        "response": _ok_envelope(receipt)},
        "capture.begin": {
            "request": capture_req,
            "response": _ok_envelope(session_state),
        },
        "capture.feed": {
            "request": {"type": "object",
                        "required": ["session_id", "reply"],
                        "properties": {"session_id": _STR, "reply": _STR,
                                       "truncated": {"type": "boolean",
                                                     "default": False},
                                       "finish_reason": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(session_state),
        },
        "capture.cancel": {
            "request": {"type": "object", "required": ["session_id"],
                        "properties": {"session_id": _STR},
                        "additionalProperties": True},
            "response": _ok_envelope(cancelled),
        },
        "context.get": {
            "request": {"type": "object", "required": ["scope", "query"],
                        "properties": {"scope": _scope_ref(), "query": _STR,
                                       "limit": {"type": "integer",
                                                 "default": 8},
                                       "mount": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(
                {"$ref": "#/schemas/ContextResult"}),
        },
        "maintenance.check": {
            "request": {"type": "object", "required": ["scope"],
                        "properties": {"scope": _scope_ref(),
                                       "mount": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope({
                "type": "object", "required": ["needed"],
                "properties": {"needed": {"type": "boolean"},
                               "reason": _OPT_STR,
                               "error": {"type": ["string", "null"]},
                               "trace": {"type": "object"},
                               "schema_version": {"type": "integer"}},
                "additionalProperties": True}),
        },
        "maintenance.run": {
            "request": {"type": "object", "required": ["scope", "locale"],
                        "properties": {"scope": _scope_ref(), "locale": _STR,
                                       "mount": _OPT_STR,
                                       "ai_name": _OPT_STR,
                                       "user_name": _OPT_STR,
                                       "recent_conversations": _OPT_STR,
                                       "idempotency_key": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(receipt),
        },
        "maintenance.begin": {
            "request": {"type": "object", "required": ["scope", "locale"],
                        "properties": {"scope": _scope_ref(), "locale": _STR,
                                       "mount": _OPT_STR,
                                       "ai_name": _OPT_STR,
                                       "user_name": _OPT_STR,
                                       "recent_conversations": _OPT_STR,
                                       "idempotency_key": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(session_state),
        },
        "maintenance.feed": {
            "request": {"type": "object",
                        "required": ["session_id", "reply"],
                        "properties": {"session_id": _STR, "reply": _STR,
                                       "truncated": {"type": "boolean",
                                                     "default": False},
                                       "finish_reason": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(session_state),
        },
        "maintenance.cancel": {
            "request": {"type": "object", "required": ["session_id"],
                        "properties": {"session_id": _STR},
                        "additionalProperties": True},
            "response": _ok_envelope(cancelled),
        },
        "records.browse": {
            "request": {"type": "object", "required": ["scope"],
                        "properties": {"scope": _scope_ref(),
                                       "include_archived": {"type": "boolean",
                                                            "default": False},
                                       **_page()},
                        "additionalProperties": True},
            "response": _ok_envelope({"$ref": "#/schemas/BrowsePage"}),
        },
        "records.export": {
            "request": {"type": "object", "required": ["scope"],
                        "properties": {"scope": _scope_ref(),
                                       "include_archived": {"type": "boolean",
                                                            "default": True},
                                       **_page()},
                        "additionalProperties": True},
            "response": _ok_envelope({"$ref": "#/schemas/ExportPage"}),
        },
        "records.write": {
            "request": {"type": "object", "required": ["scope", "text"],
                        "properties": {"scope": _scope_ref(), "text": _STR,
                                       "bucket": _OPT_STR, "mount": _OPT_STR,
                                       "locale": _OPT_STR,
                                       "idempotency_key": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(receipt),
        },
        "records.promote": {
            "request": {"type": "object",
                        "required": ["scope", "record_id", "to_mount",
                                     "authorized"],
                        "properties": {"scope": _scope_ref(),
                                       "record_id": _STR, "to_mount": _STR,
                                       # 🔴 授权由**宿主**给，不是模型能决定的。
                                       "authorized": {"type": "boolean"},
                                       "reason": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(receipt),
        },
        "records.migrate": {
            "request": {"type": "object",
                        "required": ["scope", "old_cards", "allowed_ids", "locale"],
                        "properties": {"scope": _scope_ref(), "old_cards": _STR,
                                       "allowed_ids": {"type": "array",
                                                       "items": _STR,
                                                       "minItems": 1},
                                       "vocab": _OPT_STR, "mount": _OPT_STR,
                                       "locale": _NONEMPTY_STR,
                                       "ai_name": _OPT_STR,
                                       "user_name": _OPT_STR,
                                       "idempotency_key": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(receipt),
        },
        "history.import": {
            "request": {"type": "object",
                        "required": ["scope", "material", "locale"],
                        "properties": {"scope": _scope_ref(), "material": _STR,
                                       "locale": _STR,
                                       "material_kind": _OPT_STR,
                                       "max_cards": {"type": "integer",
                                                     "minimum": 1,
                                                     "default": 50},
                                       "policy": _OPT_STR, "mount": _OPT_STR,
                                       "ai_name": _OPT_STR,
                                       "user_name": _OPT_STR,
                                       "idempotency_key": _OPT_STR,
                                       # 断点续跑：把上次的 progress 传回来
                                       "progress": _import_progress(),
                                       "max_batches": {"type": "integer",
                                                       "minimum": 1}},
                        "additionalProperties": True},
            "response": _ok_envelope(_import_progress()),
        },
        "records.delete": {
            "request": {"type": "object",
                        "required": ["scope", "record_id", "requested_by"],
                        "properties": {"scope": _scope_ref(),
                                       "record_id": _STR,
                                       # 删除必须能追溯到是谁要求的。
                                       "requested_by": _STR,
                                       "reason": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(receipt),
        },
        "tool.list": {"request": {"type": "object"},
                      "response": _ok_envelope({"type": "array"})},
        "tool.invoke": {
            "request": {"type": "object", "required": ["scope", "name"],
                        "properties": {"scope": _scope_ref(), "name": _STR,
                                       "arguments": {"type": "object"}},
                        "additionalProperties": True},
            "response": _ok_envelope({"$ref": "#/schemas/ToolResult"}),
        },
    }


def schemas() -> dict[str, Any]:
    """全部 schema。键名就是契约名。"""
    return {
        "Mutation": _mutation(),
        "Card": _card(),
        "Record": _record(),
        "Actor": _actor(),
        "Scope": _scope(),
        "OperationReceipt": _receipt(),
        "ContextResult": _context_result(),
        "BrowseItem": _browse_item(),
        "ExportResult": _export_result(),
        "BrowsePage": _page_result({
            "type": "array", "items": {"$ref": "#/schemas/BrowseItem"}}),
        "ExportPage": _page_result({"$ref": "#/schemas/ExportResult"}),
        "ToolResult": _tool_result(),
        "ErrorCode": {"type": "string", "enum": list(ERROR_CODES)},
        "ErrorEnvelope": _error_envelope(),
        "MaintenanceState": _maintenance_state(),
        "ImportProgress": _import_progress(),
    }


#: Wire 上真正可调的方法 —— 这一份是 :class:`memgarden.service.GardenService`
#: 的方法表的**唯一事实源**，Service 启动时会拿自己的注册表和它对账，对不上
#: 就直接报错。以前两边各写一份，于是 manifest 声明的能力和实际能调的方法
#: 长期不一致，而测试拿 manifest 和另一个 manifest 比，稳定地锁住了错误答案。
WIRE_OPERATIONS: tuple[str, ...] = (
    "manifest.get", "schema.get", "health.get",
    "capture.run", "capture.begin", "capture.feed", "capture.cancel",
    "context.get",
    "maintenance.check", "maintenance.run", "maintenance.begin",
    "maintenance.feed", "maintenance.cancel",
    "records.browse", "records.export", "records.delete",
    "records.write", "records.promote", "records.migrate",
    "history.import",
    "tool.list", "tool.invoke",
)

#: 一个能力由一组可选的完整 lane 支撑：外层 tuple 是 OR，内层 tuple 是 AND。
#: 例如 Capture 可以有 ``run``，也可以有完整的 ``begin+feed+cancel``；只有
#: ``begin`` 不能算支持，否则宿主开始会话后永远无法完成或取消。
_CAPABILITY_BACKING: dict[str, tuple[tuple[str, ...], ...]] = {
    "capture": (("capture.run",),
                ("capture.begin", "capture.feed", "capture.cancel")),
    "turn_context": (("context.get",),),
    "maintenance": (("maintenance.run",),
                    ("maintenance.begin", "maintenance.feed",
                     "maintenance.cancel")),
    "model_tools": (("tool.list", "tool.invoke"),),
    "tools": (("tool.list", "tool.invoke"),),
    "browse": (("records.browse",),),
    "export": (("records.export",),),
    "delete": (("records.delete",),),
    # 🔴 下面这三项 wire 上**没有**入口。内核的 Python API 做得到，
    # 但陌生 Runtime 调不到 —— 对它而言就是做不到。声明成 True 的后果是
    # 对方照着 manifest 写代码，然后发现没有这个方法。
    "curated_write": (("records.write",),),
    "promote": (("records.promote",),),
    "migrate": (("records.migrate",),),
    "history_import": (("history.import",),),
}

#: 不由方法撑着的纯策略开关。**现在是空的** —— 曾经 history_import 在这里，
#: 因为提示词模板只支持 conversation_capture 一档，即使有 wire 入口也做不成。
#: 那个限制 2026-09-06 解除了（三档的模板全部策略化），所以它回到了
#: 「有方法撑着就是 true」这条统一规则下。
_POLICY_FLAGS: frozenset[str] = frozenset()


def manifest(
    operations: tuple[str, ...] | None = None, *,
    disabled_capabilities: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """这个**已装配的服务**是什么、能做什么、说哪个版本的协议。

    接入方**启动时**就该核对这个 —— 版本不兼容要立刻拒绝启动，而不是跑到
    第一条用户消息才失败。

    ## 🔴 能力声明由「实际可调的方法」推出来，不手写

    以前这里读的是 ``GardenComponent(model=None).capabilities()`` ——
    那是**判断内核**的能力，不是**这个服务**的能力。两者能差很远：

        turn_context   内核说 False，而服务其实提供了 context.get
        curated_write  内核说 True，而 wire 上根本没有对应方法

    接入方读的是这份 manifest，于是照着一份和现实对不上的清单写代码。
    而当时的测试拿 manifest 和同一个 ``GardenComponent`` 比，两边同源，
    **稳定地锁住了这个错误答案**。

    现在：能力名必须有方法撑着（见 ``_CAPABILITY_BACKING``），没有就是 False。
    """
    from .component import GardenComponent
    from dataclasses import asdict

    ops = tuple(operations if operations is not None else WIRE_OPERATIONS)
    available = set(ops)

    caps = asdict(GardenComponent(model=None).capabilities())
    mounts = list(caps.pop("mounts", ("agent-private",)))
    caps.pop("schema_version", None)

    wire_caps: dict[str, Any] = {}
    for name in set(caps) | set(_CAPABILITY_BACKING):
        if name in _POLICY_FLAGS:
            wire_caps[name] = bool(caps.get(name))
            continue
        backing = _CAPABILITY_BACKING.get(name)
        if backing is None:
            # 没登记的能力保守处理：没人说它由哪个方法撑着，就不敢声明支持。
            wire_caps[name] = False
            continue
        # 🔴 判据只有一个：**wire 上有没有能调到的方法**。
        # 不去 and 内核的那个开关 —— 那个说的是 Python API 的能力，
        # 和「陌生 Runtime 调不调得到」是两件事（turn_context 就是这么
        # 被错报成 False 的：内核标 False，而服务其实一直提供 context.get）。
        wire_caps[name] = name not in disabled_capabilities and bool(backing) and any(
            all(method in available for method in lane) for lane in backing)

    return {
        "component_id": "memgarden",
        "component_version": _version(),
        "protocol_version": f"{SCHEMA_VERSION}",
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "mutation_schema_version": MUTATION_SCHEMA_VERSION,
        # 🔴 全链路明文。传输和磁盘层面的安全由部署环境决定，不进这个协议 ——
        # 接入方据此知道「不需要给我密钥，也别指望我替你加密」。
        "plaintext": True,
        # 每一项都对应 wire 上真的调得到的方法,调不到的是 False。
        "capabilities": wire_caps,
        # 只列**权限执行层真正保护**的 mount。
        "mounts": mounts,
        "operations": list(ops),
        "error_codes": list(ERROR_CODES),
        "schemas": sorted(schemas()),
        # 每个 operation 都能定位到自己的 request/response schema。
        "method_schemas": sorted(method_schemas()),
    }


def _version() -> str:
    """装出来的版本号。取不到时给空串,不猜 —— 猜出来的版本号会让接入方
    的兼容性判断建立在假数据上。"""
    try:
        from importlib import metadata

        return metadata.version("memgarden")
    except Exception:  # noqa: BLE001
        return ""


def dump(indent: int = 2) -> str:
    """导出成 JSON 文本 —— CI 里可以把它和签入的副本比对，防止悄悄漂了。"""
    return json.dumps(
        {"manifest": manifest(), "schemas": schemas(),
         "methods": method_schemas()},
        ensure_ascii=False, indent=indent, sort_keys=True,
    )
