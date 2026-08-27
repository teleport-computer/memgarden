# Memory Garden 插件化目标、当前实现差距与后续交付要求

> 日期：2026-08-27  
> 文档性质：产品目标对齐与工程反馈  
> 面向对象：MemGarden 维护者、IO Runtime 维护者、外部 Memory Adapter 维护者及其 AI Coding Agent  
> 依据：`io-memory-component-plan.md`、MemGarden `v0.2.0` 代码与 IO 实际接入路径的联合评审

## 0. 先给结论

本次评审认为，MemGarden 已经较好地完成了第一层工作：

> 把 Memory Garden 中“什么值得记、如何写卡、如何召回、什么时候整理”等判断逻辑，从原 IO 宿主中抽成独立 Python 包，并由 IO 通过外部依赖复用。

但它还没有完成产品真正需要的第二层工作：

> 把完整记忆能力整理成一套与具体 Agent Runtime、具体 Memory 实现、具体数据库无关的可插拔组件协议，使任意 Agent 只需按标准提供输入、存储和运行接口，就能快速挂载、替换或组合记忆能力。

因此，当前版本更准确的定位是：

```text
Memory Garden judgment kernel
Memory Garden 判断内核
```

而最终目标应当是：

```text
Portable Memory Component SDK
可移植、可声明能力、可按插槽替换的记忆组件 SDK
```

这不是否定当前抽离工作的价值，而是明确“独立包”和“真正插件”之间仍然存在一层关键的产品协议与编排层。

## 1. 本文的结论等级

为避免工程实现把讨论建议误当成已经冻结的公共协议，本文使用以下标记：

| 标记 | 含义 | 工程处理方式 |
|---|---|---|
| `已确认` | 产品目标已经明确 | 设计与实现需要满足；如存在不可行之处，应明确提出冲突 |
| `工程要求` | 为实现可插拔、可测试和可靠接入必须具备的语义 | 具体类名、数据库可以调整，但不能丢失保证 |
| `建议方案` | 当前推荐的接口形态 | 工程可以提出更简单方案，但需要覆盖相同用例 |
| `第二阶段` | 不要求当前立即实现 | 第一阶段仍需保证公共契约可以容纳，不能先写死成 Garden 专用接口 |

## 2. 最终产品目标

### 2.1 与 PerceptKit 相同的插件化原则

`已确认` Memory Component 与 Perception Component 的最终形态应遵循同一套原则：

1. 不绑定 IO V1、V2 或任何具体 Runtime；
2. 不绑定 PostgreSQL、SQLite、向量数据库或某个 SaaS；
3. 使用稳定、版本化、可校验的数据结构；
4. 能力被拆成独立插槽，组件明确声明自己支持哪些插槽；
5. 宿主只通过公共接口接入，不深层 import 某个实现的内部模块；
6. 某个组件缺少部分能力时，可以明确降级，而不是让宿主猜测；
7. 同一个插槽在一次挂载中只有一个实际负责人，避免两套内核重复执行；
8. 更换组件默认不等于自动迁移数据；迁移是单独的显式任务；
9. SDK 核心保持小而清晰，具体模型、身份、加密、调度和权限继续由宿主负责。

### 2.2 理想接入体验

`已确认` 一个新的 Agent Runtime 接入记忆能力时，原则上只需要：

1. 按标准结构提供对话、文档、查询或用户主动写入；
2. 选择组件自带存储，或实现宿主自己的 Storage Adapter；
3. 把组件提供的每轮 Context、模型 Tools 和后台任务接到 Runtime。

理想使用方式应接近：

```python
memory = runtime.mount_memory(
    component=GardenComponent(),
    store=MyMemoryStore(),
    model=MyModelAdapter(),
    mounts=("agent-private", "family-shared"),
)
```

随后宿主只调用标准方法：

```python
memory.capture(input)
memory.build_context(query)
memory.list_tools()
memory.invoke_tool(name, arguments)
memory.run_background("consolidate")
```

更换另一套完整 Memory 系统时，宿主业务流程原则上不变：

```python
component=OtherMemoryComponent()
```

### 2.3 JSON 与 SDK 的关系

`已确认` 最终交付物不应被理解为“只能是 Python SDK”或者“只要一个 JSON 文件”。推荐形态是：

