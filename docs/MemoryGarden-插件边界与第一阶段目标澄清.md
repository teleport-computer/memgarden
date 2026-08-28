# Memory Garden 插件边界与第一阶段目标澄清

> 日期：2026-08-29  
> 文档性质：对 `MemoryGarden-插件化目标与当前实现差距反馈.md` 及工程回复的范围澄清  
> 面向对象：MemGarden 维护者、Runtime 维护者、外部 Memory Adapter 维护者及其 AI Coding Agent  
> 核心目标：先解释为什么这样判断，再明确第一阶段究竟应该交付什么

## 0. 本轮澄清的最终结论

经过对 MemGarden 当前实现、原插件化 Plan 和工程回复的进一步讨论，我们需要纠正一个逐渐扩大的范围：

> MemGarden 的第一目标，是把完整的 Memory Garden 能力做成一个可以快速插入任意 Agent Runtime 的 SDK；它不是要在第一阶段成为兼容所有第三方记忆系统内部模型的通用 Memory Framework。

因此需要把两个方向彻底拆开：

```text
方向 A：Memory Garden → 任意 Agent Runtime
        这是 MemGarden 自己必须完成的产品目标。

方向 B：任意第三方 Memory → 我们自己的 Runtime
        这是 Runtime 插件接口与第三方 Adapter 的责任。
```

两个方向有关联，但不是同一个交付物，也不能让方向 B 反过来削弱方向 A。

本轮确认以下产品判断：

1. `已确认` MemGarden 第一阶段必须完整保留并交付 Garden 自己的 Capture、Record/Card、Recall、Context、Dream/Maintenance、Tools 和 Storage 能力。
2. `已确认` 不得因为未知的“第二实现”可能没有 bucket、thread、revision 或 dream，就删减或模糊 Garden 已有能力。
3. `已确认` 第二个完整 Memory 实现不是 MemGarden 第一阶段的完成前提，也不是 Garden 数据模型的设计者。
4. `已确认` 其他完整 Memory 系统如果要接入某个 Runtime，应使用该 Runtime 的 Memory Plugin 接口并实现自己的 Adapter/SDK。
5. `已确认` 只有数据库、字段映射或向量搜索能力的外部系统，只能称为 Storage/Retrieval Adapter，不能称为第二套完整 Memory 实现。
6. `已确认` Memory Component 全链路按明文设计；不考虑应用层密文、envelope、enclave、AAD、用户密钥或 shared-memory 密钥分发。
7. `已确认` private/shared 在本项目里只定义逻辑 mount、可见性和读写规则，不定义密码学方案。
8. `已确认` 第一阶段可以定义完整 Garden SDK，同时只给 Runtime 留一个很薄、可演进的 `v0alpha` 接入边界；不需要先冻结“所有 Memory 通用”的 Record Schema。

## 1. 为什么要做这次范围澄清

### 1.1 原评审指出的问题仍然成立

此前评审指出，当前 MemGarden 更接近“独立判断内核”，还不是完整可接入 SDK。这些问题仍然成立：

- 宿主需要深层 import 多个内部模块；
- Capture、模型调用、解析、写入、Recall 和 Dream 仍由宿主手工拼装；
- 缺少顶层 GardenComponent；
- 缺少稳定的 Garden 输入输出结构；
- 缺少 CLI 和 MCP 薄外壳；
- private/shared 逻辑未定义；
- IO 仍有多处直接依赖内部函数；
- StoragePort 仍有 typed mutation、能力执行和升级迁移等问题。

这些缺口要继续补齐。

### 1.2 但此前反馈把“Garden 插件化”和“通用 Memory 平台”放得太近

为了避免公共接口只适用于 Garden，此前反馈提出用第二个真实 Memory 实现验证可替换性。这个验证对 Runtime 的通用插件接口有价值，但如果把它当成 MemGarden 第一阶段的前提，就会出现新的偏差：

