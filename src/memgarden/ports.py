"""宿主要提供的能力 —— GardenComponent 自己不做这些事。

## 为什么是「注入」而不是「配置」

内核不调模型、不碰存储、不持有任何凭据。这不是洁癖，是三条实际的约束：

    模型      key 在宿主手里。谁付钱、走哪个 provider、失败怎么退避，
              都是宿主的运维决定，不该由一个判断库替它定
    存储      宿主的库可能加密、可能分片、可能在别人的云上。
              内核只说「该这么改」，怎么落是宿主的事
    调度      定时器、队列、重试是宿主的基础设施。内核只负责「该整理了吗」

所以这里定的是**接口**，实现由宿主传进来。用 Protocol 而不是基类：
宿主已有的对象只要方法对得上就能直接用，不必为了接入而继承什么。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ModelPort(Protocol):
    """「帮我问一次模型」。

    只要一个方法：给一段提示词，返回模型的原始回复文本。

    **故意不定义 temperature / max_tokens / 超时 / 重试**：那些是宿主的运维策略，
    而且各家 provider 的参数名都不一样。内核只关心「我给你一段话，你还我一段话」。

    需要按用途分模型（capture 用便宜的、dream 用强的）时，宿主自己在实现里分流 ——
    内核会把用途通过 ``purpose`` 告诉你。
    """

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        """``purpose`` 是这次调用的用途（``"capture"`` / ``"dream"`` / ``"migrate"``），
        供宿主分流模型、打点、限流。内核不依赖它的返回值有任何结构。"""
        ...


@runtime_checkable
class ClockPort(Protocol):
    """「现在几点」。

    看起来多余，但**内核里不许直接调 ``datetime.now()``**：那样一来所有跟时间
    有关的判断（这张卡多久没动了、该不该整理了）在测试里就没法固定，
    只能靠 sleep 或者 monkeypatch 全局函数。
    """

    def now_iso(self) -> str:
        """当前时刻，ISO 8601 字符串。"""
        ...


class SystemClock:
    """默认时钟。宿主不传就用它。"""

    def now_iso(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()


__all__ = ["ModelPort", "ClockPort", "SystemClock"]
