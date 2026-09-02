# @teleport-computer/dsh-memgarden

把 Memory Garden 挂到 DeepSeek Harness 上的**薄** Adapter。

> ## ⚠️ 状态：接线对着真实 API 写，但**没有跑过端到端**
>
> 核对基线：`dsh-v0.1.2-alpha.4`，commit `4e84901e6471b79ec0338099867ebb4606d12bb5`
> —— 和 sevenfloor 2026-09-02 文档里 pin 的一致，已从公开仓库拉下来逐个核对。
>
> **已核对属实**（不是照文档猜的）：
>
> | 项 | 真实值 |
> |---|---|
> | 包名 | `@deepseek-ai/dsh-*`（先前猜的 `@deepseek/harness` 不存在） |
> | 注入上下文 | `@deepseek-ai/cordis` 的 `Context` |
> | 工具注册 | `ctx.tools.register(defineTool({...}))` |
> | 每轮召回 | `agent/pre-step`，waterfall，返回 `PreStepDecision` |
> | 轮末落卡 | `agent/turn-stopping`，payload `{ agent, turn, signal }` |
>
> **仍未验证**：
>
> - 从 Agent 取 `profileId` / `userId` / 本轮文本的**具体字段名**还是推测；
> - sevenfloor §8.2 那 16 步端到端，一步都没跑；
> - 失败路径那 12 项（子进程不存在、握手不兼容、hot reload、并发…）都没试。
>
> 所以按他的验收标准，**这不算「兼容完成」**。拿到能跑的 DSH 环境之后：
> 删掉 `types/dsh.d.ts`（那是为了独立类型检查抄的最小子集，会和真类型漂）、
> 把 Adapter 放进他们的 workspace、跑通 §8.2、把结果和确切版本写回这里。
>
> 类型检查：`tsc` 5.6 严格模式 0 错误（注入类型错误会报，确认不是假绿）。

## 形状

```
DeepSeek Harness
      │  pre-step hook / tool registry / scheduler
      ▼
DshMemGardenAdapter（TypeScript，薄）
      │  stdio，一行一个 JSON
      ▼
memgarden serve（Python 长驻）
      ├── MountedGarden
      ├── ModelPort ← 反过来回调 DSH 的 LLM
      └── StoragePort（官方 SQLite）
```

**Adapter 只做翻译和接线**，不复制任何 Garden 的提示词、解析、挑卡、整理逻辑。
判断一律回到 Python 那边——复制过来就会漂，而漂了不报错。

## 三条边界

1. **Python 内核不 import DSH**，DSH 的类型不进 Garden 契约。
2. **DSH 升级只改 Adapter**，不改 Garden 领域模型。
3. **作用域来自 DSH 的可信 scope**，不从模型的工具参数读 —— 否则模型只要在
   参数里写别人的 tenant 就能读到别人的记忆。

## 为什么不只用 MCP

DSH 自带 MCP Client，只注册 `memory_search` / `memory_write` 确实能跑。
但那样只有「模型主动想起来要查」这一种触发方式，做不到：

- 每轮自动带上相关记忆（模型不调工具就没有）
- 对话结束自动落卡
- 后台整理

这三件恰恰是「记忆」这个产品的主体。所以要 Native Adapter + 长驻 Service，
MCP Server 作为通用分发面另算。

## 待确定（需要 DSH 环境才能定）

- 新 Session 是否继续用同一个 agent 的长期花园
- Fork / Resume / Clear Session 各自算不算新的 capture 来源
- 同一 agent 的多个 Session 如何防重复 capture
- 删除 agent 时它的 private 记忆怎么处理
- turn 失败 / 被取消 / 纯工具轮 要不要 capture
- hot reload 时 pending capture 怎么办