```text
我们还不知道第二实现长什么样
    ↓
不敢定义 Garden 的完整接口
    ↓
第一阶段只留下 capture/recall/context 等名称
    ↓
Garden 仍然不能被一个新 Runtime 直接使用
```

这会让“避免过度冻结”变成“不交付可运行接口”。

### 1.3 第二实现不应该决定第一实现能做多少

Memory Garden 的价值正是其有明确判断和工作路径：

- 什么值得记；
- 如何形成较厚的 Card；
- 如何使用 Bucket/Thread；
- 如何控制 Capture 数量和质量；
- 如何召回；
- 如何 Dream、合并和消除矛盾；
- 如何保留 supersede 链条。

如果为了兼容一个可能只有 `add/search/delete` 的系统，把 Garden 也缩成 CRUD，那么最终得到的不是可插拔 Garden，而是一个失去产品特点的最低公分母接口。

正确的抽象原则应该是：

> 通用层只统一 Runtime 真正需要调用的边界，不统一各个 Memory 内部如何思考、如何组织和如何保存。

## 2. 两个方向的责任边界

### 2.1 方向 A：把完整 Garden 插入任意 Runtime

这是 MemGarden 仓库的主要责任。

目标链路是：

```text
任意 Runtime 提供一轮对话/文档/用户主动输入
    ↓
GardenComponent
    ├── Capture 判断
    ├── Garden Record/Card 建模
    ├── StoragePort 写入
    ├── Recall/Selection
    ├── Context 构造
    ├── Dream/Maintenance
    └── Model Tools
    ↓
Runtime 获得可直接使用的结果
```

新的 Runtime 不应需要理解 15 个 MemGarden 子模块，也不应自己复制 Garden 的编排流程。

### 2.2 方向 B：把任意 Memory 插入我们的 Runtime

这是 Runtime 或独立协议包的责任。

合理结构是：

```text
Runtime MemoryPlugin Port
    ├── GardenRuntimeAdapter → MemGarden SDK
    ├── OmbreRuntimeAdapter  → Ombre 自己的 SDK/API
    ├── OtherRuntimeAdapter  → 其他系统自己的 SDK/API
    └── MCPMemoryAdapter     → 普通 MCP Memory Server
```

第三方系统可以完全保留自己的：

- 数据模型；
- Capture 机制；
- Embedding/Graph；
- Recall；
- Consolidation；
- 自有存储；
- Tools。

它只需要通过 Runtime 接口交付该 Runtime 真正需要的结果。

### 2.3 为什么这两个接口不能合并成一个巨大协议

如果强行共用一个完整协议，会出现两种失败：

1. 把 Garden 的 bucket/thread/supersede 强迫给所有外部系统；
2. 为了兼容最简单的外部系统，把 Garden 的 Dream、Card 和关系模型全部降级掉。

因此应保留两层：

| 层 | 所有者 | 是否 Garden-specific |
|---|---|---:|
| Garden SDK Contract | MemGarden | 是，应完整表达 Garden 能力 |
| Runtime MemoryPlugin Port | Runtime/独立协议 | 否，只保留很薄的运行时接入语义 |

## 3. “第二实现”到底是什么

### 3.1 不属于第二完整实现的情况

以下都不是第二套完整 Memory Component：

- `InMemoryStore`；
- `SqliteStore`；
- PostgreSQL Store；
- Notion FieldMap；
- 只负责向量检索的数据库；
- 只替换 Garden Card 保存结构的 Adapter。

它们仍然运行：

```text
Garden Capture → Garden Record → 外部保存/检索 → Garden Recall/Dream
```

所以只能称为：

- Storage Adapter；
- Field Adapter；
- Retrieval Adapter。

### 3.2 什么才是第二个完整实现

一套完整外部 Memory 至少独立拥有其中大部分能力：

