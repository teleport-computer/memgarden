# 当前验收状态

更新：2026-09-10。当前代码与本地验证基线：[1e50d98](https://github.com/teleport-computer/memgarden/commit/1e50d98)。本页之后的文档收口提交不改变生产代码。

结论：可以进入维护者 PR 审核；不应宣称所有模型和生产场景已稳定验收。确定性测试、接入示例、构建安装均通过；本轮真实 DSH 完整验收 **14/15**，失败的工具写入单项复测 **2/2**。完整运行的失败不能被单项重跑抹掉。

## 1. 合并、发布与本轮改动

- 基线 `main` 为 [2bf2744](https://github.com/teleport-computer/memgarden/commit/2bf2744d50306a7a33b3c11a9d5a759132eb724a)。PR #1–#4 已合并，v0.20.0 的发布 workflow 已成功；此前状态页中“PR #1 尚未合并”的描述已过期。
- 本轮分支 `codex/open-source-readiness` 是 v0.20.0 之后的修复与公开接入文档，**尚未因此发布新版本**。合并状态看对应 PR，发布状态看 Release/PyPI，不能从文档推定。
- 新 relevant/hybrid 算法已在 v0.20.0 提供，本轮没有再造一套算法，也没有自动切换 SDK/DSH 默认召回。

| 本轮内容 | 变化及兼容边界 |
|---|---|
| 检索线索字段闭环 | `retrieval_cues` 补入正式 Card、schema、typed mutation 和默认 FieldMap；覆盖 Capture/Dream、两个 Store、SQLite 重开和导出。字段追加在旧 Card 全部位置参数之后，不改变旧顺序 |
| Hybrid 元数据校验 | 使用向量版本校验时，两侧 metadata 必须成对提供，实际参与计算的卡向量不能缺标签或标签不匹配；以前误接受的输入现在报 `VectorContractError`。完全不使用标签的兼容方式保留，无向量时仍词法降级 |
| 公开接入 | 重写中英文 README，新增 SDK/JSON Lines 主循环指南与检索指南、两个可运行示例和文档回归；同步字段、DSH、发版文档 |
| 验收维护 | DSH 脚本允许显式指定模型并记录；工具写入诊断区分调用与落库证据，不输出卡正文、不放宽判据。CI 模型通过仓库变量配置 |
| 开源卫生 | 新增 CONTRIBUTING/SECURITY、忽略本机与私密运行文件；移除两个含本机路径或绕过当前 provider 路径的旧实验脚本，Git 历史仍可恢复 |

没有新增存储表、加解密、通用 Memory 产品协议、默认向量存储或外部运行依赖。

## 2. 确定性证据

| 检查 | 本轮实际结果 |
|---|---|
| Python 3.10 / 3.13 全量 pytest | 各 **454 passed**，包含真实 SQLite/事务、owner 隔离、协议、时间戳、恢复、DSH 离线接线和新指南回归 |
| 对旧版的反证 | 在 `2bf2744` 上运行三项新回归，typed cues、FieldMap cues、缺向量标签均失败；当前全通过 |
| README 可执行性 | 中英文 Python 代码完全一致；运行两次、核对输出及 SQLite 仅一张卡 |
| 接入示例 | quickstart、mount_in_ten_minutes、wire_capture、retrieval_runtime 均通过；后两者包括真实子进程协议和 Scope/检索投影 |
| 确定性 eval | recall 10 查询、gate 16 场景、language 15 场景及语料自查通过既有基线 |
| 打包与安装 | wheel/sdist 构建；干净 venv 安装确切 wheel，CLI manifest、顶层 SDK、typed cues 和三个接入示例通过，运行依赖仍为零 |
| 版本、链接与 diff | 版本一致性通过（0.20.0）；指南本地文件链接、机器路径、`git diff --check` 通过。链接检查不等于逐个验证网页锚点 |
| 独立交叉审核 | 第二位 Agent 只读审查 API/边界/兼容性；定向 48 项和新示例通过，未发现高、中严重度问题 |

本地结果不等于远程 CI。PR checks 才是对应提交的远程证据；矩阵覆盖 Python 3.10–3.13。无 key 的模型步骤明确 SKIP，不能凭 job 绿色宣布实联通过。本仓未配置额外的完整类型检查或格式化 gate，本页不把 pytest/diff 检查称作这些检查。

回归入口：[检索线索](../tests/test_retrieval_cues.py)、[混合检索](../tests/test_hybrid_relevance.py)、[公开指南](../tests/test_public_guides.py)、[DSH 判据](../tests/test_dsh_acceptance_evidence.py)、[实际离线 Adapter](../tests/test_dsh_adapter_offline.py)、[Store 契约](../tests/test_store_contract.py)。此前卡片时间、空回复重试和入口一致性修复仍由全量套件覆盖。

## 3. 真实 DSH 与模型：本轮不能报全绿

环境：官方 DSH Python SDK 源码 commit `4e84901e6471b79ec0338099867ebb4606d12bb5`；CLI 及内部 DSH npm 依赖统一 `0.1.2-alpha.4`。使用合成材料，凭据只经运行进程传递，未提交原始日志或用户数据。

本次账号的模型列表不再包含历史 `deepseek-v4-flash`；不可用的原基线前置检查退出 2，未当成成功。随后**显式选择 `deepseek-flash`**，其结果独立记录，不能当作旧模型复测。

| 运行 | 检查 | 结果 |
|---|---|---|
| 完整 A | 轮末自动记忆、DSH 提供模型、新会话自动召回独特事实 | 3/3 |
| 完整 B | 工具注册；真实 memory_write 事件且以 model_tool 来源落库 | **1/2**；注册通过，调用＋落库组合判据失败 |
| 完整 C | 同库、同租户跨 owner 隔离 | 3/3 |
| 完整 D | 服务启动/模型配置/会话/协议错误，不伪造记忆成功 | 5/5 |
| 完整 E | 整理成功回执、持久账本、旧卡到新卡取代链 | 2/2 |
| 完整 A–E 总计 | 同一次完整运行 | **14/15，退出 1** |
| 单项 B 诊断复测 | 未改生产代码或判据；新增有界诊断后再运行 | **2/2，退出 0**；确有 memory_write 调用和一张 model_tool 卡，summary/content 均含验收标记 |
| 单独 Capture 质量 eval | 中文、英文、闲聊不记、明确偏好记一张、多侧面厚卡 | **5/5，退出 0**，不是 SKIP |

完整运行与单项复测使用同一组本轮生产代码和模型；复测只补验收诊断，没有修补生产 Adapter，也没有以自动 Capture 卡冒充主动工具卡。初次失败时未保存足够诊断，**尚不能定位是模型选择、provider/DSH 行为还是其他瞬态因素**。后续 B 通过只证明链路可工作，不证明已找到并修复首次失败根因。

工具调用稳定性仍待维护者复核。自动 Capture 不依赖模型选择该工具，本轮 A 组已通过；但不能拿 A 的成功代替 B 的工具验收。

历史证据：2026-09-08 在 [78b0baf](https://github.com/teleport-computer/memgarden/commit/78b0baf86fcf07847c323d2c993fa793c5b9a9ee) + `deepseek-v4-flash` 曾完整 15/15、Capture 5/5。仅作历史记录，不外推到今天的模型、新功能或其他宿主版本。

复现见 [DSH 指南](../adapters/dsh-memgarden/README.md) 和 [Evals](../evals/README.md)。DSH 用 `MEMGARDEN_ACCEPTANCE_MODEL` 显式选择，Capture 用 `--model`；CI 使用 `EVAL_DEEPSEEK_MODEL` 仓库变量（默认 `deepseek-flash`）。无凭据、无兼容运行时、模型不可用应分别报告，不能变成假通过。

## 4. 维护者审核与发布判断

| 事项 | 当前结论 / 下一步 |
|---|---|
| 检索修复与兼容性 | 审核 Card/schema/FieldMap 闭环、旧位置参数和严格向量标签的报错变化；使用新版本发布，不能覆盖 v0.20.0 |
| 文档是否准确表达产品 | 重点看厚卡、三种写入意图、Capture/工具分工、Dream、默认摘要上下文与宿主职责；对照真实主循环运行一次 |
| DSH 工具稳定性 | 在目标模型/profile 复跑完整 A–E，保留新诊断；单项通过不足以宣布稳定。若仍失败，收集有界脱敏证据后定位，不放宽验收 |
| Relevant/hybrid 效果 | 本轮验证协议、数学、投影和模拟向量；尚未完成真实 embedding/目标语料质量校准。默认阈值/权重不是已证明的最佳值 |
| 私密漏洞报告 | 检查时 GitHub 私密漏洞报告未开启。维护者应开启或提供可用私密渠道；本次仅写安全文档，未改仓库设置 |
| 公开与发布 | 检查时仓库已 public；本轮未修改可见性、合并或发布。通用凭据模式扫描在当时 94 个可达提交的 diff 未命中，不等于完整隐私/秘密审计；公开资料仍需维护者终审 |

仍存在但不能混称“本轮漏修”的接入边界：

- **History Import/Migrate**：核心已有；默认 DSH 无模型服务没有对应宿主管理通道，按运行时 manifest 降级。
- **规模与导出**：SQLite 按 owner 集合读取，响应分页不提供跨请求快照；目标规模压测与一致导出协调仍是部署判断。
- **完整忘记**：指定卡真删，不自动级联删除宿主素材、备份、向量或其他派生卡；宿主需协调并防止旧素材重放复活。
- **outbox 部署**：没有证明多进程共享一个文件安全；实例独立 stateDir，或另外实现并验证共享队列。

这些边界不意味着要把 Garden 扩成宿主、向量服务或其他记忆系统的通用门面。接入者应按自己的规模、安全边界和模型验收。

## 5. 维护方式

只维护本页作为“当前状态”。概览在 [README](../README.md)，接入在 [Getting started](GETTING-STARTED.md)，字段/表在 [数据参考](INTEGRATION-AND-DATA.md)，检索在 [Retrieval](RETRIEVAL.md)。后续 PR 同步更新受影响指南、验证基线与未完成项；讨论留在 PR/Git 历史，不新增另一份相互矛盾的当前审核报告。