```text
JSON Schema / Manifest
    负责稳定数据协议、版本和能力声明

Memory Component SDK / Adapter
    负责真正执行 capture、recall、context、maintenance 和 tools
```

JSON 文件可以描述一个组件支持什么、入口在哪里、输入输出采用哪个版本，但无法独立完成语义判断、召回或 Dream。对于 MCP 或远程 API 组件，SDK Adapter 可以很薄，只负责把标准接口映射到远端工具。

## 3. 必须区分的两种“可替换”

这是当前实现和产品预期之间最重要的概念差异。

### 3.1 替换存储后端

当前 `InMemoryStore`、`SqliteStore`、`StoragePort` 和 `FieldMap` 主要证明的是：

> Garden 继续负责 Capture、Card 建模、Recall 和 Dream，只把最终数据换一种方式保存。

工作流仍然是：

```text
Garden Capture
    ↓
Garden Card / Bucket / Thread
    ↓
Garden Mutation
    ↓
可替换的存储后端
    ↓
Garden Selection
    ↓
Garden Dream
```

这是“Garden 可换数据库”，不是“宿主可换 Memory 组件”。

### 3.2 替换完整 Memory 组件

另一套真实 Memory 系统可能自带：

- 判断什么值得记；
- 自己的记忆对象和生命周期；
- 相似度、图结构或其他召回机制；
- 合并、遗忘、消除矛盾或 consolidation；
- 自己的存储；
- 提供给 Agent 的工具；
- 自己的后台维护任务。

真正的组件替换意味着：

```text
标准 Runtime 输入
    ↓
Other Memory Component
    ├── Capture
    ├── Store
    ├── Recall
    ├── Context
    ├── Maintenance
    └── Tools
    ↓
标准 Runtime 输出
```

此时不能让 Garden 和外部组件同时判断一次“该记什么”或同时整理一次记忆，否则会产生重复和冲突。

### 3.3 当前实现只能完成哪一种

| 替换层级 | 当前支持情况 | 说明 |
|---|---:|---|
| InMemory 换 SQLite | 支持 | 同一个 Garden 内核的两种保存方式 |
| SQLite 换宿主 PostgreSQL | 接口原型存在 | 真实 IO Storage Adapter 尚未由本仓库验证 |
| 不同字段映射成 Garden Card | 支持部分 | 仍要求对方服从 Garden 的记录语义 |
| 替换 Recall 排序策略 | 内核支持 | IO 顶层生产路径仍需确认全部流量都经过该插槽 |
| 替换 Capture 内核 | 不支持标准替换 | 没有顶层 capture capability 接口 |
| 替换 Dream/Maintenance 内核 | 不支持标准替换 | 没有通用 maintenance capability 接口 |
| 接入完整外部 Memory | 不支持 | 没有 MemoryComponent 顶层协议 |
| 接入普通 MCP Memory Server | 不支持直接挂载 | 没有 MCP shell 和 slot mapping |

## 4. 当前实现已经完成的部分

本次评审确认以下工作具有实际价值，应保留并继续作为 Garden 实现的内部能力：

### 4.1 独立发布与宿主复用

- 已经是独立 GitHub 仓库和 Apache-2.0 包；
- 已有 `v0.2.0` Release；
- IO 使用外部 Release wheel，而不是继续复制一份源码；
- V1/V2 的 Capture/Dream 路径共用外部 MemGarden 判断逻辑；
- 核心模块大部分保持标准库、确定性和无网络调用。

### 4.2 Garden 判断内核

- Capture prompt 和解析；
- 不同 Capture policy；
- Card 写作、清洗和泄漏防护；
- Scoring、selection 和 trace；
- Dream prompt、是否需要 Dream 的判断和快照逻辑；
- 观测记录与 evals；
- 字段映射；
- StoragePort 原型；
- InMemory/SQLite 参考实现。

### 4.3 边界方向基本正确

以下内容留在宿主是合理的：

- 真正调用模型；
- 身份、租户和权限；
- 加密、解密和 enclave；
- 获取聊天记录；
- 定时器、队列和重试；
- 审计和产品级通知；
- 运行时最终怎样使用 Context。