- 接收对话或资料；
- 自己判断如何写入；
- 自己维护记忆身份和生命周期；
- 自己召回；
- 自己产生给 Agent 的 Context 或 Tools；
- 可能拥有自己的 maintenance/consolidation；
- 可能自带存储。

它的内部链路可能完全不是 Garden：

```text
输入 → embedding → vector/graph update → similarity retrieval → context
```

这没有问题，因为它应接入 Runtime MemoryPlugin Port，而不是接入 Garden StoragePort。

### 3.3 第二实现未来能做到什么程度

第二实现接入 Runtime 后，能做到的程度由自己的 capability 决定：

| 能力 | 可能情况 |
|---|---|
| 接收每轮输入 | 支持或不支持 |
| 自动 Capture | 支持或不支持 |
| 每轮自动 Context | 支持或不支持 |
| Agent Tools | 支持或不支持 |
| 后台 Maintenance | 支持或不支持 |
| Browse/List | 支持或不支持 |
| private/shared | 支持或不支持 |
| 自带 Storage | 支持或不支持 |

“可接入”不等于“与 Garden 功能完全相同”。Runtime 只根据 capability 使用它确实提供的能力。

## 4. 为什么不需要为第二实现削弱 Garden

### 4.1 可替换不等于功能完全一致

第二实现没有 Dream，不代表 Garden 也不能有 Dream。它只需要声明：

```json
{
  "turn_context": true,
  "model_tools": true,
  "background_maintenance": false
}
```

Runtime 可以继续运行，只是用户获得的是不同能力和体验。

### 4.2 通用的是 Runtime 结果，不是内部过程

Runtime 真正需要的通常只有：

- 本轮是否产生 Context；
- Context 内容是什么；
- 有哪些 Tool；
- Tool 调用结果；
- 本轮输入是否被接受；
- 后台任务是否完成；
- 当前组件支持哪些能力。

Runtime 不需要知道对方内部叫 Dream、Consolidation、Graph Rewrite 还是没有维护过程。

### 4.3 Garden 专属结构可以公开且稳定

Garden SDK 可以正式定义：

- `GardenRecord`；
- `GardenCaptureRequest/Result`；
- `GardenMutation`；
- `GardenContextRequest/Result`；
- `GardenMaintenanceRequest/Result`；
- `GardenStoragePort`。

其中 bucket、thread、supersede 等字段可以作为 Garden 的正式领域语义，不需要全部藏进一个假装通用的 `extensions`。

只有跨 Runtime 的最薄 Adapter 层，才不应强迫其他实现理解这些字段。

## 5. 明文是已经确定的边界

### 5.1 公共链路一律按明文设计

`已确认` 以下内容全部是明文语义：

- Garden 输入；
- Garden Record/Card；
- Capture 输入输出；
- Recall 输入输出；
- Context；
- StoragePort；
- component-managed storage 的传输内容；
- private/shared mount 中的记忆内容。

MemGarden 不负责、也不定义：

- ciphertext；
- envelope；
- enclave；
- AAD；
- 用户密钥；
- 密钥分发；
- 解密后排序；
- shared-memory re-encryption。

### 5.2 基础设施加密不改变协议

HTTPS/TLS、磁盘加密、云数据库静态加密可以透明存在，但 Memory SDK 看到的仍然是明文。这些不是 Memory Component Contract 的字段或工作路径。

### 5.3 旧 IO 加密实现不得限制公共 SDK

如果旧 IO 在迁移期间仍然存在 envelope/enclave，它只能留在旧 IO Adapter：

```text
旧 IO 数据
    ↓
旧 Adapter 转成明文 Garden 输入
    ↓
GardenComponent
```

不能把旧 IO 的加密结构带入 MemGarden 公共接口，也不能以密钥分发为理由阻塞 logical mount。

## 6. private/shared 的本轮定义

本项目只定义逻辑数据归属和访问范围：

