# 接入与数据参考

本文描述当前源码的接入和存储行为，配合 [README](../README.md) 使用。完成度与验证基线只在 [STATUS](STATUS.md) 维护；本文不重复阶段性审查记录。

## 1. Runtime 需要接的路径

| 时机 | SDK / wire 入口 | 宿主处理结果 |
|---|---|---|
| 对话前召回 | `context_for_turn` / `context.get` | 把 `blocks` 注入本轮上下文，保留 `record_ids` 供追溯 |
| 对话后记忆 | `capture_and_store` / `capture.run` | 检查业务回执，再标记这段素材已处理 |
| 宿主自行调模型 | `capture.begin/feed/cancel` | 按 `needs_model` 调模型并 feed，直到 `completed`，再检查其中回执 |
| 检查、执行整理 | `check_maintenance`、`run_and_store_maintenance` / `maintenance.check/run` | 调度归宿主，卡片与整理账本由 Garden 一起提交 |
| 宿主驱动整理 | `maintenance.begin/feed/cancel` | 与 Capture 使用相同的模型往返方式 |
| 用户明确保存 | `write_one` / `records.write` | 不再套自动 Capture 的价值筛选 |
| 历史材料导入 | `import_history` / `history.import` | 持久保存原材料和 `ImportProgress`，失败后以相同语义续传 |
| 读取与导出 | `browse`、`export` / `records.browse/export` | 持续读取 `next_cursor`，直到为空 |
| 用户删除 | `delete_record` / `records.delete` | 提供请求身份并检查删除回执 |
| 改变可见范围 | `promote` / `records.promote` | 宿主先授权，不能把模型给的 `authorized` 当作权限证据 |
| 升级旧卡字段 | `migrate_and_store` / `records.migrate` | 给出旧卡和允许修改的 ID；这不是数据库 schema 迁移 |
| 给模型的工具 | `tools`、`invoke_tool` / `tool.list/invoke` | 绑定可信 Scope 后再执行 |

SDK 完整参数见 [contracts.py](../src/memgarden/contracts.py) 和 [mounted.py](../src/memgarden/mounted.py)。wire 字段以 [`schema.py`](../src/memgarden/schema.py) 为准，可通过 `schema.get` 获取；不要假定 Python 请求类的每一个字段都由每条 wire 方法透传。

直接构造 SDK 时需注入挑卡策略；未配置 `selection_policy` 的判断组件不会自动返回召回结果。服务壳提供默认策略。

### 模型调用与能力声明

Python 模型接口是 `complete(prompt, *, purpose="") -> str`。凭据、超时、取消和模型选择由宿主实现。需要使用 Runtime 自己的模型调度时，走 begin/feed 路径。

`capture.run`、`maintenance.run`、`history.import`、`records.migrate` 需要服务侧配置模型。前两项另有 begin/feed 路径；后两项当前没有。因此默认 DSH 无模型服务的 `history_import`、`migrate` 为 false，这是明确的接入边界。

独立 `memgarden manifest` 是静态声明；连接后的 `manifest.get` 才按实际模型和 Store 给出能力。`manifest.storage.capabilities`、`degradations`、`user_notices` 用于识别缺失条件，不是外部 Store 已经通过测试的证明。

JSON Lines 每行一个请求与响应，stdout 承载协议。示例请求：

```json
{"id":"browse-1","method":"records.browse","params":{"scope":{"tenant_id":"example","memory_owner_id":"user-42","allowed_mounts":["agent-private"]},"limit":100}}
```

请求 ID 可为字符串、整数或 null。先检查响应顶层 `error`，再检查业务结果中的 `error` / 工具的 `ok`；`completed` 只代表会话已结束，不能替代存储成功判断。

## 2. 归属与权限

| 字段 | 含义 | 当前约束 |
|---|---|---|
| `Scope.tenant_id` | 账户、组织或部署边界 | 由宿主认证上下文提供 |
| `Scope.memory_owner_id` | 一座长期花园的稳定所有者 | 必填；不能使用每次变化的 session ID |
| `Scope.actor` | 此次操作者的 user / agent / session 身份 | 用于来源记录，不决定数据归属 |
| `Scope.allowed_mounts` | 本次调用获准使用的范围 | 空列表按默认 `agent-private` 处理，不解释为全部允许 |
| `mount` | 卡片的逻辑可见范围 | 原生名称有 `agent-private`、`user-private`、`family-shared`、`workspace-shared` |

Store 按 `(tenant, owner)` 查询隔离；MountedGarden 再检查 mount 和目标卡。原始 Store 不替调用方认证；直接调用 Store 的宿主必须自己承担权限检查。