问题不在于这些内容没有搬入 MemGarden，而在于 MemGarden 尚未定义宿主应该通过什么通用端口把它们提供进来。

## 5. 当前实现缺少的核心层

### 5.1 没有顶层 MemoryComponent Contract

`工程要求` 宿主不应需要知道以下内部模块：

- `prompts.capture`；
- `prompts.dream`；
- `scoring`；
- `selection`；
- `dreaming`；
- Garden Card 的内部 helper。

当前 quickstart 和 IO 需要分别深层 import 多个模块，再手工编排输入、模型调用、解析、存储和召回。这证明现在是一个工具函数库，而不是一个组件。

需要一个稳定顶层接口统一表达：

- 组件是谁；
- 它支持哪些能力；
- 哪些能力需要宿主提供模型或存储；
- 如何 Capture；
- 如何构造每轮 Context；
- 提供哪些 Tools；
- 后台能运行哪些任务；
- 缺少能力时如何返回。

### 5.2 没有版本化的标准输入输出

当前不同函数直接接收 `window`、`buckets`、`threads`、`identity`、`cards` 和自由字典。外部 Agent 无法仅通过公共协议回答：

- 一轮对话如何标识和幂等；
- 一次历史导入如何表达；
- 用户主动新增的记忆如何表达；
- 查询发生在哪个 Agent、用户和 mount；
- Capture 返回的是候选、最终记录还是 mutation；
- Recall 返回 ID、摘要、正文还是已经渲染好的 Context；
- 部分失败、模型失败和存储冲突如何表达；
- schema 如何演进。

`工程要求` 需要稳定 DTO 和 JSON Schema，而不是让每个宿主自己拼字典。

### 5.3 当前 Capabilities 只是存储能力

现有 `Capabilities` 声明：

- `supports_supersede`；
- `supports_atomic_batch`；
- `supports_custom_fields`；
- `supports_metadata_sort`。

这些回答的是“这个存储后端能否承载 Garden mutation”，没有回答完整 Memory 组件支持什么。

完整组件至少需要声明：

- `model_tools`；
- `turn_context`；
- `background_capture`；
- `background_maintenance`；
- `history_import`；
- `curated_write`；
- `private_memory`；
- `shared_memory`；
- `host_managed_storage`；
- `component_managed_storage`；
- `export`；
- `delete`。

### 5.4 Capture、Recall、Dream 没有成为独立插槽

目前这些是 Garden 包里的独立模块，但“代码分成多个文件”不等于“能力可以替换”。

`工程要求` 每项能力需要具有：

- 标准请求；
- 标准结果；
- 能力声明；
- 明确负责人；
- 缺失时的降级语义；
- 对应的 conformance tests。

### 5.5 CLI 和 MCP 外壳缺失

原 Plan 明确要求一个严肃 Python Kernel 和两个薄外壳：CLI 与 MCP。

当前：

- `pyproject.toml` 没有正式 `[project.scripts]`；
- `evals/` 下的 argparse 不是产品 CLI；
- 没有 MCP Server；
- 没有 MCP tools 与 component slots 的映射。

CLI/MCP 不应复制 Garden 逻辑，只应调用统一 `MemoryComponent` 接口。

### 5.6 private/shared Memory 没有模型

当前主要使用自由字符串 `tenant`，无法完整表达：

- 某个 Agent 的私有记忆；
- 用户跨 Agent 的私有记忆；
- 家庭/团队共享记忆；
- 读取顺序；
- 默认写入目标；
- 跨 mount 权限；
- 将 private 内容提升到 shared 的显式操作。

`已确认` private/shared 应是两个独立 mount，不应通过给 tenant 字符串加前缀来假装完成。

### 5.7 没有组件选择、切换与 fallback

当前不存在统一的：

- component registry；
- component config；
- capability negotiation；
- disabled/degraded 状态；
- switch-without-migration 行为；
- 不可用时的薄 fallback 展示。

`已确认` 更换组件默认不迁移数据。宿主需要明确告诉用户当前挂载的是哪套 Memory，以及旧数据是否仍可从原组件访问。

## 6. 防止两套记忆内核重复工作的所有权规则

### 6.1 每个插槽只能有一个执行负责人

`已确认` 在一次 Memory mount 中，同一个能力插槽只能由一个 provider 实际执行。