| Mount | 含义 |
|---|---|
| `agent-private` | 仅当前 Agent 范围内使用 |
| `user-private` | 用户授权的 Agent 可使用 |
| `family-shared` / `workspace-shared` | 授权成员或 Agent 共同使用 |

最低语义：

1. 每条记录属于一个明确 mount；
2. 默认写入 private；
3. 写入 shared 必须显式指定；
4. Runtime 注入已认证的 actor 和可访问 mounts；
5. MemGarden 不自行授予权限；
6. private 提升到 shared 是显式操作；
7. 成员退出后由 Runtime 撤销访问；
8. 不讨论重新加密或密钥轮换。

第一阶段如果只实现 `agent-private`，应在 capability 中明确声明，而不是等待加密设计。

## 7. 推荐的三层工程结构

### 7.1 第一层：Garden Kernel

继续保留现有纯函数和判断模块：

- prompt builders/parsers；
- policies；
- scoring/selection；
- dreaming；
- text guards；
- observability；
- evals。

这一层便于单元测试和高级用户直接调用。

### 7.2 第二层：GardenComponent 高层 SDK

这是第一阶段的核心新增交付。

它负责把现有零件编排成完整链路，但通过注入 Port 使用外部能力：

```python
garden = GardenComponent(
    store=my_store,
    model=my_model,
    config=GardenConfig(...),
)
```

建议最小接口：

```python
class GardenComponent:
    def capabilities(self) -> GardenCapabilities: ...
    def capture(self, request: GardenCaptureRequest) -> GardenCaptureResult: ...
    def build_context(self, request: GardenContextRequest) -> GardenContextResult: ...
    def tools(self) -> list[ToolDefinition]: ...
    def invoke_tool(self, request: ToolCall) -> ToolResult: ...
    def run_maintenance(
        self, request: GardenMaintenanceRequest
    ) -> GardenMaintenanceResult: ...
```

模型 API key、厂商 SDK 和真正调用方式仍由注入的 ModelPort 负责。GardenComponent 负责完整编排，而不是让每个 Runtime 重写编排。

### 7.3 第三层：Runtime Adapter

GardenRuntimeAdapter 把 Runtime 的薄接口映射到 GardenComponent：

```python
class GardenRuntimeAdapter(RuntimeMemoryPlugin):
    ...
```

这层可以位于 Runtime 仓库或独立协议包，不应要求 MemGarden 兼容所有第三方内部记录。

## 8. 第一阶段应该冻结什么、不冻结什么

### 8.1 应该完整定义和版本化

这些属于 Garden 自己，可以在第一阶段清楚定义：

- Garden Record/Card；
- Garden Capture 请求和结果；
- Garden typed mutations；
- Garden StoragePort；
- Garden Recall/Context；
- Garden Maintenance/Dream；
- Garden Tools；
- Garden capability manifest；
- Garden error/receipt；
- logical mounts；
- CLI/MCP 对 GardenComponent 的映射。

### 8.2 不需要在第一阶段冻结

这些属于“所有 Memory 的通用世界标准”，当前没有必要：

- 所有系统都必须使用的 Memory Record；
- 所有系统都必须支持的 revision；
- 所有系统都必须支持的 supersede；
- 所有系统都必须拥有的 bucket/thread；
- 所有系统共用的 mutation lifecycle；
- 不同 Memory 内部数据迁移格式。

### 8.3 Runtime 薄接口可以先使用 `v0alpha`

为了避免 IO 继续依赖 Garden 内部模块，Runtime Adapter 仍需要一个可运行的最小接口，而不能只有五个 slot 名称。

`v0alpha` 至少应表达：

- request ID；
- actor/mount；
- 输入类型；
- capability；
- accepted/completed/failed；
- context blocks；
- tool definitions/results；
- provider-owned opaque references；
- standard error/receipt。

它不承诺统一外部 Memory 的内部 Record，也可以在第二阶段验证后再稳定成 `v1`。

