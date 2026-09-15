# 当前验收状态

更新：2026-09-15。当前代码与本地验证基线：`release/next` 分支 [01ecd8d](https://github.com/teleport-computer/memgarden/commit/01ecd8d)（本地提交，尚未推送；本页所在提交只改文档）。

结论：可以作为下一版的发布候选进入维护者 PR 审核；不应宣称真实模型和生产场景已验收。确定性测试、评测闸、接入示例、构建均通过；**本轮没有重跑真实 DSH / 真实模型**，第 3 节是上一轮（2026-09-10）的证据，不外推到本轮新增能力。例外：`fix/dsh-toolcall-flake` 分支针对 DSH B 组工具调用波动另跑了真实 DSH，结果单列在 3.1，只覆盖 DSH A–E，不代表本轮其他新能力已过真实模型。

## 1. 合并、发布与本轮改动

- 基线 `main` 为 [10e566f](https://github.com/teleport-computer/memgarden/commit/10e566f)（v0.20.1 之后，PR #5 已合并）。`release/next` 在它之上依次合入 `fix/parse-edge-cases` → `feat/recall-eval`（两者是链式分支）→ `feat/retrieval-unify` → `feat/related-dream-body` → `feat/import-session`，再加整合提交。**尚未发布新版本**；合并状态看 PR，发布状态看 Release/PyPI。
- 逐项变化与兼容影响见 [CHANGELOG](../CHANGELOG.md) 的 Unreleased 段；这里只列能力面。

| 本轮内容 | 变化及兼容边界 |
|---|---|
| 统一排序器（MG-1..MG-5） | `memgarden.retrieval`：BM25 + 可注入分词器 + 停用词 + 覆盖率/强证据门槛；自动想起 `select_context`、主动搜索 `rank` / `search` / `records.search` 同一把尺子、同一个版本号。**默认 `RelevanceStage` 换成 bm25**（`scorer="legacy"` 回滚），`evals/baseline.json` 随之重生成。`STABLE_MODULES` 首次列出公开子模块并快照 |
| 关联读取（MG-6） | `memgarden.related.one_hop` 与 `MountedGarden.related`，与 io 读侧实现由 144 组黄金用例对拍；仅 SDK，不做反向与多跳 |
| Dream 带正文（MG-7） | 整理提示词恢复卡片正文，`MaintenanceRequest` 新增四个预算字段；**提示词文本有变化**，依赖整份提示词快照的宿主要更新 |
| 宿主驱动历史导入（MG-8） | `GardenComponent.import_session` / `ImportSession`；`MountedGarden.import_history` 改跑在同一状态机上，默认请求的续传指纹不变；`two_pass` 的进度对象含用户内容；**单段式 `history_import` 提示词有变化** |
| wire 接通与分面声明（`feat/wire-capabilities`） | JSON Lines 新增 `records.related`、宿主驱动导入 `history.import_begin/feed/commit/fail/cancel`（manifest `import_session`），`maintenance.run/begin` 接受四个渲染预算；`memgarden.surfaces` 按实际方法给出 SDK / JSON Lines / DSH 各自的能力表并双向测试。老方法请求与回复不变；manifest 多出 `related`、`import_session` 两个能力键 |
| 写入路径共用验收场景（`feat/store-conformance`） | `memgarden.conformance`：22 个宿主无关场景 + `Host` 适配器协议 + 按条款的 `Deviation` 声明；`ReferenceHost`（MountedGarden + SqliteStore / InMemoryStore）零声明全绿，10 个坏宿主各破坏一条语义、对应场景全部变红（`tests/test_conformance.py`，Python 3.14 与 3.10 全量 947 passed, 2 xfailed）。宿主 io 已在真实 Postgres 写读路径上跑同一套场景，差异与缺陷清单在 io 仓库 `tests/test_memory_store_conformance.py`。不含 Dream 写回场景 |
| 整合 | 导入的已有记忆索引默认用 `retrieval.rank` 挑卡；想起/搜索/关联读取共用一个候选过滤（外部 Store 写在卡上的非 active 生命周期不再进想起和搜索）；`related` 进 `STABLE_MODULES`，宿主设置的请求字段与默认值进快照；多轮窗口评测与可选的 `strong_evidence_terms` |

没有新增存储表、加解密、默认向量存储或外部运行依赖；运行包仍只依赖标准库。

## 2. 确定性证据

| 检查 | 本轮实际结果（01ecd8d，本机 Apple Silicon） |
|---|---|
| 全量 pytest | Python 3.14.4 与 3.10.20 各 **910 passed, 2 xfailed**。两个 strict xfail 都是有意记录的已知缺陷：「我喜欢什么颜色的车」仍带回写着「喜欢」的卡；默认闸在多轮窗口上挡不住无关闲聊（见下） |
| 确定性 eval | `evals/run.py --baseline`：挑卡 recall 88.9%、违反禁忌 0、零召回 0；落库闸 16/16；花园语言 15/15；语料自查通过 |
| 召回排序评测闸 | 单句集（53 条，默认分词器）recall@5 0.932、MRR 0.891、无命中 4/5、有答案却返回空 1；多轮窗口集（16 条）默认闸 recall@5 0.962 / 无命中 0/3（strict xfail），`strong_evidence_terms=8` recall@5 0.923 / 无命中 3/3。数字与 jieba 对照见 [evals/retrieval](../evals/retrieval/README.md) |
| 导入索引挑卡 | `evals/retrieval/import_index.py`：4 话题 / 6000 字批次，该进索引的旧卡 0.775（初版词面重叠）→ 0.944（`retrieval.rank`） |
| 接入示例 | quickstart、mount_in_ten_minutes、wire_capture、retrieval_runtime 均退出 0 |
| 打包 | `uv build` 出 wheel/sdist（0.20.1）；wheel 含 `related` / `importing` / `retrieval` / `prompts/history_import`，运行依赖为零（Requires-Dist 只有 dev extra） |
| 版本与 diff | `scripts/check_version_consistency.py` 通过；`git diff --check` 通过 |

未做：远程 CI（分支未推送）、Python 3.11–3.13 矩阵、干净 venv 安装 wheel 后跑示例、Node 侧 DSH Adapter 以外的真实运行。本地结果不等于远程 CI。

## 3. 真实 DSH 与模型（2026-09-10 的上一轮证据，本轮未重跑）

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

工具调用稳定性仍待维护者复核。自动 Capture 不依赖模型选择该工具，本轮 A 组已通过；但不能拿 A 的成功代替 B 的工具验收。（2026-09-15 补充：根因复现与修复见 3.1；上表是 09-10 的原始结果，保持不变。）

### 3.1 B 组工具调用波动：根因复现与修复（2026-09-15）

环境：DSH 官方 commit `4e84901e6471b79ec0338099867ebb4606d12bb5` 本机源码构建（`dsh --version` = `0.1.2-alpha.4`），Python SDK 取自同一 checkout；Node 22.23.1、macOS。模型 `deepseek-flash`，与 09-10 记录相同。材料全是验收脚本的合成句子。与 09-10 的差别：机器不同；安装时 `@openai/codex` 和 `@anthropic-ai/claude-agent-sdk` 两个 darwin-arm64 可选二进制包从本地转存的 npm tarball 取（pnpm frozen lockfile 按 integrity 校验，内容相同），验收不加载这两个包。

**根因**：DSH 组装每一步请求时，先收集工具列表（agent-loop `preStep` 里的 `systemPrompt.assemble`），再跑 `agent/pre-step`；SDK `initialize` 也不等插件的异步 `apply`。Adapter 以前在服务握手和 `tool.list` 之后才注册 `memgarden_*` 工具，首个模型请求里没有它们。模型接下来有两种反应：直接回「当前环境未提供 `memgarden_memory_write`」结束本轮，B 组失败；或者先用 bash 找这个工具，拖到后续 step 时工具已注册，于是调用成功，B 组通过。日志里「注册了 2 个工具」两种情况都会出现。09-10 那次失败当时没留诊断，无法事后证明是同一原因，但失败形态与这里一致。

证据是每次 DSH `request/header` 事件里记录的工具名，不是推断：

| 代码 | 运行 | B 组判据通过 | 首个模型请求带 `memgarden_memory_write` | 说明 |
|---|---|---|---|---|
| `release/next`（54472c5，Adapter 与 fe3b8ed 相同） | 单跑 B ×15 | **12/15** | **0/15** | 3 次失败都是模型回「工具不存在」、没有任何工具调用；12 次通过都先调了 1–5 次 bash |
| 放弃的方案：`apply` 返回有界 Promise 等注册 | 单跑 B ×4 | 2/4 | 0/4 | 证明 SDK initialize 不等插件 apply，这条路无效 |
| 修复（apply 内同步注册），基于 54472c5 | 单跑 B ×16 | **16/16** | **16/16** | 每次只调一次 `memgarden_memory_write`，无 bash 绕路 |
| 修复（apply 内同步注册），基于 54472c5 | 完整 A–E ×1 | 15/15，退出 0 | 是 | |
| 修复提交 0fcd751（基于 fe3b8ed） | 完整 A–E ×2 | **15/15、15/15**，均退出 0 | 是、是 | |

确定性复现：`tests/test_dsh_adapter_offline.py`（经 `adapter_offline.mjs`）断言 `apply()` 同步返回时两个工具已注册，旧 Adapter 上得到空列表而失败；另有服务定义漂移、服务路径不存在两条降级用例。B 组诊断新增 `write_tool_offered_per_request`，以后再失败能直接看出请求里有没有工具。

仍未证明：模型拿到工具后是否**永远**会调用。修复后 19 次含 B 组的运行都调用了，这是 `deepseek-flash` + 这句验收指令上的小样本，不外推到其他模型、其他措辞或生产对话。DSH 升级后如果改了工具收集时机或 initialize 语义，需要重新验证。另修了顺带发现的 Adapter 缺陷：服务路径不存在且插件很快被 dispose 时，`kill()` 会落到 `kill(0, SIGTERM)` 杀掉宿主整个进程组（6a400f5，有离线回归）。

历史证据：2026-09-08 在 [78b0baf](https://github.com/teleport-computer/memgarden/commit/78b0baf86fcf07847c323d2c993fa793c5b9a9ee) + `deepseek-v4-flash` 曾完整 15/15、Capture 5/5。仅作历史记录，不外推到今天的模型、新功能或其他宿主版本。

复现见 [DSH 指南](../adapters/dsh-memgarden/README.md) 和 [Evals](../evals/README.md)。DSH 用 `MEMGARDEN_ACCEPTANCE_MODEL` 显式选择，Capture 用 `--model`；CI 使用 `EVAL_DEEPSEEK_MODEL` 仓库变量（默认 `deepseek-flash`）。无凭据、无兼容运行时、模型不可用应分别报告，不能变成假通过。

## 4. 维护者审核与发布判断

| 事项 | 当前结论 / 下一步 |
|---|---|
| 默认召回换尺子 | 默认 `RelevanceStage` 换成 bm25 会改变所有用默认策略的宿主（CLI / DSH 服务壳）挑到的卡；合成评测更好，但**没有真实用户语料证据**。审核回滚路径（`scorer="legacy"`）和版本号是否需要大版本信号 |
| 多轮窗口的门槛 | 默认闸在「最近四条对话拼成的查询」上几乎全部放行（无关闲聊平均带回 7.9 张）；可选的 `strong_evidence_terms=8` 能挡住，但会挡掉只靠编号命中的长粘贴，且 8 两侧参数区间窄、只有 16 条合成窗口。默认不开；宿主要不要开是产品取舍，上线后看 trace 的 `below_gate` / `evidence_scale` |
| 提示词变化 | Dream（带正文、TRUNCATED 规则）和单段式 `history_import` 开场都改了；**只有真实模型 e2e 能验证行为**，发布前需在目标模型上跑 `--with-model` 与 DSH A–E |
| 导入 | `two_pass` 默认不开，两种形状哪个更好尚无结论；`ImportProgress.candidates` 含用户内容，宿主的进度存储等级要跟上 |
| 生命周期过滤收紧 | 外部 Store 若把非 active 状态写在卡的 `status` / `lifecycle` 上，升级后这些卡不再被想起和搜到——这是修正，但属于可见行为变化 |
| DSH 工具稳定性 | 09-10 完整运行 14/15（工具写入单项复测 2/2）。09-15 定位根因：工具注册晚于 DSH 收集首个请求的工具列表（修前单跑 B 12/15、首个请求带工具 0/15）。`fix/dsh-toolcall-flake` 修复后单跑 B 16/16、完整 A–E 3 次均 15/15（见 3.1）。分支未合入 `release/next`、未推送；模型拿到工具后是否总会调用只有小样本 |
| Relevant/hybrid 效果 | 词法部分有合成评测；hybrid 的词法一侧仍是旧打分，真实 embedding 质量未校准 |
| 私密漏洞报告、公开发布 | 同上一轮：私密报告渠道需维护者确认；本轮未改仓库设置、未推送、未发布 |

仍存在但不能混称“本轮漏修”的接入边界：

- **History Import/Migrate**：核心已有；宿主驱动导入在 SDK 与 JSON Lines（`history.import_*`）都有，Migrate 在 wire 上仍没有宿主驱动入口；DSH Adapter 两者都没接。
- **关联读取**：SDK 与 JSON Lines 有，DSH Adapter 没接；不做反向（从旧卡找新卡）和多跳。
- **规模与导出**：SQLite 按 owner 集合读取，响应分页不提供跨请求快照；目标规模压测与一致导出协调仍是部署判断。
- **完整忘记**：指定卡真删，不自动级联删除宿主素材、备份、向量或其他派生卡；宿主需协调并防止旧素材重放复活。
- **outbox 部署**：没有证明多进程共享一个文件安全；实例独立 stateDir，或另外实现并验证共享队列。

这些边界不意味着要把 Garden 扩成宿主、向量服务或其他记忆系统的通用门面。接入者应按自己的规模、安全边界和模型验收。

## 5. 维护方式

只维护本页作为“当前状态”。概览在 [README](../README.md)，接入在 [Getting started](GETTING-STARTED.md)，字段/表在 [数据参考](INTEGRATION-AND-DATA.md)，检索在 [Retrieval](RETRIEVAL.md)。后续 PR 同步更新受影响指南、验证基线与未完成项；讨论留在 PR/Git 历史，不新增另一份相互矛盾的当前审核报告。