建议最小插槽如下：

| 插槽 | 责任 |
|---|---|
| `capture` | 从对话/文档判断什么值得形成记忆 |
| `store` | 保存、读取、修改和删除记忆 |
| `recall` | 根据当前查询选择相关记忆 |
| `context` | 将召回结果转换成 Agent 每轮上下文 |
| `maintenance` | 合并、归档、消除矛盾、Dream/Consolidation |
| `tools` | 给模型主动调用的查、写、修改工具 |
| `import_export` | 历史导入和用户数据导出 |

Garden 全栈模式可能是：

```json
{
  "capture": "memgarden",
  "store": "host-postgres",
  "recall": "memgarden",
  "context": "memgarden",
  "maintenance": "memgarden",
  "tools": "memgarden"
}
```

完整外部组件模式可能是：

```json
{
  "capture": "other-memory",
  "store": "other-memory",
  "recall": "other-memory",
  "context": "other-memory",
  "maintenance": "other-memory",
  "tools": "other-memory"
}
```

### 6.2 不允许默认双重执行

以下工作流必须禁止：

```text
Garden Capture
    ↓
External Capture 再判断一次
    ↓
两边分别写入
```

也必须禁止：

```text
Garden Dream
    +
External Consolidation
    同时修改同一批记忆
```

否则会产生重复记录、相互覆盖、不同生命周期状态和不可追溯的更新。

### 6.3 混合模式必须显式且经过兼容性验证

允许未来出现：

```json
{
  "capture": "memgarden",
  "store": "external-store",
  "recall": "external-retriever",
  "maintenance": "memgarden"
}
```

但只有在各方明确支持共同的：

- record identity；
- revision；
- lifecycle；
- mutation semantics；
- metadata round-trip；
- delete/export semantics；

时才能组合。默认策略应是整套组件接入，不能把任意外部 Memory 当成数据库后就声称完成适配。

## 7. 建议的公共协议

本节是推荐形态，具体命名可以讨论，但覆盖的语义不能缺失。

### 7.1 Component Manifest

```json
{
  "schema_version": 1,
  "component_id": "memgarden",
  "component_version": "0.3.0",
  "protocol_version": "memory-component-v1",
  "capabilities": {
    "model_tools": true,
    "turn_context": true,
    "background_capture": true,
    "background_maintenance": true,
    "history_import": true,
    "curated_write": true,
    "private_memory": true,
    "shared_memory": true,
    "export": true
  },
  "storage": {
    "modes": ["host_managed"]
  }
}
```

### 7.2 Memory Input Envelope

```json
{
  "schema_version": 1,
  "input_id": "turn_01J...",
  "input_type": "conversation_turn",
  "occurred_at": "2026-08-27T14:00:00+08:00",
  "actor": {
    "agent_id": "agent_123",
    "user_id": "user_456"
  },
  "mount": "agent-private",
  "content": {
    "messages": []
  }
}
```

`工程要求` `user_id`、权限和可写 mount 必须由宿主认证上下文注入，不能只信任模型或客户端自报。

### 7.3 Canonical Memory Record

公共协议不应强迫所有组件使用 Garden 的 bucket/thread，但需要最小通用结构：

```json
{
  "record_id": "mem_01J...",
  "mount": "agent-private",
  "kind": "fact",
  "summary": "用户不吃辣",
  "content": "用户吃辣后会胃疼，因此通常避免辣食。",
  "source": {
    "input_ids": ["turn_01J..."]
  },
  "created_at": "2026-08-27T14:00:05+08:00",
  "updated_at": "2026-08-27T14:00:05+08:00",
  "revision": "1",
  "lifecycle": "active",
  "extensions": {
    "memgarden": {
      "bucket": "preferences",
      "threads": []
    }
  }
}
```

Garden 专属字段应进入命名空间扩展，不能成为所有 Memory 组件必须理解的公共名词。

### 7.4 Typed Mutation

```json
{
  "operation_id": "op_01J...",
  "type": "add",
  "record": {},
  "expected_revision": null,
  "idempotency_key": "capture:turn_01J...:0"
}
```

需要至少定义：

- `add`；
- `update`；
- `archive`；
- `supersede`；
- `delete`；
- `no_op`。

