"""模型协议残留处理 —— 聊天与记忆两边**共用**，谁也不依赖谁。

单独成包的原因：`memory_garden` 的卡片文本校验要用它（判断模型把协议头/
思考标记漏进了卡里），而 io 的聊天主链路、工具循环、主动唤醒也要用它。

放进 memory_garden 会让**普通聊天反向依赖记忆包**（codex review 2026-08-14 指出）；
各留一份又会漂 —— 所以独立成一个只依赖标准库的小包。
"""
from . import protocol_leak, self_thinking

__all__ = ["protocol_leak", "self_thinking"]
