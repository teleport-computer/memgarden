"""环境变量的读取口 —— 名字归内核，宿主专属的名字不进这里。

## 为什么单独一个文件

这个包是公开发布的。别人装了 `pip install memgarden` 之后，如果要调一个阈值，
得去设 `FEEDLING_DREAM_FUSE_RATIO` —— 而他们**根本不知道 Feedling 是什么**。
产品专属的名字漏进公共包，跟写死中文桶名、写死宿主的枚举是同一类问题。

所以：内核只认 ``MEMGARDEN_*``。宿主要沿用自己的名字，在自己的适配层转换
（读自己的 env，把值当参数传进来），不要指望内核认识它。

## 旧的 FEEDLING_ 回退已经删掉（2026-08-31）

曾经为了稳妥保留过「MEMGARDEN_ 取不到就退回 FEEDLING_」。删掉的理由：

- 核实过 io 的 deploy / CI / 代码里**一处都没设**这几个名字，全在吃默认值 ——
  留着不解决任何实际问题；
- 而它直接违反公共包的边界要求（公共接口里不许有宿主专属的配置行为）。

宿主真要沿用旧名，在自己那边读 env、把值当参数传进来即可。
"""
from __future__ import annotations

import os

_PREFIX = "MEMGARDEN_"

_FALSEY = frozenset({"0", "false", "off", "no"})


def _raw(name: str) -> str | None:
    """只认 ``MEMGARDEN_<name>``。宿主专属的名字请在宿主那边转换。"""
    value = os.environ.get(_PREFIX + name)
    if value is not None and value.strip() != "":
        return value.strip()
    return None


def flag(name: str, *, default: bool = True) -> bool:
    """开关。**默认给 True 是刻意的** —— 这些开关都是回滚闸（出问题时关掉止血），
    不是功能门（等人来开）。默认关会制造一类新 bug：代码上线了、功能没上线。"""
    value = _raw(name)
    if value is None:
        return default
    return value.lower() not in _FALSEY


def ratio(name: str, *, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """比例。取不到、解析不了、或落在区间外，一律回到默认值 ——
    **配错一个阈值不该让整条路径炸掉**，尤其这些阈值管的是安全闸。"""
    value = _raw(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if lo < parsed <= hi else default


def count(name: str, *, default: int, minimum: int = 1) -> int:
    """正整数。同上，配错回默认。"""
    value = _raw(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default