并标准化：

- Revision conflict；
- Idempotency conflict；
- Unsupported operation；
- Permission denied；
- Partial/unknown outcome；
- Retryable/unretryable failure。

### 7.5 Context Result

```json
{
  "schema_version": 1,
  "record_ids": ["mem_01J..."],
  "context_blocks": [
    {
      "type": "memory",
      "text": "...",
      "mount": "agent-private"
    }
  ],
  "trace": {
    "policy": "memgarden-default",
    "candidate_count": 20,
    "selected_count": 4
  }
}
```

第三方 selection 不应通过返回伪造完整记录来绕过宿主所有权检查。推荐先返回稳定 ID，再由拥有数据权限的一侧回填内容。

### 7.6 Background Result

```json
{
  "job_type": "maintenance",
  "status": "completed",
  "mutations": [],
  "next_cursor": null,
  "metrics": {
    "examined": 10,
    "changed": 2
  }
}
```

后台能力负责判断与产出，不负责宿主的定时器、队列、重试和模型凭证。

## 8. 两种存储模式必须同时被协议容纳

### 8.1 Host-managed Storage

宿主拥有 canonical record，组件返回标准 mutation，宿主通过 StoragePort 落库。

适用于：

- Garden + IO PostgreSQL；
- Garden + 用户自己的数据库；
- 本地 Agent + SQLite。

### 8.2 Component-managed Storage

外部组件自己保存数据，宿主只持有远端 record ID、操作 receipt 和必要投影。

适用于：

- SaaS Memory；
- MCP Memory Server；
- 自带数据库和召回能力的完整 Memory 服务。

`工程要求` 第一阶段可以只实现 Garden 的 host-managed 模式，但 Manifest 和顶层接口不能假设所有组件都必须暴露 Garden StoragePort。

## 9. private/shared Memory 的最低要求

`已确认` 至少需要区分：

| Mount | 典型含义 |
|---|---|
| `agent-private` | 仅当前 Agent 可读写 |
| `user-private` | 用户授权的多个 Agent 可读取 |
| `family-shared` / `workspace-shared` | 家庭、团队或空间共享记忆 |

建议默认规则：

1. 先读当前 Agent private；
2. 再读用户授权的 shared mounts；
3. 默认写入 private；
4. 写入 shared 必须显式指定并经过宿主权限检查；
5. private 提升到 shared 是显式 mutation；
6. 组件不自行决定跨 mount 权限；
7. 查询 trace 需要标明结果来自哪个 mount，但不得泄露未授权 mount 的存在。

## 10. 当前 StoragePort 和参考 Store 的具体问题

这些问题不一定阻塞契约讨论，但在把它作为公共 SDK 示例之前需要修复。

### 10.1 Mutation 没有类型

当前是 `list[dict]`，调用方和实现方只能靠字符串约定操作类型，无法可靠地：

- 推导所需 capability；
- 做 schema validation；
- 区分普通归档和用户删除；
- 生成稳定 digest；
- 标准化错误。

代码自身已经注明该接口尚未定完，因此不应把它当成稳定公共协议发布。

### 10.2 Capability enforcement 依赖调用方手工执行

`ensure_supported` 需要每个调用方主动记得调用。正确做法是统一 executor 根据 typed mutation 自动校验，避免漏检。

### 10.3 SQLite ID 会在删除后发生冲突覆盖

当前 SQLite 使用记录总数生成下一 ID：

```text
COUNT(*) → m_{n+1}
```

实测：

```text
原有：m_1=first, m_2=second
删除：m_1
新增：third
生成：m_2
结果：原 m_2 被 third 覆盖
```

原因是 `_put` 同时使用 upsert。ID 应使用 UUID/ULID、数据库 sequence，或至少使用不会因删除回退的单调计数器。

### 10.4 幂等键不验证请求摘要

同一个 idempotency key：

1. 第一次请求写入 A；
2. 第二次请求使用相同 key 写入完全不同的 B；
3. 当前实现直接返回第一次结果，不报冲突。

需要保存 mutation digest，并在同 key 不同 digest 时返回 `IdempotencyConflict`。

### 10.5 “不硬删”与通用 delete 操作冲突