## 9. StoragePort 应该只解决 Garden 的保存问题

### 9.1 Garden Storage Adapter

如果 PostgreSQL、SQLite、Notion 或向量数据库保存的是 Garden 产生的记录，它实现 `GardenStoragePort`。

Garden 仍然负责：

- Capture；
- Record；
- Recall 规则；
- Dream；
- Tools。

### 9.2 完整外部 Memory 不实现 GardenStoragePort

如果外部系统自带 Capture、Recall、Consolidation 和 Storage，它不应被降级成 Garden 数据库。

它应当实现 Runtime MemoryPlugin Adapter，并整套拥有自己的 mount。

### 9.3 Storage capability 分层

建议 GardenStoragePort 的契约测试分为：

```text
必需能力
  稳定身份、写入、读取、按 ID 取、幂等、冲突、原子 mutation

可选能力
  metadata sort、filter、full-text、vector search、自定义字段
```

不支持可选能力的 Store 必须显式声明，并由 Garden 在可证明正确时降级。必需能力不满足时拒绝对应操作。

## 10. UI 不应决定 Garden 或通用 Memory 的内部数据模型

当前 IO UI 使用 bucket、summary、threads 和 occurred_at，这是 Garden UI 的合理需求，但不应变成所有外部 Memory 的内部记录要求。

建议区分：

### Garden 原生展示

可以完整使用：

- bucket；
- summary；
- threads；
- occurred_at；
- supersede/lifecycle。

### Runtime 通用展示投影

外部 Memory 如果希望进入通用 Browse UI，只需要提供：

| 字段 | 建议 |
|---|---|
| `record_ref` | 必需 |
| `display_text` / `summary` | 必需 |
| `mount` | 必需 |
| `occurred_at` / `updated_at` | 建议 |
| `provider` | 建议 |
| `group_label` | 可选 |
| `tags` | 可选 |

没有 `group_label` 时 UI 应平铺或显示“未分类”，不能空白。完全不支持 Browse 的组件则声明该 capability 缺失，Runtime 显示薄 fallback。

这属于 Runtime 展示协议，不需要反过来删掉 Garden bucket/thread。

## 11. 第一阶段的完整交付范围

### 11.1 Garden 数据与行为契约

- Garden Record/Card 的正式字段和版本；
- bucket/thread/lifecycle/supersede 的明确语义；
- typed mutation；
- idempotency 和 revision/CAS；
- 导入、Capture、用户主动写入的不同模式；
- delete/archive/supersede 的区别；
- 明文 StoragePort；
- 数据导出和用户删除语义。

### 11.2 完整 GardenComponent

- 一次调用完成 Capture 编排；
- 一次调用完成 Recall + Context；
- 一次调用完成 Maintenance/Dream 编排；
- ModelPort 和 StoragePort 注入；
- 不要求宿主深层 import 内部模块；
- 前台与后台返回结构化 receipt。

### 11.3 Agent 接入面

- Python SDK；
- versioned JSON；
- CLI；
- MCP shell；
- model tools；
- quickstart；
- “十分钟接入陌生 Runtime”的完整示例。

### 11.4 Runtime 集成

- GardenRuntimeAdapter；
- IO V1/V2 逐步迁移到单一挂载点；
- 每次迁移保证行为不变；
- 迁移结束后，IO 中 MemGarden 的生产入口收敛到一个地方；
- 旧深层接口可以暂时保留兼容，但不得继续成为新接入方式。

### 11.5 Mount 与权限上下文

- logical mount schema；
- Runtime 注入 actor/allowed mounts；
- 默认 private 写入；
- shared 显式写入；
- 无应用层加密字段。

## 12. 第二阶段的正确任务

第二阶段可以选择一套完整外部 Memory，用于验证 Runtime MemoryPlugin Port，而不是用于裁剪 Garden。

验证问题包括：

