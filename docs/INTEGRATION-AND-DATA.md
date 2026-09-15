# 接入与数据参考

本文描述当前源码的接入和存储行为。初次接入请先读 [Getting started](GETTING-STARTED.md)，选卡与向量接线见 [Retrieval](RETRIEVAL.md)。完成度与验证基线只在 [STATUS](STATUS.md) 维护；本文不重复阶段性审查记录。

## 1. Runtime 需要接的路径

| 时机 | SDK / wire 入口 | 宿主处理结果 |
|---|---|---|
| 对话前召回 | `context_for_turn` / `context.get` | 把 `blocks` 注入本轮上下文，保留 `record_ids` 供追溯 |
| 主动搜索 | `search` / `records.search` | 只返回真实命中（`record_ids` + `hits` + `ranking`），无命中为空；不经过挑卡策略，不补最近卡 |
| 取回卡时的关联提示 | `related` / `records.related`（纯函数 `memgarden.related.one_hop`） | 把返回的一跳邻居（id、摘要、关系、是否历史版本）附在取回结果旁；要读全文由宿主在同一 Scope 再取。见 [Retrieval §7](RETRIEVAL.md#7-关联读取一跳邻居) |
| 对话后记忆 | `capture_and_store` / `capture.run` | 检查业务回执，再标记这段素材已处理 |
| 宿主自行调模型 | `capture.begin/feed/cancel` | 按 `needs_model` 调模型并 feed，直到 `completed`，再检查其中回执 |
| 检查、执行整理 | `check_maintenance`、`run_and_store_maintenance` / `maintenance.check/run` | 调度归宿主，卡片与整理账本由 Garden 一起提交 |
| 宿主驱动整理 | `maintenance.begin/feed/cancel` | 与 Capture 使用相同的模型往返方式 |
| 用户明确保存 | `write_one` / `records.write` | 不再套自动 Capture 的价值筛选 |
| 历史材料导入 | `import_history` / `history.import` | 持久保存原材料和 `ImportProgress`，失败后以相同语义续传 |
| 宿主驱动导入 | `GardenComponent.import_session`、`MountedGarden.import_session` / `history.import_begin/feed/commit/fail/cancel` | 按 `needs_model` 调模型并 feed；host 写入模式下按 `needs_commit` 写自己的库再 commit 真实 id。**每个回复都存下 `progress`**，续传就是带它重新 begin |
| 读取与导出 | `browse`、`export` / `records.browse/export` | 持续读取 `next_cursor`，直到为空 |
| 用户删除 | `delete_record` / `records.delete` | 提供请求身份并检查删除回执 |
| 改变可见范围 | `promote` / `records.promote` | 宿主先授权，不能把模型给的 `authorized` 当作权限证据 |
| 升级旧卡字段 | `migrate_and_store` / `records.migrate` | 给出旧卡和允许修改的 ID；这不是数据库 schema 迁移 |
| 给模型的工具 | `tools`、`invoke_tool` / `tool.list/invoke` | 绑定可信 Scope 后再执行；`memory_search` 走 `search`，只给摘要文本 |

SDK 完整参数见 [contracts.py](../src/memgarden/contracts.py) 和 [mounted.py](../src/memgarden/mounted.py)。wire 字段以 [`schema.py`](../src/memgarden/schema.py) 为准，可通过 `schema.get` 获取；不要假定 Python 请求类的每一个字段都由每条 wire 方法透传。

直接构造 SDK 时需注入挑卡策略；未配置 `selection_policy` 的判断组件不会自动返回召回结果。服务壳提供默认策略。

### 模型调用与能力声明

Python 模型接口是 `complete(prompt, *, purpose="") -> str`。凭据、超时、取消和模型选择由宿主实现。需要使用 Runtime 自己的模型调度时，走 begin/feed 路径。

Capture 与 Maintenance 的 SDK / 宿主驱动入口分别共用各自的内核状态机。成功调用却返回空白正文时，会按现有重试预算请求一次格式修正（默认最多额外一次）；连续空白明确失败，不当作“无需记忆／整理”，也不推进处理进度或整理账本。非空但没有 JSON 的纯文本仍按原策略报解析失败。截断、格式修正共享同一预算，provider 明确报错不伪装成空正文。SDK 可接既有 `{text, truncated}` 回复信封；宿主驱动时将正文和 `truncated` 分开传入 feed。

Dream（整理）提示词里的卡片**带正文**：按 `MaintenanceRequest.cards` 的顺序渲染 id、bucket、threads、occurred_at、summary、retrieval_cues 和 content。上限由请求字段控制——`cards_limit`（默认60张）、`cards_budget_chars`（卡片区总字符，默认60000，按整张卡累加，放不下下一张就停，不切半张也不跳着塞短卡）、`card_body_chars`（单卡正文，默认5000）、`card_summary_chars`（单卡摘要，默认2000）。被截断的卡在卡头和正文处标 `TRUNCATED`，提示词禁止把这种卡放进 `card_ids`；这是提示词约束，解析和守卫不据此拦截，需要硬拦的宿主可读 trace 的 `truncated_card_ids` 自行检查。trace 另记 `cards_rendered` / `cards_truncated` / `cards_omitted`。墓碑卡守卫的 `known_ids` 自动并入实际渲染的卡。宿主要把最该整理的卡排在前面；读字段只认 `summary` / `content` 等规范名，旧标题字段先翻译。wire 的 `maintenance.run` / `maintenance.begin` 接受同名的四个字段（都要 ≥1，不传用默认值）。称呼规则 `MaintenanceRequest.naming_rule` 与 Capture / 导入同义：不传按 `user_name` + `locale` 生成默认规则，宿主给的串原样进提示词；给 Capture 传了自己规则的宿主，Dream 也要传同一份，否则整理时换回默认规则重写卡片。wire 不接受这个字段。

Capture 的「已有记忆索引」决定模型能不能把新信息并进旧卡：索引为空时模型只能 add，同一件事说两次就是两张卡。宿主自己调模型（`capture_session` / `capture`）时，推荐把这个人现有的、可见的卡交给 `CaptureRequest.existing_cards`（明文，至少 `id` + `summary`，`bucket` / `importance` 有就用），而不是自己渲染 `cards` 串：组件按这段对话挑索引（与导入同一把尺子，相关的排前面），受 `index_cards_limit`（默认 60 张）、`index_budget_chars`（默认 16000 字）、`index_summary_chars`（单张摘要，默认 400 字，压成一行）约束；并且 merge/supersede 的 `target_id` 必须是 `existing_cards` 里的一张（对全部现有卡校验，不只对进了索引的那几张），不是就重问一次，仍不是只丢那一张（`Step` 的 `dropped` / `why="unknown_target"`，trace 的 `dropped_unknown_target`）。宿主同时给了 `cards` 串时，提示词用宿主的串，校验照做。不给 `existing_cards`（默认 `None`）时行为不变，`target_id` 是否存在留给宿主的写库校验。`MountedGarden.capture_and_store` 自己从 Store 渲染索引，不经过这条。

解析对模型输出的容错范围是有限的：Capture/Dream/Migrate 从回复中取第一个完整 JSON 对象，允许前后有说明文字、Markdown 代码围栏或推理块；合法 JSON 字符串里的 `{` `}` 不影响取块。只有 Capture 在直接解析失败后会尝试补转义裸引号，并按 JSON 结构判断：只修**对象成员的值**里的引号，例如 `她说"好的"然后走了`、`他报价"1000"块`、`He said "ok" and left`、`他说"好"、"行"`，以及引语在值末尾的 `"他只说了"算了""`；键名和数组元素（如 `threads`）里的裸引号一概不修。值里的引号后面紧跟 `,` `}` `]` `:`（如 `她说"好的", 然后`）与字段结束无法区分，也修不了。只要结构上还有别的错，整份不修：漏冒号（`"is_sensitive" True,`，无论后面跟的是什么）、数组元素之间写错分隔符（`["a"， "b"]`、`["a"、"b"]`、`["a” "b"]`、`["a" "b"]`）、成员或对象之间写错分隔符、缺/多逗号、截断。这些情况都报 `json_decode_error`（Capture 与 Maintenance 会按现有重试预算重问），修复不会把它们猜成另一种意思落库。值里既有裸引号又有 `}`（`"她说"好"然后看 } 这个符号"`）时，Capture 会用完整对象去修；Dream/Migrate 不做引号修复，这类回复对它们仍是 `json_decode_error`。已知局限（会按字面解析而不是报错）：值里的引号后面恰好是 `,` 且残文又拼成合法 JSON（`"她说"好的","content":"…"`）；值末尾多敲一个引号（`"好的""` 读成 `好的"`）；值里的引号后面紧跟 `}` 且对象恰好在这里闭合（`{"content":"a "b" }"}` 读成 `a "b`）。

`capture.run`、`maintenance.run`、`history.import`、`records.migrate` 需要服务侧配置模型。前三项另有宿主驱动路径（`capture.*`、`maintenance.*` 的 begin/feed，历史导入的 `history.import_*`，manifest 能力名 `import_session`）；`records.migrate` 在 wire 上没有。因此无模型服务的 `history_import`、`migrate` 为 false，而 `import_session` 为 true，这是明确的接入边界。

### 各接入面接通了什么

同一个能力名在三条入口上不是一回事。`memgarden.surfaces.surface_capabilities()` 按**实际存在的方法**算出这张表（SDK 看 `MountedGarden` 的方法，JSON Lines 看服务方法表，DSH 看 Adapter 里真正发出的 `client.request`），`tests/test_surfaces.py` 双向对账并快照：

| 能力 | SDK（`MountedGarden`） | JSON Lines | DSH Adapter |
|---|---|---|---|
| `capture` | `capture_and_store`；`prepare_capture` + `store_capture_result` | `capture.run`；`capture.begin/feed/cancel` | ✓（begin/feed/cancel，轮末 hook） |
| `turn_context` | `context_for_turn` | `context.get` | ✓（pre-step） |
| `search` | `search` | `records.search` | ✗（只有模型工具 `memory_search`） |
| `related` | `related` | `records.related` | ✗ |
| `maintenance` | `check_maintenance` + `run_and_store_maintenance` 或 `prepare_maintenance` + `store_maintenance_result` | `maintenance.check` + `run` 或 `begin/feed/cancel` | ✓（check + begin/feed/cancel） |
| `model_tools` | `tools`、`invoke_tool` | `tool.list/invoke` | ✓ |
| `curated_write` | `write_one` | `records.write` | ✗（只有模型工具 `memory_write`） |
| `browse` / `export` / `delete` / `promote` / `migrate` | 对应方法 | `records.*` | ✗ |
| `history_import` | `import_history` | `history.import`（需服务侧模型） | ✗ |
| `import_session` | `import_session` + `prepare_import_batch` + `store_import_batch` | `history.import_begin/feed/commit/fail/cancel` | ✗ |

这是静态接线。连上具体服务后以 `manifest.get` 为准，它还会按模型与 Store 能力关掉一部分。

独立 `memgarden manifest` 是静态声明；连接后的 `manifest.get` 才按实际模型和 Store 给出能力。`manifest.storage.capabilities`、`degradations`、`user_notices` 用于识别缺失条件，不是外部 Store 已经通过测试的证明。

JSON Lines 每行一个请求与响应，stdout 承载协议。示例请求：

```json
{"id":"browse-1","method":"records.browse","params":{"scope":{"tenant_id":"example","memory_owner_id":"user-42","allowed_mounts":["agent-private"]},"limit":100}}
```

请求 ID 可为字符串、整数或 null。先检查响应顶层 `error`，再检查业务结果中的 `error` / 工具的 `ok`；`completed` 只代表会话已结束，不能替代存储成功判断。

### 稳定公开模块

顶层 `memgarden.__all__` 之外，宿主常用的工具函数放在少数子模块里。`memgarden.STABLE_MODULES` 列出**承诺稳定**的那些，每个模块的 `__all__` 就是可以依赖的名字；删名字要先经过一个 deprecated 版本，并写进 [CHANGELOG](../CHANGELOG.md)。

| 模块 | 用途 |
|---|---|
| `memgarden.contracts` | 请求/结果数据类 |
| `memgarden.selection` | 挑卡插口：`Chain`、各 `Stage`、`SelectionPolicy` |
| `memgarden.timestamps` | 历史时间戳解析、排序键、规范化 |
| `memgarden.text.card_guard` / `card_text` / `leak_signals` | 卡片文本闸、JSON 取块、宿主泄漏识别器组合 |
| `memgarden.guards.dream_gates` | 整理结果的 id 泄漏与爆炸半径闸 |
| `memgarden.prompts.recall_fields` | `retrieval_cues` 规范化 |
| `memgarden.prompts.buckets` | 常用桶、写卡指引、桶名语言归一 |
| `memgarden.dreaming` | 整理门槛、快照与幂等键 |
| `memgarden.observability` | 内容无关的注入记录 |
| `memgarden.garden_language` | 花园语言判定 |
| `memgarden.policies` | 落卡档位与提示词常量 |
| `memgarden.retrieval` | 统一排序器 `rank`、自动想起 `select_context`、`Tokenizer` 插口 |
| `memgarden.related` | 关联读取纯函数 `one_hop`（挂了 Store 用 `MountedGarden.related`） |
| `memgarden.surfaces` | SDK / JSON Lines / DSH 各自接通的能力（`surface_capabilities()`） |
| `memgarden.conformance` | 写入路径共用验收场景：宿主实现 `Host` 适配器，在自己真实的写读路径上跑 `run_all`；`ReferenceHost` 是 MountedGarden + 官方 Store 的参考实现（见 §6） |

分批导入的会话对象 `ImportSession` / `ImportBatch` / `ImportBatchResult` 从顶层导出。Dream 带正文渲染的预算是 `MaintenanceRequest` 的 `cards_limit` / `cards_budget_chars` / `card_body_chars` / `card_summary_chars` 字段；渲染函数 `prompts.dream.render_dream_cards` 本身不是公开合同，宿主走 `maintenance_session` / `run_maintenance` 就会用到它。请求对象上宿主直接设置的字段和默认值同样由快照测试钉住。

不在清单里的模块（`prompts.capture`、`prompts.dream`、`scoring.*`、`rendering`、`importing` 的其余名字等）是内部零件：可以读、可以在测试里用，但不承诺兼容。宿主应在自己仓库加一条「只 import 公开 API」的守卫，以 `STABLE_MODULES` 和各模块 `__all__` 为准；`tests/test_public_api_surface.py` 在本仓库对这两样做快照。

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
| `retrieval_cues` | 可选搜索线索，字符串数组 | Capture/Dream 可生成；正式 Card、schema、typed mutation、Store/导出与 FieldMap 保留。宿主显式纳入 search_text 或 embedding 投影才用于检索 |
| `type` | Capture 类型字符串 | 当前解析器产出 `event` / `fact` / `quote` / `moment`；平铺卡可包含，`Card` 类型未单独声明 |
| `importance` / `pulse` | 重要度 / 情绪激活度，数值 | 供判断或策略使用；importance_level 1–5在解析时映射为既有importance 0.2–1.0，存储仍为0–1 |
| `occurred_at` | 事情发生时间，字符串 | 历史导入/人工档案按 `keep_dates=True` 保留；对话档 `keep_dates=False` 不传递；空值不推定日期 |
| `role` / `is_sensitive` | 记忆角色 / 敏感标识 | 供策略与宿主展示判断；敏感标识不替代访问权限 |
| `source` | 来源，开放字符串 | 内置工作流写入 `conversation_capture`、`history_import`、`curated`、`model_tool` |
| `source=memory_dream` | 整理产物，保留值 | 排除在下一轮原始卡新增水位之外 |
| `source_material_kind` | 导入材料类型 | 例如 `diary` / `chat_export`，由可信调用参数提供 |
| `source_actor` | 操作者对象 | MountedGarden 新增/取代时用可信 Scope 覆盖；普通 update 不得更改来源三个字段 |

字段定义见 [Card](../src/memgarden/records.py)。空的可选字段可省略。Capture 解析器保留模型提供的合法 `role` 字符串和 `is_sensitive` 布尔值；不保证模型每次都产出这些可选字段，也不把它们当作授权证据。日期按策略保留，日期字符串只有日期时不补时间，带时区时间转换为 UTC；未带时区的日期时间沿用现有解析约定按 UTC 解释，调用方应提供明确时区以免产生歧义。非法日期或元数据类型触发现有格式重试；字符串 `"false"` 不会被当作布尔值使用。字段映射工具 [FieldMap](../src/memgarden/adapt.py) 可以把外部字段转成候选卡；它不是完整的外部记忆系统适配器。

生成的 cues 当前最多5条、每条120个Python字符，是辅助线索的生成规则，不是正文或全部存量字段的硬存储上限。模型可能省略 cues，不据此判定记忆无效。这个可选字段不新增表、不提升数据库 schema 版本。持久 `role` 与选卡输入 `roles` 仍是两个接口形状；宿主如何显式映射见[召回指南](RETRIEVAL.md)。

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

### 共用验收场景（`memgarden.conformance`）

Store 契约测试验的是 `StoragePort` 这一层。**不走 MountedGarden、写入用自己执行器的宿主**（io 就是这种接法）过不了、也不该只拿它当证据：内置 Store 通过了幂等、删除、CAS，不代表宿主那条路也满足。`memgarden.conformance` 把共同业务语义写成 22 个可运行场景，宿主实现一个 `Host` 适配器（add / patch / supersede / archive / delete / observe、Capture 提交与进度、`inspect` 存储真相，以及 fetch / index / search / recall / related / history 六条产品读路径）后，在真实数据库上 `run_all`：

| 场景组 | 断言的语义 |
|---|---|
| `add.*`、`order.occurred_at` | 字段原样读回；created_at / updated_at 是带时区的同一写入时刻；自带 id 撞上已有卡（含历史卡）被拒且原卡不变；同分搜索按 occurred_at 从新到旧，日期与带时区值同轴 |
| `reads.no_side_effects` | 六条读路径都不刷新 created_at / updated_at / occurred_at |
| `patch.*` | 修改后只有一个当前版本；就地修改保留 id 与 created_at；修正正文不改 occurred_at / source / bucket / threads |
| `supersede.*`、`archive.retires` | 旧卡 superseded 并指向新卡、只在历史读取中出现；并发取代同一目标只成功一次；归档退出召回、历史可见 |
| `delete.*`、`conflict.*` | 真删后存储与六条读路径都读不到（含关联读取与正文）、重复删除安全、id 不复用；删除已被取代的卡优先；目标已删或已改时，基于旧观察的取代 / 修改报 not_found / conflict，不写继任卡 |
| `owner.isolation` | 另一 owner 读不到正文，patch / supersede / archive / delete 一律 not_found 且原卡不变 |
| `idempotency.*` | 同一请求重放不重复写、不改写时间；同一请求身份不同内容报 idempotency_conflict |
| `receipt.errors`、`content.length` | 规范错误类别（`ERROR_KINDS`），回执不回显正文；超长正文要么完整保存要么明确 invalid |
| `capture.*` | 判断成功但写库失败回 storage_failed、进度不动、不留半批，重试写一次、重放不重复；没什么可记推进进度且不是错误 |

宿主**有意**不同的地方按条款声明 `Deviation("by_design", 理由)`，已知缺陷声明 `Deviation("bug", 理由)`。结果只有四种：`pass`、`deviation`、`bug`、`fail`；未声明的失败和「声明了但其实已经通过」都是 `fail`，所以声明清单只会变短。`ReferenceHost`（MountedGarden + SqliteStore / InMemoryStore）零声明通过全部场景，`tests/test_conformance.py` 另用一组各破坏一条语义的坏宿主证明每组场景都会红。

范围：v1 不含 Dream 写回场景（账本与卡改动的原子性由 `test_maintenance_reaches_the_store.py` 覆盖），不含加解密，不调模型。场景证明的是写库与读路径语义，不证明检索质量。

## 7. 导入、分页和规模

History Import 的 cursor 是原材料的字符偏移（宿主预切 `batches` 时是各批文字长度的累计）。`source_digest` 绑定材料；`import_fingerprint` 还绑定 scope（宿主驱动时为 `owner_key`，默认 `actor.user_id`）、mount、locale、policy、材料类型、称呼、导入幂等键及批次规则；`strategy`、`max_total_cards`、`fallback_occurred_at`、`naming_rule`、`identity`、预切批次只在偏离默认值时进指纹，所以默认请求的旧进度仍能续传。失败不推进该批 cursor；单批成功可续传；更改导入语义必须从头开始。`max_batches` 限制一次调用工作量，`max_cards` 限制单批输出，`max_total_cards` 限制整次导入写出的卡数（add 与 supersede 都算；满了之后剩余批次不再调模型，并在 `skipped` 里记 `max_total_cards`）。

`MountedGarden.import_history` 和 `GardenComponent.import_session` 走同一个 `ImportSession`：切批、提示词、解析与重问、跨批去重、上限和进度推进是同一份代码。区别只在谁调模型、谁写库，以及索引从哪来——前者每个写卡批次前重读 Store，后者用宿主开会话时给的 `existing_cards`，并在每次 `commit(outcome, record_ids=...)` 时把刚写的卡（带宿主的真实 id）登记进后面批次的索引。`record_ids` 必须与 `mutations` 一一对应；写库失败用 `fail(outcome, error)` 记录，游标不动。

wire 上的 `history.import_begin` 接受和 `history.import` 同一组导入字段（外加 `write_mode`、`existing_cards`），续传指纹相同，所以两边存下的进度可以互相续。流程：

```text
history.import_begin(scope, material|batches, locale, …, progress?, write_mode?)
  → needs_model   {session_id, next_prompt, batch, progress, estimate}
history.import_feed(session_id, reply, truncated)
  → needs_model   本批重问 / 下一批（写库冲突后重读重算时带 retrying_after=conflict）
  → needs_commit  仅 write_mode=host：batch.mutations / cards / idempotency_key 交给宿主写库
                  → history.import_commit(session_id, record_ids) 或 history.import_fail(session_id, error)
  → completed     读完或整次上限已满；committed 是最后一批的回执
  → failed        这一批失败，游标不动，会话结束；带 progress 重新 begin 会重试这一批
history.import_fail(session_id, error)   宿主放弃当前这一批（模型调用失败、host 写库失败）
history.import_cancel(session_id)        丢掉会话，progress 不变
```

`write_mode=service`（默认）时服务把每批写进自己的 Store：写卡前重读、CAS 提交、冲突最多重算 3 次。`write_mode=host` 时服务不碰 Store，已有记忆索引来自 begin 的 `existing_cards` 与每次 commit 登记的卡。会话在进程内（与 capture 共用 15 分钟 TTL 和容量上限），服务重启或过期后给 `unknown_session`；**持久状态只有宿主存下的 `progress`**。已提交的批次续传时不再调模型；写回幂等键由批次内容和导入语义算出，但续传若对同一批拿到不同的模型回复，Store 会报 `idempotency_conflict` 而不是写第二份——所以每个回复之后都要存 progress。⚠️ `two_pass` 的 progress 含用户内容，按记忆正文等级保存，不要写进日志；服务自身不记录 progress、prompt 或 reply。

「已有记忆索引」按和这一批文字的相关性挑旧卡（最多 60 张，其中四分之一留给重要度最高的卡），不再只取重要度前 60。相关性默认用 `retrieval.rank`（关掉门槛、分词器跟组件的 `tokenizer=`，即 `importing.bm25_index_ranker`）；宿主可传 `index_ranker(batch_text, cards) -> ids` 换成自己的检索。桶名只做确定性收敛（大小写/空白一致并到已有写法，`中文/English` 通用桶对按 locale 取一半），近义词不猜。`fallback_occurred_at` 只填没有日期的卡，内核不推测日期。

`strategy="two_pass"`：每批先抽「候选事实 + 原话证据」（不写卡），材料读完后按 `write_batch_candidates`（默认 40）分组，用同一个 Capture 提示词写卡、去重、归桶。候选按字面归一后跨批去重（同义改写交给写卡模型），总数上限 4000，超出记进 `skipped`。⚠️ 两段式的 `ImportProgress.candidates` 含用户内容，宿主要按记忆正文的等级保存进度。两种形状哪个默认更好尚无结论，默认仍是 `single_pass`。

浏览/导出默认每页 100、最多 1000 条，按字符串 ID 排序；继续传 `next_cursor`。cursor 对应卡消失时从头返回，调用方可能收到重复项。导出每页 `items.counts` 是该页统计，外层 `total` 是该次查询总量，不能把第一页当全量。

分页没有冻结跨请求快照。导出途中持续新增/编辑可能改变所见集合；需要一致备份时，由宿主控制写入或使用自己的快照机制。

当前 MountedGarden 与参考 Store 多条路径都加载 owner 的候选集合。更换数据库驱动本身不会让这层自动获得数据库分页、索引召回或快照导出。旧文档里的毫秒/内存数字没有随库提供可复现实验，此处不将它们作为容量承诺；上线按实际卡长、卡量、导入规模及并发测量。

## 8. 文档与历史证据维护

字段/接口改变时，同步代码、schema、契约测试与本文；操作步骤改变时更新相应指南。功能状态和待验证项统一更新 [STATUS](STATUS.md)。

阶段性评审保留在 PR / Git 历史。如确需留存文件，应明确标注日期、对应 commit 和“历史记录”，链接到当前状态；不要把旧审查结论不断改写成新事实。当前仓库无需恢复已从工作树移除的旧评审文件。
