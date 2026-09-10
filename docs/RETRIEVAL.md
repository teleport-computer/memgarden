# 召回与混合检索接入指南

[接入主循环](GETTING-STARTED.md) · [字段与存储](INTEGRATION-AND-DATA.md) · [验证证据](STATUS.md)

Garden 提供选卡算法和策略接口；宿主决定怎样把它接入自己的上下文、搜索索引和 embedding。**安装或升级包不会自动开启新检索模式。**

## 1. 选择入口

| 入口 | 行为 | 如何接入 |
|---|---|---|
| `selection.Chain` | 组合 RelevanceStage、RecentStage、RoleStage 等；每段按自己的规则选卡 | 传给 `MountedGarden(selection_policy=...)` |
| `scoring.relevance` 的 default / strict | 保留既有行为；default 中可有不经过相关性门槛的最近卡/转折卡 | 低层函数，传入已授权候选 |
| `select_relevant_context_memories_with_trace` | 各类候选都必须通过词法相关性门槛；转折≤3、最近≤2，空余名额供其他相关卡使用，默认总数8 | 调用低层函数，或用下面的 SelectionPolicy 包装 |
| `select_hybrid_context_memories_with_trace` | 向量与词法分别过门槛，再用加权 RRF 融合；在 shortlist 内按软配额选卡 | 显式传入宿主向量及参数；不在旧 dispatcher 或 DSH 配置中自动启用 |

`mode="relevant"` 只属于 `scoring.relevance.select_context_memories_with_trace`，不是 `context_for_turn` 的一个参数。调用低层选卡函数前，必须先过滤 tenant/owner/mount 和归档、取代、删除状态；这些纯函数不替你做授权。

默认 `context_for_turn` 的 block 文本是**记忆摘要**，同时返回 `record_ids`。厚正文保存在卡中；需要全文时，由宿主在同一授权范围读取并按上下文预算组织，不要假定 block 已包含全部细节。

## 2. 三个容易混淆的字段

| 字段 | 当前责任与行为 |
|---|---|
| `retrieval_cues` | Capture/Dream 可生成的搜索线索，保存在卡里；不是新事实，也不会自动进入词法打分 |
| `search_text` | 宿主构造的搜索投影，存在时优先用于词法匹配；缺失时算法只用 summary/content/bucket |
| `role` / `roles` | 持久 Card 是单个 `role` 字符串；选卡器接收 `roles: list[str]`。宿主需要显式映射，例如 `role="turning_point"` → `roles=["turning_point"]`，不会自动迁移或猜测标题 |

如果希望 cues 参与检索，把它们加入宿主的 `search_text` 或 embedding 输入。不要把检索投影覆盖回原始记忆正文，也不要把 cues 当成事实证据。`importance_level` 1–5 在解析时映射到既有 `importance` 0.2/0.4/0.6/0.8/1.0，存储仍是 0–1；不是新增一套长期数值字段。

生成器将 cues 规范化为最多5条、每条120个 Python 字符；这是模型生成的辅助线索预算，不是正文存储上限。正式 Card/schema 的可选字符串数组不对既有或外部卡片增加这两个硬限制。

## 3. 接到 MountedGarden

[可运行示例](../examples/retrieval_runtime.py) 提供 `RelevantPolicy.select`：

1. 接收 MountedGarden 已按 Scope 过滤的候选。
2. 在候选副本中把 cues 加入 search_text，把 role 映射为 roles；保留宿主已显式提供的投影。
3. 调用 relevant 选卡函数。
4. 返回 `SelectionResult(Pick(...))`，只给出候选 ID 和选择依据；Garden 使用原始卡片生成上下文。

```bash
uv run python examples/retrieval_runtime.py
```

示例还检查另一个 owner 召回不到这些记忆。不注入这类策略就不会隐式切换默认行为。直接用自定义 Store 的宿主同样需要遵守权限和生命周期过滤。

## 4. Hybrid 的分工

宿主负责 embedding 模型、投影文本、向量存储、更新/删除、权限和成本。Garden 只接收：

- 本次已授权的候选卡与查询文本；
- query vector、`card_id -> vector`，以及可选但建议成对提供的模型/投影版本标识；
- 经你的模型和语料校准的 `min_cosine`；它没有默认值；
- `reference_time`：宿主认为的当前时刻，用来界定“最近”。

各向量必须维度相同、分量有限且范数非零。启用向量计算时，若使用版本校验，`vector_model` / `card_vector_models` 必须成对提供，每张实际参与计算的向量都必须有匹配标签；不接受“缺标签当匹配”。不提供版本校验时，维度相同不等于模型相同，宿主自行保证来源一致。

词法默认门槛为0.35且至少 medium 证据；向量使用宿主的余弦门槛。通过任意一侧即可成为候选，每侧只给自己合格的卡排名。融合使用 `w_v/(k+r_v) + w_l/(k+r_l)`；缺席一侧贡献0，权重0禁用该侧。默认 k=20、权重2:1是算法初始值，**不是在你的数据上验证过的最佳参数**。

融合 shortlist 默认20张（至少覆盖 cap），软配额在其中分配席位，最终按融合顺序返回。没有向量的卡只参加词法通道；没有查询向量时明确降级为词法。空查询返回空，不用向量暗中注入记忆。

不传 `reference_time` 时以最新候选的创建时间为参照，是确定性的后备行为：一座全旧的花园也可能被视为“最近”。线上建议传入明确的当前时间，不能拿这个后备规则推断真实新鲜度。

## 5. 向量生命周期必须跟着卡片走

| 卡片变化 | 宿主的向量/索引操作 |
|---|---|
| 新建 | 按同一模型和投影版本生成向量，绑定卡 ID |
| 更新正文/cues | 使旧向量失效或重新生成；未准备好的卡暂时只走词法 |
| Dream 取代旧卡 | 不让旧卡进入 active 候选；为新卡建立自己的向量，不挪用旧卡 ID |
| 删除 / 改权限 | 同步移除或隔离索引、缓存；在调用 Garden 前再次按当前权限过滤 |
| 更换 embedding 模型或投影 | 更新版本标识并重建；不要混算不同模型的向量 |

需要通过 SelectionPolicy 接入 hybrid 时，把向量提供能力包在宿主策略对象里，沿用示例的“返回 Pick，不返回自造卡片”方式；不要把一个用户的查询向量放在多用户共享对象的可变全局字段里。

## 6. 验证与观测

`trace` 包含分数、排名、所选 bucket、缺失向量/被门槛挡下的样本。它不回显向量，但可能包含标题和命中词，**不是天然脱敏日志**。传出信任边界前由宿主做字段白名单处理。

发布前至少检查：语义近似但用词不同的命中、编号/专名的精确命中、无关卡排除、空查询、部分向量缺失、角色和最近时间、更新/取代/删除后索引一致。对比旧策略和新策略的同一批人工标注样本，记录参数与模型/投影版本。纯数学与模拟向量测试不等于真实 embedding 质量评测。

早期实现说明：[T510/T513](T510-selection.md)、[T523](T523-hybrid.md)。当前接入方式以上述指南和源码接口为准，当前验收范围只在 [Status](STATUS.md) 维护。