- Runtime 是否可以不改业务流程完成挂载；
- provider 是否可以使用自己的内部存储；
- provider 是否可以只提供 tools 或 context；
- capability 缺失时 Runtime 是否正确降级；
- 不同 provider 是否能拥有不同 mounts；
- Runtime UI 是否能用通用展示投影或 fallback；
- 切换 provider 时是否明确不自动迁移旧数据。

Ombre Brain 可以是候选，但目前不需要指定为第一阶段依赖，也不需要照它的数据结构设计 Garden。

## 13. 关于多组件和“混合”的最终规则

我们不推荐两个 provider 共同修改同一套记录，但仍需允许不同组件拥有不同能力或 mounts。

正确规则是：

> 同一个 mount 的同一个 slot 只有一个执行者；不同 mount 可以由不同组件拥有。

例如：

```text
agent-private mount
  capture/recall/context/maintenance → Garden

external-mcp mount
  tools → External MCP
  数据由 External MCP 自己管理
```

宿主可以聚合 Context Blocks 或 Tool Definitions，但不要求双方共享 record ID、revision、lifecycle 或 mutation。

禁止：

- Garden 和外部组件同时 Capture 同一输入并写入同一 mount；
- 两套 Maintenance 同时修改同一记录集合；
- 把外部 provider 的记录伪装成 Garden Card 后让双方共同维护。

## 14. 对工程回复关键问题的明确答复

### 对 Q3：第一阶段冻结到哪一层

结论：

- 完整冻结并版本化 Garden SDK 自己的契约；
- Runtime 层提供可运行的 `v0alpha` 薄接口；
- 不冻结“所有 Memory 通用 Record Schema”；
- 不能只定义五个 slot 名称而没有请求响应。

### 对 Q4：storage ownership

结论：

- GardenStoragePort 解决 Garden 的 host-injected storage；
- MemGarden 可以自带 SQLite 作为参考/本地默认实现；
- 完整外部组件的 component-managed storage 由它自己的 Runtime Adapter 隐藏；
- 不要求完整外部组件实现 GardenStoragePort。

### 对 Q5：bucket/thread

结论：它们是 Garden 的正式领域能力，可以留在 Garden Record，不是所有 Memory 的公共必需字段。

### 对 Q7：private/shared 与加密

结论：不考虑应用层密文。现在定义 logical mount 和权限上下文，不等待密钥方案。

### 对 Q9：Storage conformance

结论：拆成必需/可选是正确方向；真实 Adapter 仍应跑必需契约，并验证可选能力声明和降级，不应因为能力不同而完全退出 conformance tests。

### 对 Q10：第二实现是否已定

结论：未指定为 MemGarden 第一阶段依赖。Ombre Brain 等只是第二阶段 Runtime Adapter 验证候选。

### 对 Q12：是否允许混合

结论：禁止同 mount 同 slot 双执行；允许不同组件拥有不同 mount 或独立 slot。不要求不同组件共享内部记录模型。

## 15. 第一阶段验收标准

### 15.1 陌生 Runtime 能接入完整 Garden

可运行示例应证明：

```text
安装 MemGarden
→ 注入 ModelPort 和 StoragePort
→ 提交一轮明文对话
→ Capture 产生 Garden Record
→ 存储成功
→ 新一轮查询召回
→ 生成 Context
→ 运行一次 Maintenance
→ 注册并调用 Memory Tool
```

接入方不需要复制 IO 代码，也不需要了解 MemGarden 内部文件布局。

### 15.2 Garden 功能不得因通用化而缺失

- Capture policy 可用；
- bucket/thread 可用；
- supersede 可用；
- Dream/Maintenance 可用；
- Selection policy 可用；
- CLI/MCP 使用同一 GardenComponent；
- JSON/SDK 能表达完整 Garden 结果。

### 15.3 可靠性

