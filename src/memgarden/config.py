"""环境变量的读取口 —— 名字归内核，宿主专属的名字不进这里。

## 为什么单独一个文件

这个包是公开发布的。别人装了 `pip install memgarden` 之后，如果要调一个阈值，
得去设 `FEEDLING_DREAM_FUSE_RATIO` —— 而他们**根本不知道 Feedling 是什么**。
产品专属的名字漏进公共包，跟写死中文桶名、写死宿主的枚举是同一类问题。

所以：内核只认 ``MEMGARDEN_*``。宿主要沿用自己的名字，在自己的适配层转换
（读自己的 env，把值当参数传进来），不要指望内核认识它。

## 为什么还读旧名

``memgarden`` 已经公开发布过几个版本，宿主 io 的部署里可能有人临时设过旧名。
直接改名会让「设了但不生效」——**这是最难查的一类问题：没有报错，只是行为
和你预期不一样**。所以旧名继续读，但排在新名之后，并且在文档里标成弃用。

（实际核实过：io 的 deploy / CI / 代码里一处都没设这三个，全在吃默认值。
保留旧名读取纯属稳妥，不是必需。）
"""
from __future__ import annotations

import os

#: 旧的宿主专属前缀 → 弃用，仅为兼容保留。新代码一律用 MEMGARDEN_*。
_LEGACY_PREFIX = "FEEDLING_"
_PREFIX = "MEMGARDEN_"

_FALSEY = frozenset({"0", "false", "off", "no"})


def _raw(name: str) -> str | None:
    """按 ``MEMGARDEN_<name>`` → ``FEEDLING_<name>`` 的顺序取值。"""
    for prefix in (_PREFIX, _LEGACY_PREFIX):
        value = os.environ.get(prefix + name)
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