README 和测试强调 supersede 不应硬删除，但两个参考 Store 都接受普通 `delete` mutation。

建议区分：

- editorial archive/supersede；
- 用户发起的数据删除；
- 合规删除；
- 管理员修复。

用户删除可以存在，但必须是显式、带权限和审计语义的操作，不能藏在普通非类型化 mutation 中。

### 10.6 实际宿主 Storage Adapter 尚未形成同一套验证

当前共享契约测试覆盖 InMemory 和 SQLite，但 IO 自己的真实 Postgres/enclave 路径没有作为本仓库的第三个 adapter 跑同一套 conformance tests。

这意味着参考实现通过，并不等于生产存储已经符合该接口。

## 11. 宿主中立性仍需清理

当前包虽然不直接 import IO 模块，但仍存在行为级宿主痕迹，例如：

- `FEEDLING_DREAM_FUSE_RATIO`；
- `FEEDLING_DREAM_FUSE_MIN_CARDS`；
- `FEEDLING_MEMORY_CARD_GUARD`；
- 同源 `agent-protocol-core` 中的 IO/V1/V2/Feedling 运行时语义。

`工程要求` 公共 Kernel 应使用实现中立配置对象或 `MEMGARDEN_*` 命名空间。IO 专属配置应在 IO Adapter 中转换后传入。

当前 purity/no-host-leakage 测试主要检查 import 和少量文本模式，没有阻止产品专属环境变量和运行时语义进入公共包，因此守卫范围需要调整。

## 12. 第一阶段和第二阶段的正确划分

### 12.1 第一阶段必须完成

`已确认` 第一阶段不要求立即接入 Ombre Brain，但至少需要交付：

1. 实现中立的 Memory Component Contract；
2. Component Manifest 和 capability slots；
3. 版本化输入输出 JSON Schema；
4. GardenComponent 对公共接口的实现；
5. host-managed/component-managed 两种模式的协议表达；
6. private/shared mount 的公共语义；
7. 顶层自动 Context、Tools、Capture、Maintenance 接口；
8. CLI 和 MCP 薄外壳的接口设计，至少 CLI 可运行；
9. Mock/Fake Component conformance tests，验证宿主不会深层依赖 Garden；
10. 明确声明“第二真实实现尚未验证”。

### 12.2 第二阶段再完成

`第二阶段`：

- 接入 Ombre Brain 或另一套真实完整 Memory；
- 接入普通 MCP Memory Server；
- 验证完整替换时宿主业务流程不变；
- 验证组件只提供部分能力时的降级；
- 验证 component-managed storage；
- 验证两个 Agent 分别使用 private/shared mounts；
- 验证切换组件但不迁移数据。

因此，“尚未接入第二个真实实现”不应被描述成第一阶段实现失败；更准确的状态是：

> 第二真实实现验证属于第二阶段，目前尚未进行，因此当前可替换性只能称为设计目标，不能称为已经验证的事实。

但是，如果第一阶段只交付 Garden StoragePort，没有完整 MemoryComponent slots，那么第二阶段不是“增加一个 adapter”，而是需要重新设计公共 API。这才是当前需要立即修正的风险。

## 13. 建议的工程交付顺序

### P0：先冻结公共语义

1. 确认 MemoryComponent 的最小 slots；
2. 确认每个 slot 的请求、结果和错误；
3. 确认两种 storage ownership；
4. 确认 private/shared mount；
5. 确认同一 slot 单一 provider 的所有权规则。

### P1：把现有 Garden 包进组件接口

1. 新建 GardenComponent facade；
2. 将 capture、recall、context、maintenance 收口到 facade；
3. 保留现有内部模块，但不再让宿主依赖内部路径；
4. IO V1/V2 改为只依赖同一个顶层 adapter；
5. 自动 Context 的全部生产流量真正通过顶层 Selection/Context slot。

### P2：补齐开发者接入面

1. Manifest；
2. JSON Schema；
3. CLI；
4. MCP shell；
5. quickstart：新 Runtime 十分钟内接入；
6. component conformance test kit。

### P3：第二真实实现验证

选择一套真实 Memory 实现，验证完整切换、部分能力、内部存储和 MCP 场景。

## 14. 第一阶段验收标准

第一阶段完成时，以下场景应当能够由自动化测试或 runnable example 证明：

