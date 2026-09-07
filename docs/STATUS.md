# 当前验收状态

更新：2026-09-08。当前行为验证基线：[6706e8d](https://github.com/teleport-computer/memgarden/commit/6706e8d9a013fc6381ef42c83aa7c373d704bb10)。本轮复核修正历史导出遗漏被取代卡、严格模型评测无凭据却报全部通过、Capture 丢失已声明元数据的问题，并补强 DSH 验收判据与依赖准备说明。

交付入口：[PR #1](https://github.com/teleport-computer/memgarden/pull/1)。本文记录该修复分支的验收状态，不代表其已合并、已发布；合并/发布状态以 PR 和对应 Release 为准。

## 已验证范围

| 范围 | 证据 |
|---|---|
| Capture、召回、整理、明确写入、导入、迁移与生命周期 | 当前基线本地 Python 3.10 / 3.13 全量 pytest 各 355 项通过 |
| 存储、隔离与恢复 | 两个 Store 共用契约测试；另覆盖 CAS、同租户跨 owner、跨 mount、幂等、真实 SQLite 事务回滚和旧库迁移 |
| 服务与协议 | JSON Schema 验证、错误响应、真实能力降级、会话过期和容量边界测试 |
| DSH Adapter 离线接线 | 实际加载 plugin.mjs，假 DSH 上下文接真实 `memgarden serve`；故障场景另用假 wire service |
| DSH 验收判据和依赖准备 | 离线反证测试拒绝已知假阳性；从官方固定 commit 的源码 SDK 环境安装、导入成功，并核对官方事件形状 |
| 包与示例 | sdist/wheel 构建、干净 venv 安装与 CLI/SDK 冒烟、两个独立示例通过 |
| 判断质量的确定性部分 | recall、gate、garden language 与语料完整性评测通过 |

当前提交的远程 CI 以 PR checks 为准，矩阵覆盖 Python 3.10–3.13。真实模型步骤可能因无 key 而 SKIP，整个 job 绿色不能替代该步骤的执行证据。本轮重跑确定性 eval、两个独立示例、sdist/wheel 构建、干净 venv 安装及 CLI/元数据 SDK 冒烟；前轮还核对了 README 代码、wire 请求以及真实 SQLite 表和卡片形状。两位 Agent 交叉复核；测试通过不等于已穷尽所有故障。

主要回归入口：[Store 契约](../tests/test_store_contract.py)、[本轮业务修复](../tests/test_final_closure.py)、[交叉审核回归](../tests/test_closure_review.py)、[历史分页导出](../tests/test_history_export.py)、[DSH 离线回归](../tests/test_dsh_adapter_offline.py)。新增的两种 Store 导出测试对原读取逻辑均复现失败，修复后通过。

`evals/run.py --with-model` 现在缺 key 会明确失败；普通 CI 单独调用 `capture.py` 仍可显式 SKIP。行为由 [评测 CLI 回归](../tests/test_eval_cli.py) 验证。

[Capture 元数据回归](../tests/test_capture_metadata.py) 覆盖策略差异、日期精度、时区转换、非法字段重试、导入失败不推进游标、两种 Store 与 SDK/宿主驱动/导入到导出的链路。极端时区日期溢出在修复前实际复现崩溃，修复后按无效日期处理。`conversation_capture.keep_dates=False` 的既有策略没有改变；历史导入和人工档案保留合法日期，`role`/`is_sensitive` 仅在模型提供合法值时保存。

[DSH 验收反证](../tests/test_dsh_acceptance_evidence.py) 要求自动召回有非空召回日志、独特事实且没有主动搜索；工具写入有真实事件和 `source=model_tool` 卡；重复卡整理夹具有成功回执、持久账本和取代链。普通整理的合法 no-op 不因此被禁止。依赖来源和可复现步骤见 [Adapter 文档](../adapters/dsh-memgarden/README.md)。

## 工程师接手后的验收

| 事项 | 当前结论 | 需要的下一步 |
|---|---|---|
| 当前提交的真实 DSH + 模型 | 未运行；当前验收环境无可用的 DSH+模型 key 组合。SDK 来源/版本与安装说明已经补齐 | 按 Adapter 文档准备 pinned 环境，运行 `dsh_acceptance.py`，记录 commit、版本、模型和各组结果；不要用离线通过代替 |
| 当前提示词的真实模型质量 | 本轮未运行真实模型；确定性 eval 明确提示没有运行该层 | 配好凭据运行 `evals/run.py --with-model`；保存不含真实用户材料的结果 |
| 卡片创建/更新时间 | 平铺卡片，未统一自动填充逐卡 `created_at`/`updated_at`；这不是本轮元数据修复的内容 | 待产品确认：建议新卡自动记录创建时间，实际修改更新修改时间，旧卡缺失的历史时间不补造；确认后定义写入责任及幂等/重放测试 |
| History Import / Migrate 的 DSH 入口 | 核心已有；默认无模型服务关闭这两项 | 当前接入不要调用；未来要在该宿主开放时补模型路径和管理入口 |
| 大规模读取与一致导出 | owner 全量读取；分页不是跨请求快照 | 按宿主规模压测；若要一致导出，明确快照或写入协调方案 |
| 完整“忘记” | 指定卡已真删；无跨素材、备份、派生卡自动级联 | 宿主需要全域删除时，补自己的协调流程和重放防复活测试 |
| 多进程 outbox / 服务部署 | 本轮未证明多个进程共享同一个 outbox 文件安全 | 每个实例独立 stateDir，或另行实现并验收共享队列；明确失败待办的运营处理 |

上表区分现有实现、宿主责任和待验证范围，不把每一项都当作本库必须新增的 P0 功能。卡片时间建议尚未作为已确认合同实施；不能只靠文档说明就认定需求已满足。

2026-09-04 的旧 DSH 实测属于历史证据，不能证明本次修改后的模型桥和恢复路径。当前可以进入工程师 PR 复核；真实 DSH 验收完成前，不作“当前版本所有链路均已实测”的结论。

## 维护方式

后续 PR 改变行为时更新这一份状态页的验证基线、证据和未完成项；概览在 [README](../README.md)，数据/接入细节在 [参考文档](INTEGRATION-AND-DATA.md)。阶段性讨论留在 PR 和 Git 历史，不再新增相互重叠的当前审查报告。