mount 是分区里的范围标签，不会自动建立跨 owner 家庭/工作区成员关系。共享参与者映射到哪个稳定 owner、谁能获得哪些 mount，仍由 Runtime 决定。不能仅把标签改成 `family-shared` 就声称已实现完整家庭共享。

## 3. 卡片字段与 metadata

metadata 指描述一条记忆的辅助属性，例如来源、分类、时间、重要度；不是额外采集信号，也不要求单建一张 metadata 表。

| 字段 | 含义 / 类型 | 保存与使用 |
|---|---|---|
| `summary` | 摘要，字符串 | 列表、召回展示；也是用户内容，不应默认写入普通日志 |
| `content` | 正文，字符串 | 完整记忆内容；不会因当前上下文预算而裁短已存正文 |
| `bucket` | 分类，字符串 | Garden 分类与整理；由 locale 和素材决定 |
| `threads` | 线索，字符串数组 | 关联记忆与检索 |
| `type` | Capture 类型字符串 | 当前解析器产出 `event` / `fact` / `quote` / `moment`；平铺卡可包含，`Card` 类型未单独声明 |
| `importance` / `pulse` | 重要度 / 情绪激活度，数值 | 供判断或策略使用；不是额外原始材料 |
| `occurred_at` | 事情发生时间，字符串 | 历史导入/人工档案按 `keep_dates=True` 保留；对话档 `keep_dates=False` 不传递；空值不推定日期 |
| `role` / `is_sensitive` | 记忆角色 / 敏感标识 | 供策略与宿主展示判断；敏感标识不替代访问权限 |
| `source` | 来源，开放字符串 | 内置工作流写入 `conversation_capture`、`history_import`、`curated`、`model_tool` |
| `source=memory_dream` | 整理产物，保留值 | 排除在下一轮原始卡新增水位之外 |
| `source_material_kind` | 导入材料类型 | 例如 `diary` / `chat_export`，由可信调用参数提供 |
| `source_actor` | 操作者对象 | MountedGarden 新增/取代时用可信 Scope 覆盖；普通 update 不得更改来源三个字段 |

字段定义见 [Card](../src/memgarden/records.py)。空的可选字段可省略。Capture 解析器保留模型提供的合法 `role` 字符串和 `is_sensitive` 布尔值；不保证模型每次都产出这些可选字段，也不把它们当作授权证据。日期按策略保留，日期字符串只有日期时不补时间，带时区时间转换为 UTC；未带时区的日期时间沿用现有解析约定按 UTC 解释，调用方应提供明确时区以免产生歧义。非法日期或元数据类型触发现有格式重试；字符串 `"false"` 不会被当作布尔值使用。字段映射工具 [FieldMap](../src/memgarden/adapt.py) 可以把外部字段转成候选卡；它不是完整的外部记忆系统适配器。

`summary` 和 `search_text` 用途不同：后者可以包含用于匹配的更多文本，不能因此直接作为公开摘要。挑卡过程的内容无关指标由 [observability.py](../src/memgarden/observability.py) 生成；查询指纹仍可用于关联、也可能被猜测，不应宣称绝对无法还原或等同匿名化。

### 定义里的 Record 与实际存储形状

`records.Record` 是声明的嵌套结构：`record_id` + `card` + `mount/lifecycle/revision/created_at/updated_at/superseded_by/schema_version`。

当前两个参考 Store 保存的是平铺字典，`records.export` 返回这类原始记录；`records.browse` 返回简化的 BrowseItem 投影。它们不是自动序列化后的 `Record`，不能按嵌套形状直接建表。实际新卡示意如下（ID 与内容均虚构）：

```json
{
  "id": "m_1",
  "type": "event",
  "summary": "饮食清淡",
  "content": "对方不吃辣，一吃就胃疼。",
  "bucket": "饮食",
  "threads": ["饮食习惯"],
  "importance": 0.0,
  "pulse": 0.0,
  "mount": "agent-private",
  "source": "conversation_capture",
  "created_at": "2026-09-08T00:00:00Z",
  "updated_at": "2026-09-08T00:00:00Z",
  "source_actor": {"user_id":"u-42","agent_id":"a-1","session_id":"s-1"}
}
```

归档时平铺记录增加 `archived: true`、`archive_reason`；取代时旧卡增加 `archived: true`、`superseded_by`。新卡可能没有显式 `lifecycle`、`schema_version` 或逐卡 `revision`。

### 三种时间及写入规则