### 14.1 接入体验

- 一个陌生 Python Agent 不 import MemGarden 内部模块即可挂载 GardenComponent；
- 宿主只需实现文档列出的最小 Ports；
- 示例能够完成输入 → capture → store → recall → context；
- 示例能注册并调用 Memory tools；
- 示例能运行一次后台 maintenance，但调度仍由宿主触发。

### 14.2 能力降级

- 组件没有 maintenance 时，前台 capture/recall 仍正常；
- 组件只有 tools 时，不会被误当成支持自动 Context；
- 组件使用 component-managed storage 时，不要求实现 Garden StoragePort；
- 不支持 shared memory 时，Manifest 明确声明，宿主不会静默写错 mount。

### 14.3 所有权与可靠性

- 同一个 slot 不会同时调用两个 provider；
- 同一输入重放不会重复写入；
- 相同幂等键、不同请求会报冲突；
- stale revision 会被拒绝；
- 批量 mutation 失败不会留下半完成状态；
- private 数据不会被 shared 查询返回；
- selection 只能返回已授权 record ID；
- 组件切换不会自动修改或迁移原组件数据。

### 14.4 公共 API 稳定性

- 输入输出都有 schema version；
- Garden 专属 bucket/thread 不进入必需公共字段；
- IO/V1/V2 名称不进入公共协议；
- README 安装方式和真实发布方式一致；
- CLI、Python SDK 与 MCP shell 使用同一套 Kernel/Contract，不复制判断逻辑。

## 15. 非目标与边界

本次插件化不要求 MemGarden 负责：

- 用户认证；
- OAuth 或凭证管理；
- 数据加密和 enclave；
- 从聊天系统主动拉取消息；
- 自己启动定时器；
- 自己选择队列；
- 自己持有模型 API key；
- 决定 Agent 最终回复什么；
- 自动迁移两个组件之间的数据；
- 实现通用 Agent Runtime。

但它必须通过协议说明：宿主需要提供什么，以及组件会返回什么。

## 16. 需要工程师明确回应的问题

请工程侧逐项回应，避免只提交零散代码而没有完成架构对齐：

1. 是否同意最终交付物是 Memory Component SDK，而不只是 Garden 判断函数包？
2. 是否同意“更换存储”和“更换完整 Memory 组件”是两种不同能力？
3. 第一阶段准备提供哪些 component slots？
4. 是否会同时支持 host-managed 和 component-managed storage 的协议表达？
5. Garden 专属 bucket/thread/supersede 如何避免成为所有组件的强制公共语义？
6. IO 当前所有 Capture、Context、Tools、Dream 路径何时统一经过顶层 Adapter？
7. private/shared mount 的身份、读取顺序和写入目标如何表达？
8. CLI 和 MCP shell 的交付顺序是什么？
9. 当前 StoragePort 的 typed mutation、错误、幂等摘要和 SQLite ID 问题如何修复？
10. 第二阶段准备选择哪一个真实 Memory 系统做完整替换验证？
11. 在第二真实实现完成前，README 是否会避免宣称已经实现完整可替换性？
12. 如何保证未来接入自带 Capture/Dream 的系统时，不会与 Garden 重复执行？

## 17. 最终产品判断

当前 MemGarden 已经完成了有价值的内核抽离，也在 IO 中产生了真实复用价值。但它目前证明的是：

> Garden 的判断代码可以独立发布，Garden 的数据可以尝试换一种方式保存。

它尚未证明的是：

> 任意 Agent 可以通过统一协议快速挂载记忆；任意完整 Memory 系统可以替换 Garden；不同能力可以按插槽组合；缺少某项能力时宿主仍能正确运行。

后续工作的核心不是继续增加更多 Garden prompt、更多 Card 字段或更多内部 helper，而是补齐：

```text
Manifest
    +
版本化 JSON Contract
    +
MemoryComponent Facade
    +
能力插槽与唯一负责人
    +
Storage Ownership
    +
Private / Shared Mount
    +
CLI / MCP Shell
    +
Component Conformance Tests
```

完成这些以后，MemGarden 才能从“IO 抽出来的记忆判断内核”，变成真正可以交给任意 Agent Runtime 使用的外接记忆插件。
