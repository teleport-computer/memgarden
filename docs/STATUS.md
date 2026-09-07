# 当前验收状态

更新：2026-09-08。当前行为验证基线：[78b0baf](https://github.com/teleport-computer/memgarden/commit/78b0baf86fcf07847c323d2c993fa793c5b9a9ee)。本轮补齐两个 Store 的卡片创建／修改时间，修复空模型回复的有限重试，并把 SDK 整理收敛到宿主驱动入口使用的同一状态机。此前的历史导出、Capture 元数据和严格评测修复仍在本基线内。

交付入口：[PR #1](https://github.com/teleport-computer/memgarden/pull/1)。本文记录该修复分支的验收状态，不代表其已合并、已发布；合并/发布状态以 PR 和对应 Release 为准。

## 已验证范围

| 范围 | 证据 |
|---|---|
| Capture、召回、整理、明确写入、导入、迁移与生命周期 | 当前基线本地 Python 3.10 / 3.13 全量 pytest 各 408 项通过 |
| 存储、隔离与恢复 | 两个 Store 共用契约测试；另覆盖 CAS、同租户跨 owner、跨 mount、幂等、真实 SQLite 事务回滚和旧库迁移 |
| 服务与协议 | JSON Schema 验证、错误响应、真实能力降级、会话过期和容量边界测试 |
| DSH Adapter 离线接线 | 实际加载 plugin.mjs，假 DSH 上下文接真实 `memgarden serve`；故障场景另用假 wire service |
| DSH 验收判据和依赖准备 | 离线反证测试拒绝已知假阳性；从官方固定 commit 的源码 SDK 环境安装、导入成功，并核对官方事件形状 |
| 真实 DSH + 模型 | 固定 DSH `0.1.2-alpha.4` + `deepseek-v4-flash`，A–E 五组 15/15 检查通过；下文记录范围 |
| 包与示例 | sdist/wheel 构建、干净 venv 安装与 CLI/SDK 冒烟、两个独立示例通过 |
| 判断质量的确定性部分 | recall、gate、garden language 与语料完整性评测通过 |
| 真实模型落卡质量 | `capture.py --provider deepseek --model deepseek-v4-flash --require-key`，5/5 场景通过，不是 SKIP |

当前提交的远程 CI 以 PR checks 为准，矩阵覆盖 Python 3.10–3.13。真实模型步骤可能因无 key 而 SKIP，整个 job 绿色不能替代该步骤的执行证据。本轮重跑确定性 eval、两个独立示例、sdist/wheel 构建、干净 venv 安装及 CLI/元数据 SDK 冒烟；前轮还核对了 README 代码、wire 请求以及真实 SQLite 表和卡片形状。两位 Agent 交叉复核；测试通过不等于已穷尽所有故障。

主要回归入口：[Store 契约](../tests/test_store_contract.py)、[本轮业务修复](../tests/test_final_closure.py)、[交叉审核回归](../tests/test_closure_review.py)、[历史分页导出](../tests/test_history_export.py)、[DSH 离线回归](../tests/test_dsh_adapter_offline.py)。新增的两种 Store 导出测试对原读取逻辑均复现失败，修复后通过。

`evals/run.py --with-model` 现在缺 key 会明确失败；普通 CI 单独调用 `capture.py` 仍可显式 SKIP。行为由 [评测 CLI 回归](../tests/test_eval_cli.py) 验证。

[Capture 元数据回归](../tests/test_capture_metadata.py) 覆盖策略差异、日期精度、时区转换、非法字段重试、导入失败不推进游标、两种 Store 与 SDK/宿主驱动/导入到导出的链路。极端时区日期溢出在修复前实际复现崩溃，修复后按无效日期处理。`conversation_capture.keep_dates=False` 的既有策略没有改变；历史导入和人工档案保留合法日期，`role`/`is_sensitive` 仅在模型提供合法值时保存。

[DSH 验收反证](../tests/test_dsh_acceptance_evidence.py) 要求自动召回有非空召回日志、独特事实且没有主动搜索；工具写入有真实事件和 `source=model_tool` 卡；重复卡整理夹具有成功回执、持久账本和取代链。普通整理的合法 no-op 不因此被禁止。依赖来源和可复现步骤见 [Adapter 文档](../adapters/dsh-memgarden/README.md)。

[28 项卡片时间回归](../tests/test_store_timestamps.py) 覆盖两个 Store、幂等重放、无变化写入、CAS、真实 SQLite 回滚、可信恢复、旧数据不补造创建时间，以及 SDK 写入后按创建时间召回。无需新表或 schema 迁移；外部 Store 应实现同样的写入语义。

[21 项空回复与入口一致性回归](../tests/test_empty_model_reply.py) 覆盖同步／异步 Capture、宿主驱动 Capture／Maintenance、同步 SDK Maintenance、回复信封以及共享重试预算。实际 Adapter 离线测试还验证：连续空回复不清理待落卡 outbox、不推进整理账本，provider 明确失败不伪装成空回复。

## 本轮真实模型验收

环境：官方 DSH 源码 commit `4e84901e6471b79ec0338099867ebb4606d12bb5` 的 Python SDK；npm CLI 及其 DSH 内部依赖统一固定为 `0.1.2-alpha.4`；模型 `deepseek-v4-flash`。验收使用合成材料，凭据仅经进程环境传入，不写入仓库或文档。完整 A–E 运行和真实质量 eval 均退出 0。

| 组 | 本次实际检查 | 结果 |
|---|---|---|
| A | 轮末自动记忆、模型由 DSH 提供、全新会话自动召回独特事实 | 3/3 |
| B | 工具注册、真实 `memory_write` 调用事件及 `model_tool` 来源卡持久化 | 2/2 |
| C | 同库同租户跨 owner 隔离，另一个 owner 无卡可召回 | 3/3 |
| D | 服务启动失败仍可对话、不伪造记忆、无效会话、未配置模型及非模型方法 | 5/5 |
| E | 达到整理条件后调用模型，成功回执、持久整理账本与旧卡到新卡取代链一致 | 2/2 |
| 质量 eval | 中文卡、英文卡、闲聊不记、明确偏好记一张、多个侧面合成厚卡 | 5/5 |

修复前实联曾为 14/15，E 组因空正文失败。排查发现 Adapter 提前抛错、内核未将空白正文纳入有限重试；进一步回归发现同步 SDK 整理绕过了公共状态机。修复后单独 E 组与完整 A–E 均通过。测试没有把空回复替换成成功 JSON，也没有放宽整理验收条件。空回复最初来自模型还是 DSH 内部处理，本轮没有定位到底层，不能据此宣称供应商问题已修复。

这些证据只证明上述固定版本、模型和场景，不代表其他模型质量、未来 DSH 版本或长期生产稳定性已经验证。普通 PR 的无凭据 CI 不依赖付费模型，其可选模型步骤仍可能 SKIP；本地实联证据与远程 CI 分开记录。

## 工程师接手后的验收

| 事项 | 当前结论 | 需要的下一步 |
|---|---|---|
| 固定环境的真实 DSH + 模型 | 本次已通过上述 15 项检查 | 工程师在目标部署环境复跑；升级 DSH 时重新验证版本、依赖闭包和实际事件行为 |
| 当前提示词的真实模型质量 | `deepseek-v4-flash` 的 5 个场景已通过 | 换模型或改提示词后重跑；不把五个场景外推为全面质量保证 |
| History Import / Migrate 的 DSH 入口 | 核心已有；默认无模型服务关闭这两项 | 当前接入不要调用；未来要在该宿主开放时补模型路径和管理入口 |
| 大规模读取与一致导出 | owner 全量读取；分页不是跨请求快照 | 按宿主规模压测；若要一致导出，明确快照或写入协调方案 |
| 完整“忘记” | 指定卡已真删；无跨素材、备份、派生卡自动级联 | 宿主需要全域删除时，补自己的协调流程和重放防复活测试 |
| 多进程 outbox / 服务部署 | 本轮未证明多个进程共享同一个 outbox 文件安全 | 每个实例独立 stateDir，或另行实现并验收共享队列；明确失败待办的运营处理 |

上表区分现有实现、宿主责任和待验证范围，不把每一项都当作本库必须新增的 P0 功能。卡片时间已明确为已有字段缺少自动写入的 bug，并补齐实现与回归，不再列为待产品确认。

2026-09-04 的旧 DSH 实测只作为历史证据，本次结论以上述当前基线重跑为准。现在可以交由工程师复核 PR，并在目标部署环境确认；尚未合并或发布，也不作“所有边界和生产场景均已验证”的结论。

## 维护方式

后续 PR 改变行为时更新这一份状态页的验证基线、证据和未完成项；概览在 [README](../README.md)，数据/接入细节在 [参考文档](INTEGRATION-AND-DATA.md)。阶段性讨论留在 PR 和 Git 历史，不再新增相互重叠的当前审查报告。
