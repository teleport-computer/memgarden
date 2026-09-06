"""极小的 JSON Schema 子集校验器。

## 为什么自己写

这个包**零依赖**。为了在执行前校验入参就去装 jsonschema，等于让每个接入方
都吃下一个传递依赖。而我们要校验的东西很窄：type / required / properties /
enum / minimum / maximum，够用了。

## 为什么必须在执行前校验

不校验的话，坏请求会一路走到业务代码里才炸，而那时的错误消息是
``'NoneType' object has no attribute 'strip'`` —— 调用方看到这句，
既不知道是哪个字段，也不知道是自己传错了还是服务有 bug。

提前校验给出的是「``scope.memory_owner_id`` 缺失」。这两种消息的差别，
就是对方半小时和两天的差别。
"""
from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


class SchemaViolation(ValueError):
    """入参不符合 schema。``path`` 指到具体字段。"""

    def __init__(self, path: str, why: str) -> None:
        self.path, self.why = path, why
        super().__init__(f"{path or '<root>'}: {why}")


def validate(value: Any, schema: dict, *, path: str = "",
             schemas: dict | None = None) -> None:
    """按 schema 校验 value；不合法就抛 :class:`SchemaViolation`。"""
    if not isinstance(schema, dict) or not schema:
        return

    ref = schema.get("$ref")
    if ref:
        # 只支持 "#/schemas/<Name>" 这一种形状 —— 我们只用到这一种。
        name = str(ref).rsplit("/", 1)[-1]
        target = (schemas or {}).get(name)
        if isinstance(target, dict):
            validate(value, target, path=path, schemas=schemas)
        return

    expected = schema.get("type")
    if expected:
        allowed = _TYPES.get(str(expected))
        # bool 是 int 的子类 —— 不排掉的话 True 会被当成合法的 integer，
        # 而那多半是调用方传错了字段。
        if allowed and (not isinstance(value, allowed)
                        or (expected in ("integer", "number")
                            and isinstance(value, bool))):
            raise SchemaViolation(path, f"应当是 {expected}，"
                                        f"实际是 {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaViolation(path, f"只能是 {schema['enum']!r} 之一")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low:
            raise SchemaViolation(path, f"不能小于 {low}")
        if high is not None and value > high:
            raise SchemaViolation(path, f"不能大于 {high}")

    if isinstance(value, dict):
        for name in schema.get("required") or ():
            if name not in value:
                raise SchemaViolation(_join(path, name), "必填字段缺失")
        props = schema.get("properties") or {}
        for name, sub in props.items():
            if name in value:
                validate(value[name], sub, path=_join(path, name),
                         schemas=schemas)
        # 未知字段一律放行 —— 新版本加字段时旧调用方不该崩。

    if isinstance(value, (list, tuple)):
        item = schema.get("items")
        if isinstance(item, dict):
            for i, entry in enumerate(value):
                validate(entry, item, path=f"{path}[{i}]", schemas=schemas)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name
