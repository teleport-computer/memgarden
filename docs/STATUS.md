# 当前验收状态

更新：2026-09-08。当前行为验证基线：[f9b2886](https://github.com/teleport-computer/memgarden/commit/f9b288655957b3652b9d6bce09f15b28394ccd5d)。本轮文档复核发现并修正历史导出遗漏被取代卡、严格模型评测无凭据却报全部通过的问题；分别补了存储/服务与评测 CLI 回归。

交付入口：[PR #1](https://github.com/teleport-computer/memgarden/pull/1)。本文记录该修复分支的验收状态，不代表其已合并、已发布；合并/发布状态以 PR 和对应 Release 为准。

## 已验证范围

| 范围 | 证据 |
|---|---|
| Capture、召回、整理、明确写入、导入、迁移与生命周期 | 当前基线本地 Python 3.10 / 3.13 全量 pytest 325 项通过 |
| 存储、隔离与恢复 | 两个 Store 共用契约测试；另覆盖 CAS、同租户跨 owner、跨 mount、幂等、真实 SQLite 事务回滚和旧库迁移 |
| 服务与协议 | JSON Schema 验证、错误响应、真实能力降级、会话过期和容量边界测试 |
| DSH Adapter 离线接线 | 实际加载 plugin.mjs，假 DSH 上下文接真实 `memgarden serve`；故障场景另用假 wire service |
| 包与示例 | sdist/wheel 构建、干净 venv 安装与 CLI/SDK 冒烟、两个独立示例通过 |
| 判断质量的确定性部分 | recall、gate、garden language 与语料完整性评测通过 |

前次提交 e78a0eb 的 [CI](https://github.com/teleport-computer/memgarden/actions/runs/34143620983) 已覆盖 Python 3.10–3.13，320 项测试通过；当前提交的 CI 以 PR checks 为准。真实模型步骤可能因无 key 而 SKIP，整个 job 绿色不能替代该步骤的执行证据。包构建/干净安装的前次证据来自 e78a0eb；本轮额外运行 README 代码、wire 请求以及真实 SQLite 表和卡片形状检查。

主要回归入口：[Store 契约](../tests/test_store_contract.py)、[本轮业务修复](../tests/test_final_closure.py)、[交叉审核回归](../tests/test_closure_review.py)、[历史分页导出](../tests/test_history_export.py)、[DSH 离线回归](../tests/test_dsh_adapter_offline.py)。新增的两种 Store 导出测试对原读取逻辑均复现失败，修复后通过。

`evals/run.py --with-model` 现在缺 key 会明确失败；普通 CI 单独调用 `capture.py` 仍可显式 SKIP。行为由 [评测 CLI 回归](../tests/test_eval_cli.py) 验证。

## 工程师接手后的验收

| 事项 | 当前结论 | 需要的下一步 |
|---|---|---|
| 当前提交的真实 DSH + 模型 | 未运行；缺 `dsh`、模型 key，验收脚本还依赖未锁定来源/版本的 `deepseek_harness` Python SDK | 先补齐 SDK 可复现安装说明，再在 pinned DSH 上运行 `dsh_acceptance.py`，记录 commit、版本、模型和结果 |
| 当前提示词的真实模型质量 | 本轮模型 eval 为 SKIP | 配好凭据运行 Capture eval；保存不含真实用户材料的结果 |
| 真实 DSH 验收断言 | 脚本 A/B/E 的判据仍可能假通过，不能仅凭该脚本绿色宣布闭环 | A 补实际召回/注入证据；B 区分工具写入和自动 Capture；E 断言成功回执及账本/卡片结果，详情见 Adapter 文档 |
| 卡片时间与输出形状 | 已核对现状：平铺卡片，未统一自动填充逐卡创建/更新时间 | 若产品要依赖时间排序/历史追溯，需明确字段写入责任并补行为测试；类型中有字段不等于已落盘 |
| 声明字段与 Capture 解析 | `occurred_at`、`role`、`is_sensitive` 在 Card 类型中，但自动 Capture 不透传这些模型字段 | 工程师核对实际产品是否依赖这些字段；如需要，补对应解析与全链路验证，不能只凭 schema 宣称支持 |
| History Import / Migrate 的 DSH 入口 | 核心已有；默认无模型服务关闭这两项 | 当前接入不要调用；未来要在该宿主开放时补模型路径和管理入口 |
| 大规模读取与一致导出 | owner 全量读取；分页不是跨请求快照 | 按宿主规模压测；若要一致导出，明确快照或写入协调方案 |
| 完整“忘记” | 指定卡已真删；无跨素材、备份、派生卡自动级联 | 宿主需要全域删除时，补自己的协调流程和重放防复活测试 |
| 多进程 outbox / 服务部署 | 本轮未证明多个进程共享同一个 outbox 文件安全 | 每个实例独立 stateDir，或另行实现并验收共享队列；明确失败待办的运营处理 |

上表区分现有实现、宿主责任和待验证范围，不把每一项都当作本库必须新增的 P0 功能。卡片时间规则尤其需要工程师结合实际产品使用确认；不能只靠文档说明就认定需求已满足。

2026-09-04 的旧 DSH 实测属于历史证据，不能证明本次修改后的模型桥和恢复路径。当前可以进入工程师 PR 复核；真实 DSH 验收完成前，不作“当前版本所有链路均已实测”的结论。

## 维护方式

后续 PR 改变行为时更新这一份状态页的验证基线、证据和未完成项；概览在 [README](../README.md)，数据/接入细节在 [参考文档](INTEGRATION-AND-DATA.md)。阶段性讨论留在 PR 和 Git 历史，不再新增相互重叠的当前审查报告。