- 相同请求重放不重复写；
- 同幂等键不同内容报冲突；
- stale revision 被拒绝；
- 原子 mutation 不留下半完成状态；
- SQLite 旧版本升级有 migration test；
- ID 不因删除或升级发生覆盖；
- delete/archive/supersede 不混淆；
- private mount 不被未授权 actor 查询。

### 15.4 边界纯净

- 公共接口无 IO/V1/V2 名称；
- 无 FEEDLING 专属配置行为；
- 无 ciphertext/envelope/enclave/AAD 字段；
- 模型厂商和 API key 由 ModelPort 提供；
- Runtime 调度和队列不进入 Garden Kernel；
- 第三方 Memory 的内部结构不进入 Garden Record。

## 16. 不属于第一阶段的工作

- 设计适用于所有 Memory 系统的正式 `v1` Record Schema；
- 接入 Ombre Brain；
- 为每个外部 Memory 编写 Adapter；
- 保证所有 Memory 与 Garden 功能等价；
- 自动迁移 Garden 与其他组件的数据；
- 让两个 provider 共同维护同一 mount；
- 应用层加解密；
- shared-memory 密钥分发；
- 通用 Agent Runtime 框架。

## 17. 建议的工程执行顺序

### P0：完成当前修复的发布闭环

1. 为 SQLite schema 变更补升级 migration；
2. 旧 `applied` 表增加 digest 的迁移策略；
3. 新 id counter 从旧数据正确初始化，或改用 ULID/UUID；
4. 增加从 `v0.2.0` 数据库升级的测试；
5. 发布包含修复的新版本；
6. IO 从 `v0.2.0` 升级并验证。

### P1：定义完整 Garden 领域契约

1. Garden Record；
2. typed mutations；
3. Capture/Context/Maintenance 请求响应；
4. Garden capabilities；
5. logical mounts；
6. ModelPort/StoragePort。

### P2：实现 GardenComponent

1. 收口现有模块；
2. 完整串通 Capture → Store → Recall → Context；
3. 串通 Maintenance；
4. 串通 Tools；
5. 保留低层纯函数供高级用户使用。

### P3：开发者接入面

1. Python SDK；
2. JSON Schema；
3. CLI；
4. MCP；
5. quickstart；
6. Garden conformance kit。

### P4：IO 渐进迁移

1. 新增单一 GardenRuntimeAdapter；
2. 一次迁移一个调用点；
3. 输出行为对比；
4. 所有生产路径最终收敛到一个挂载点。

### P5：第二阶段 Runtime 验证

1. 选择真实外部 Memory；
2. 由它或 Runtime 侧编写 Adapter；
3. 验证 Runtime 的薄接口；
4. 根据真实冲突再稳定 Runtime MemoryPlugin `v1`。

## 18. 最终判断

我们不是放弃可替换性，而是重新把可替换性放回正确层级：

```text
MemGarden 层
  完整、明确、有产品特点的 Garden SDK
  可以插入任何 Runtime
  可以更换 Garden 的 Storage Adapter

Runtime 层
  很薄的 MemoryPlugin Port
  Garden 和其他完整 Memory 各自实现 Adapter
  不要求内部模型一致
```

这样设计有三个直接收益：

1. Garden 不会为了未知系统自砍能力；
2. 任意 Agent 仍然能快速接入完整 Garden；
3. 将来接入其他 Memory 时，只增加 Runtime Adapter，不需要修改 Garden 内核，也不需要让两套 Capture/Dream 重复执行。

最终第一阶段的判断标准应该是：

> 一个从未接触 IO 的 Agent Runtime，能否仅通过公开 SDK、明文协议和少量 Ports，完整使用 Memory Garden 的 Capture、Storage、Recall、Context、Dream 和 Tools。

如果能做到这一点，MemGarden 的插件化第一阶段就是完整的。第二个 Memory 实现可以在下一阶段验证 Runtime 的通用接入能力，但不能成为推迟或削弱 Garden SDK 的理由。