| 字段 | 含义与写入责任 |
|---|---|
| `occurred_at` | 事情发生时间，来自素材，按 Capture 策略保留；缺失时不推测。不能用导入时间替代。 |
| `created_at` | 卡片创建时间，新卡由 Store 自动补齐；后续普通修改不能改写。 |
| `updated_at` | 卡片最后修改时间，新卡由 Store 初始化，实际持久化变化时更新。 |

两个参考 Store 共用以下规则，第三方 StoragePort 应保持相同语义：

- `add` 与 `supersede` 产生的新卡补齐缺失的创建／更新时间；一次批次使用同一个 UTC 时间，保留时钟提供的亚秒精度。可信迁移／恢复在新卡中显式提供的历史时间仍保留，不强行改成今天；普通模型 Capture 不提供这两个字段。
- `update`、`archive`、`promote` 及被 `supersede` 的旧卡，只要内容或状态实际变化，就更新 `updated_at`；保留原有 `created_at`。普通 `update.changes` 不允许直接覆盖这两个字段。
- 读取、召回、无变化 update、重复相同归档／提升、`no_op` 和幂等重放不刷新时间。自动时间不进入调用方的 mutation 指纹，重试不会因此变成幂等冲突；失败批次不会留下新的时间值。
- 既有旧卡缺失 `created_at` 时，读取、重新打开数据库和修改都不补造过去；真正修改时只记录新的 `updated_at`。旧卡上的空值仍表示未知。
- `InMemoryStore(clock=...)` 与 `SqliteStore(path, clock=...)` 可注入已有 `ClockPort`，不传时使用 `SystemClock`。这是 Store 的写入时钟，不是依赖模型生成，也不要求宿主在每次 Capture 手工填写。

时间字段存进原有平铺 JSON，不新增表，也不修改 SQLite schema 版本。`maintenance_state.updated_at` 只属于整理账本，不能替代每张卡的时间。时间值也不代替并发控制：并发版本仍由 owner 级快照和回执提供。

## 4. SQLite 实际有哪些表

以下是参考实现，不要求外部数据库照抄表名。当前 SQLite schema 版本为 **3**，存于 `PRAGMA user_version`；协议与记录的 schema 版本仍为 **1**，不要混用。

| 表 | 主键 | 其他字段 | 作用 |
|---|---|---|---|
| `cards` | `tenant, owner, id` | `doc TEXT` | 明文 JSON 卡，含当前有效及归档/被取代记录 |
| `revisions` | `tenant, owner` | `revision INTEGER` | owner 级并发版本 |
| `applied` | `tenant, owner, key` | `result TEXT, digest TEXT` | 幂等结果及内容指纹，非原始对话仓库 |
| `id_counters` | `tenant, owner` | `next_id INTEGER` | 生成 ID 的只增计数，删除不回退 |
| `maintenance_state` | `tenant, owner, mount` | `signature, seed_card_count, revision, updated_at, schema_version` | 最近已提交的整理进度 |
| `seed_generations` | `tenant, owner, mount` | `generation INTEGER` | 原始卡新增水位，不因删除下降，不把整理产物重新计入 |

表定义、升级逻辑见 [sqlite.py](../src/memgarden/stores/sqlite.py)。维护账本与对应卡片 mutation 在同一事务提交；失败要一起回滚。v2→v3 初始化水位时考虑现有卡和旧账本，避免删除历史造成水位倒退。

没有独立的原始对话表、历史导入任务表、全量审计表、逐字段版本表或向量表。`InMemoryStore` 提供对应行为但只存进程内，重启即丢失，不适合作为持久库。

### 卡片之外还有哪些数据

| 数据 | 当前保存者 | 保留 / 恢复边界 |
|---|---|---|
| 原始对话与导入材料 | Runtime | Garden 抽取卡片不等于替宿主备份素材 |
| `ImportProgress` | Runtime | 包返回进度；宿主必须持久保存后才能跨进程续传 |
| DSH 待落卡素材 | `stateDir` 下 JSONL outbox | 含对话窗口；成功后移除，失败保留重试，不是纯 metadata |
| Capture/Maintenance 会话 | Service 进程内 | 空闲默认 15 分钟过期，两个 lane 合计最多 1024 个；重启需重新 begin |
| 提示词、模型回复、日志、备份 | Runtime / 运维 | 由宿主决定是否保存，不能放入公开仓库 |

原生卡片和辅助表没有自动到期清理策略；这表示当前实现不自动淘汰，不等于备份或永久可恢复保证。幂等回执会持续增长，不能随意清理后仍宣称原有重放窗口有效。

