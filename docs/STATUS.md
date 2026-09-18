# 当前验收状态

更新：2026-09-19。发布状态以 [Releases](https://github.com/teleport-computer/memgarden/releases) 和 PyPI 为准；代码合入与生产宿主升级是不同步骤。

## 当前能力与本轮范围

0.21.0 已发布（main 基线 `989f67b`），不再是尚未推送的 release/next 草稿。
0.21.1 是在该基线上的完整性修复，不改变默认检索算法、不增加外部运行依赖。

当前未发布变更（PR #8）：`retrieval.select_context` 可显式接收宿主向量，融合 BM25 与余弦排名；不替宿主生成或保存向量，不改变主动搜索或默认 SDK / JSON Lines / DSH 策略。保留原有全体合格候选软配额和相对时间排序，不新增时间窗口或前20张 shortlist。新旧入口差异见 [Retrieval](RETRIEVAL.md)。本次不自动发版或升级宿主依赖。

| 能力 | 当前实现与边界 |
|---|---|
| Capture | 公开 policy、已有卡索引、目标校验、解析和有界重问；宿主负责模型、可信身份、存储和调度 |
| 自动召回 / 主动搜索 | 共享 BM25 排序模块，可注入分词器；主动搜索不拿最近卡填补无命中，自动召回可以有明确的背景策略 |
| 关联读取 | SDK / JSON Lines 的显式链接与 thread 一跳关联；不提供多跳和反向关系查询 |
| Dream | 带正文和预算的渲染、目标安全检查、事务写回及维护账本；全部建议被目标检查拒绝时返回错误，不推进账本 |
| 生命周期 / 存储 | 内置 InMemory、SQLite；其他宿主可自行持久化，须验证共同语义，而非借用内置 Store 的测试结论 |
| 宿主共同验收 | 22 个场景，版本 2；包含完整存储与完整 fetch 的区分，允许显式拒绝超限写入，不允许静默截断后返回成功 |
| 接入面 | SDK、JSON Lines、DSH 的覆盖见 `memgarden.surfaces` 和对应指南；DSH 接通 capture、turn_context、maintenance、model_tools |
| History Import | 已有能力保留兼容；不是当前插件收尾的新功能要求。不新增导入管线或把宿主专有流程搬进内核 |

## 本轮确定性核验

PR #8 新增合成向量协议与融合测试；审查补丁先复现重复 ID 时返回分数被另一版本覆盖，再验证选中记录自身的分数与 trace 一致。补充候选数19/20/21的配额边界、旧日期/未来日期的相对排序测试，并将可运行向量示例切换到推荐的新入口。真实 embedding 召回质量不在这些测试的证明范围内，宿主启用前仍须标定。

以下为0.21.1已完成的完整性核验：

修复前以合成材料复现：全部 Dream 建议指向截断卡时，两个内置 Store 都返回成功并推进账本。新增回归改为要求 `maintenance_targets_rejected`、卡片与账本均不变。

另用故意截短 fetch 的坏宿主验证 `content.length/read_whole` 确实失败，参考宿主完整返回。场景版本从 1 升为 2，宿主需要重跑并清理失效的差异声明。

完整测试、确定性评测、示例、wheel 安装和兼容矩阵的 exact SHA / 结果随本轮 PR 与 CI 记录；不以旧版本的测试数量代替新提交验证。

## 真实模型证据与限制

本轮没有重跑真实模型或真实 DSH，不把确定性测试写成生产验收。

最近一次已记录的 DSH 专项验证是 2026-09-15：固定官方 DSH commit `4e84901e6471b79ec0338099867ebb4606d12bb5`、CLI `0.1.2-alpha.4`、模型 `deepseek-flash`。同步注册工具的修复后，单跑 B 组 16/16，完整 A–E 三次各 15/15；它只证明该版本与合成指令上的链路，不保证其他模型永远调用工具。

修复前后诊断、09-10 的 14/15 原始结果和环境细节保留在 [0.21.0 的状态记录](https://github.com/teleport-computer/memgarden/blob/989f67b29a13b923a38bfb5b33d22302fbf40e0a/docs/STATUS.md)。历史记录不覆盖本页当前状态。

## 仍需如实披露的边界

- 词法检索不保证同义改写、跨语言语义匹配。两条 strict xfail 记录泛词和多轮闲聊误召回；本轮不通过放宽断言或改默认阈值隐藏它们。
- `strong_evidence_terms` 等可选参数有合成评测，不等于真实用户效果验证；宿主产品配置可以不同，但应记录实际 ranking 版本。
- SQLite 按 owner 集合读取；跨请求快照、一致导出协调和目标规模压测由部署方案补齐。
- 指定卡真删不自动删除宿主原始材料、备份、外部向量和其他派生卡；宿主必须防止旧材料重放复活。
- DSH outbox 不保证多个进程共享一个 stateDir 安全；不同实例应独立目录。
- 默认无加解密、无向量数据库、无 IO / runtime / provider 依赖。IO 的单卡 5000 字符规则不是 MemGarden 的统一存储上限。

## 文档入口

[README](../README.md) 是概览；[Getting started](GETTING-STARTED.md) 是接入步骤；[数据参考](INTEGRATION-AND-DATA.md) 定义字段、存储和回执；[Retrieval](RETRIEVAL.md) 解释检索；[Release](RELEASING.md) 定义发布与产物核验。本页维护当前状态，阶段性审查与原始证据留在 PR / Git 历史，不再叠加相互矛盾的“当前报告”。
