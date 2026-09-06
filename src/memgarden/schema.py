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
    # -- 服务层（长驻 serve 的分发边界） ------------------------------- #
    "unknown_method",               # 没有这个方法
    "invalid_request",              # 参数不合法
    "unknown_session",              # capture 会话不存在/已取消/服务重启过
    "model_not_configured",         # 服务没配模型，但这个方法需要模型
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
            "id": _OPT_STR,
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
    return {
        "type": "object",
        "required": ["ok", "result"],
        "properties": {"id": _OPT_STR,
                       "ok": {"type": "boolean", "enum": [True]},
                       "result": result},
        "additionalProperties": True,
    }


def _page() -> dict:
    """分页游标。**浏览和导出必须有界** —— 不分页的 export 在几万张卡的
    花园上会一次性把整座花园塞进一条响应，宿主那边直接 OOM。"""
    return {
        "limit": {"type": "integer", "default": 100, "minimum": 1,
                  "maximum": 1000},
        "cursor": _OPT_STR,
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
            "response": _ok_envelope({
                "type": "object",
                "properties": {"session_id": _OPT_STR,
                               "status": {"type": "string",
                                          "enum": ["needs_model", "completed"]},
                               "next_prompt": _OPT_STR,
                               "result": receipt},
                "additionalProperties": True}),
        },
        "capture.feed": {
            "request": {"type": "object",
                        "required": ["session_id", "reply"],
                        "properties": {"session_id": _STR, "reply": _STR,
                                       "truncated": {"type": "boolean",
                                                     "default": False},
                                       "finish_reason": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope({"type": "object"}),
        },
        "capture.cancel": {
            "request": {"type": "object", "required": ["session_id"],
                        "properties": {"session_id": _STR},
                        "additionalProperties": True},
            "response": _ok_envelope({"type": "object"}),
        },
        "context.get": {
            "request": {"type": "object", "required": ["scope", "query"],
                        "properties": {"scope": _scope_ref(), "query": _STR,
                                       "limit": {"type": "integer",
                                                 "default": 8},
                                       "mount": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope({"type": "object"}),
        },
        "maintenance.check": {
            "request": {"type": "object", "required": ["scope"],
                        "properties": {"scope": _scope_ref()},
                        "additionalProperties": True},
            "response": _ok_envelope({"type": "object"}),
        },
        "maintenance.run": {
            "request": {"type": "object", "required": ["scope", "locale"],
                        "properties": {"scope": _scope_ref(), "locale": _STR,
                                       "mount": _OPT_STR,
                                       "ai_name": _OPT_STR,
                                       "user_name": _OPT_STR},
                        "additionalProperties": True},
            "response": _ok_envelope(receipt),
        },
        "records.browse": {
            "request": {"type": "object", "required": ["scope"],
                        "properties": {"scope": _scope_ref(),
                                       "include_archived": {"type": "boolean",
                                                            "default": False},
                                       **_page()},
                        "additionalProperties": True},
            "response": _ok_envelope({"type": "object"}),
        },
        "records.export": {
            "request": {"type": "object", "required": ["scope"],
                        "properties": {"scope": _scope_ref(),
                                       "include_archived": {"type": "boolean",
                                                            "default": True},
                                       **_page()},
                        "additionalProperties": True},
            "response": _ok_envelope({"type": "object"}),
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
            "response": _ok_envelope({"type": "object"}),
        },
    }


def schemas() -> dict[str, Any]:
    """全部 schema。键名就是契约名。"""
    return {
        "Mutation": _mutation(),
        "Card": _card(),
        "Actor": _actor(),
        "Scope": _scope(),
        "OperationReceipt": _receipt(),
        "ErrorCode": {"type": "string", "enum": list(ERROR_CODES)},
        "ErrorEnvelope": _error_envelope(),
        "MaintenanceState": _maintenance_state(),
    }


#: Wire 上真正可调的方法 —— 这一份是 :class:`memgarden.service.GardenService`
#: 的方法表的**唯一事实源**，Service 启动时会拿自己的注册表和它对账，对不上
#: 就直接报错。以前两边各写一份，于是 manifest 声明的能力和实际能调的方法
#: 长期不一致，而测试拿 manifest 和另一个 manifest 比，稳定地锁住了错误答案。
WIRE_OPERATIONS: tuple[str, ...] = (
    "manifest.get", "schema.get", "health.get",
    "capture.run", "capture.begin", "capture.feed", "capture.cancel",
    "context.get",
    "maintenance.check", "maintenance.run",
    "records.browse", "records.export", "records.delete",
    "tool.list", "tool.invoke",
)

#: 一个能力要能算「Wire 上支持」，必须有方法撑着它。左边是能力名，
#: 右边是实现它的方法 —— 没有方法就是 ``False``，不管 Python 层做不做得到。
_CAPABILITY_BACKING: dict[str, tuple[str, ...]] = {
    "capture": ("capture.run", "capture.begin"),
    "turn_context": ("context.get",),
    "maintenance": ("maintenance.run",),
    "model_tools": ("tool.list", "tool.invoke"),
    "tools": ("tool.list", "tool.invoke"),
    "browse": ("records.browse",),
    "export": ("records.export",),
    "delete": ("records.delete",),
    # 🔴 下面这三项 wire 上**没有**入口。内核的 Python API 做得到，
    # 但陌生 Runtime 调不到 —— 对它而言就是做不到。声明成 True 的后果是
    # 对方照着 manifest 写代码，然后发现没有这个方法。
    "curated_write": (),
    "promote": (),
    "migrate": (),
}

#: 不由方法撑着的纯策略开关 —— 它们表达「这个版本要不要做这件事」，
#: 和有没有 wire 入口无关。
_POLICY_FLAGS = frozenset({"history_import"})


def manifest(operations: tuple[str, ...] | None = None) -> dict[str, Any]:
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
        wire_caps[name] = bool(backing) and any(b in available for b in backing)

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