## 5. 七种写操作与删除边界

| 操作 | 当前语义 |
|---|---|
| `add` | 新增；已存在 ID 被拒绝 |
| `update` | 就地订正，不能改身份、归属、来源或生命周期；不保存逐字段旧值 |
| `supersede` | 新卡取代一张或多张旧卡；旧卡保留并指向新卡 |
| `archive` | 保留内容，退出通常的召回 |
| `delete` | 从当前 Store 删除指定卡的记录和正文；相同删除重放可取原回执 |
| `promote` | 宿主授权后改变 mount，独立于普通 update |
| `no_op` | 不改卡片，表达已经处理；整理无改动时仍可原子推进账本 |

用户要求删除不得改成归档。当前 `delete` 是指定记录的应用层真删，不提供 SQLite 页/磁盘物理擦除、备份清理、原素材删除、outbox 取消或所有派生卡的级联删除。宿主若要求“忘掉某件事及其全部副本”，必须协调这些存储路径，防止旧素材再次 Capture。

幂等回执可能保留 ID、状态和摘要指纹，不保留一份已删除正文供普通查询。删除目标卡后，其他卡仍可能有 `superseded_by` 指向该 ID；不能把这个引用解释为内容仍然存在。

## 6. 自定义存储必须满足什么

实现 [StoragePort](../src/memgarden/storage.py)：`capabilities()`、`load(tenant, *, owner, **filters)`、`apply(tenant, mutations, *, owner, idempotency_key, expected_revision, maintenance_state=None)`、`maintenance_state(tenant, *, owner, mount)`。

| 能力声明 | 缺少时的处理 |
|---|---|
| `supports_owner_scoping` | 拒绝相关存储读写，不能先读取其他 owner 再过滤 |
| `supports_atomic_batch` | 拒绝需要原子性的工作流/批次 |
| `supports_supersede` | 拒绝需要保留取代链的工作流 |
| `supports_hard_delete` | 关闭删除，不能降级为归档 |
| `supports_maintenance_state` | 关闭整理；账本须与卡片原子提交 |
| `supports_monotonic_seed_generation` | 关闭整理；删除不能使新增水位倒退 |
| `supports_custom_fields` | 由外部 Adapter 映射字段，报告信息损失/检索代价 |
| `supports_metadata_sort` | 可由上层本地排序，但须报告读量与性能代价 |

后加的两个 Maintenance 能力默认 false，外部 Store 须主动实现和声明。相同幂等键和内容重放不能重复写；同键不同内容报冲突；CAS 冲突必须重读重算。继承的旧 SQLite 回执若无 digest，保留兼容的同键命中语义，不能称为完整的历史内容冲突检测。

复用 [共享 Store 契约测试](../tests/test_store_contract.py)，并为真实外部数据库补事务、重启、并发和隔离测试。能力声明本身不能替代这些证据。

## 7. 导入、分页和规模

History Import 的 cursor 是原材料的字符偏移。`source_digest` 绑定材料；`import_fingerprint` 还绑定 scope、mount、locale、policy、材料类型、称呼、导入幂等键及批次规则。失败不推进该批 cursor；单批成功可续传；更改导入语义必须从头开始。`max_batches` 限制一次调用工作量，`max_cards` 限制单批输出，不限制总导入量。

浏览/导出默认每页 100、最多 1000 条，按字符串 ID 排序；继续传 `next_cursor`。cursor 对应卡消失时从头返回，调用方可能收到重复项。导出每页 `items.counts` 是该页统计，外层 `total` 是该次查询总量，不能把第一页当全量。

分页没有冻结跨请求快照。导出途中持续新增/编辑可能改变所见集合；需要一致备份时，由宿主控制写入或使用自己的快照机制。

当前 MountedGarden 与参考 Store 多条路径都加载 owner 的候选集合。更换数据库驱动本身不会让这层自动获得数据库分页、索引召回或快照导出。旧文档里的毫秒/内存数字没有随库提供可复现实验，此处不将它们作为容量承诺；上线按实际卡长、卡量、导入规模及并发测量。

## 8. 文档与历史证据维护

字段/接口改变时，同步代码、schema、契约测试与本文；操作步骤改变时更新相应指南。功能状态和待验证项统一更新 [STATUS](STATUS.md)。

阶段性评审保留在 PR / Git 历史。如确需留存文件，应明确标注日期、对应 commit 和“历史记录”，链接到当前状态；不要把旧审查结论不断改写成新事实。当前仓库无需恢复已从工作树移除的旧评审文件。
