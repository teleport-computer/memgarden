# agent-protocol-core

**识别「模型协议残片漏进了正文」。纯标准库，零依赖。**

模型的原始输出里带着协议层的东西 —— 通道标记、工具路由、思考标签、
被截断的 JSON 尾巴。这些**不该出现在给人看的文本里**：用户会在聊天窗口
或记忆卡里看到一串乱码，而系统那边一切正常，没有任何报错。

这个包只做识别，不做处置 —— 怎么处置（打回重问、就地清洗、直接丢弃）
是调用方的决定，不同场景代价不同。

```python
from agent_protocol_core import protocol_leak, self_thinking

protocol_leak.is_orphan_json_tail(text)   # 闭括号比开括号多，且带 JSON 记号
self_thinking.strip_thinking(text)        # 思考标记
```

## 为什么单独一个包

聊天和记忆两条路都要这个判断。放在任何一边，另一边就得跨模块引用；
各写一份则会漂 —— 改了一边忘了另一边，同一段脏文本在两条路上待遇不同。

跟 [memgarden](https://github.com/teleport-computer/memgarden) 同仓库、锁步发版。

Apache-2.0
