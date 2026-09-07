# DeepSeek Harness Adapter

将 Memory Garden 接入 DeepSeek Harness，负责每轮召回、轮末 Capture、Maintenance、模型工具和待落卡恢复。判断与存储语义仍由 `memgarden serve` 执行。

当前完成度统一见 [STATUS](../../docs/STATUS.md)，数据和权限规则见 [接入与数据参考](../../docs/INTEGRATION-AND-DATA.md)。

## 安装与升级

Adapter 随 Python wheel 分发，无 npm 依赖。安装器把插件文件拷入 DSH profile，因此升级 Python 包后，需要重新运行 `install-dsh` 更新副本。

从 DSH 项目目录执行以下步骤；审核修复分支时先安装该 checkout 或构建出的确切 wheel，不能用旧 PyPI 包代替本次代码：

```bash
npm install @deepseek-ai/dsh@0.1.2-alpha.4
pip install memgarden
export DSH_HOME=/absolute/path/to/dsh-home
npx dsh --profile sdk-minimal --dump-default-config
memgarden install-dsh --tenant example --owner user-42 --locale zh-Hans
npx dsh --profile sdk-minimal
```

项目跟踪的兼容基线是 DSH `0.1.2-alpha.4`，commit `4e84901e6471b79ec0338099867ebb4606d12bb5`。真实模型凭据配置在 DSH provider 中。上述命令配置和运行宿主，会创建 profile、数据库及状态目录。

重复安装遇到已有 `id: memgarden` 时，**只更新插件副本，不覆盖原来的 YAML 配置**。更换 owner、数据库、stateDir 或 Python 环境后，要检查已有配置，不能假定新命令参数已写回。

配置示例：

```yaml
- insert:
    - id: memgarden
      name: 'dsh-memgarden'
      inject: [tools, llm]
      config:
        bin: /path/to/venv/bin/memgarden
        storage: 'sqlite:////path/to/garden.db'
        tenant: 'example'
        memoryOwner: 'user-42'
        locale: 'zh-Hans'
        stateDir: '/durable/dsh-state'
```

`tenant`、`memoryOwner` 来自可信宿主配置。owner 必须稳定，不可用 session 替代；缺 owner 时插件不启动记忆服务。一个配置绑定一座花园，不能把示例固定 owner 用于所有用户。

数据库保存卡片；stateDir 保存含对话窗口的待办。两者都需要跨进程重启保留。当前 outbox 没有跨进程文件锁，不应让多个插件进程并发写同一 stateDir。

## 一轮对话的路径

| 时机 | 扩展点与行为 |
|---|---|
| 每次模型调用前 | `agent/pre-step` waterfall，先保留其他插件处理结果，再查询并注入记忆 |
| 一轮结束 | `agent/turn-stopping`，记录待办，执行 Capture，再检查是否需要 Maintenance |
| 模型主动查/写 | 注册 `memgarden_memory_search` / `memgarden_memory_write`，经可信 Scope 调用服务 |
| 重启 | 从 outbox 重放未完成 Capture，沿用业务幂等键 |

轮末 hook 等待其 Promise 完成，因此记忆处理会增加 turn 收尾时间。自动召回和 Capture 不依赖模型主动调用工具。

模型由 DSH 提供：

```text
capture.begin     → DSH llm.stream → capture.feed
maintenance.begin → DSH llm.stream → maintenance.feed
```

服务启动时不带模型，不能直接调用 `capture.run` / `maintenance.run`；这些会返回 `model_not_configured`。History Import / Migrate 当前没有对应的宿主驱动 lane，因此该默认服务会在 manifest 关闭这两项。它们并非自动 turn hook，不能因核心接口存在就宣称 DSH 已具备管理入口。

## 窗口预算与恢复

Adapter 从本轮消息构造 Capture 窗口，包含用户、助手和工具结果。单次模型输入仍有预算：

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `captureMessageChars` | 16,000 | 单条消息插入预算 |
| `captureWindowChars` | 64,000 | 整轮插入预算 |

这些字符计数按 JavaScript 字符串长度计量，不等同于模型 token 或用户感知字符数。超预算时保留头尾并标出省略数量。outbox 保存的是这个已构造的窗口，不是完整原始对话备份；原始素材仍由 Runtime 保存。

`stateDir/memgarden-outbox.jsonl` 在模型调用前追加待办。完成且回执无 error 后移除；RPC 成功但业务写入失败时继续保留。清理使用同目录临时文件再 rename，避免直接清空原文件再写回的崩溃窗口。

outbox 写入失败会记日志并继续尝试 Capture，此时不能保证崩溃后恢复；原子 rename 也不等于已经证明断电持久性。失败待办、日志及其清理归部署方运维。库内会话是进程内状态，过期或重启后须重新 begin，持久回执承担写入幂等。

## 验证方式

在仓库根目录运行：

```bash
# 不联网：假 DSH 上下文 + 实际 Adapter + 真实服务
uv run --extra dev pytest -q tests/test_dsh_adapter_offline.py

# 不联网：只检查 MemGarden Service，不经过 Adapter
uv run python adapters/dsh-memgarden/e2e/failure_paths.py

# 还需下文所述 deepseek_harness Python SDK；当前仓库未锁定其安装来源
uv run python adapters/dsh-memgarden/e2e/dsh_acceptance.py
```

真实脚本通过 `dsh` 命令启动宿主；若只在项目本地安装 npm 包，需让对应可执行文件进入该验收进程的 PATH。

**真实验收还缺一个可复现的依赖准备说明**：`dsh_acceptance.py` 顶层导入
`deepseek_harness.DeepSeekHarness`，仅安装 npm DSH 和 memgarden 并不会提供该
Python 模块。当前仓库没有锁定该 SDK 的安装来源和版本；工程师应先补齐与
pinned DSH 匹配的来源/版本及安装步骤，再在验收解释器中验证
`import deepseek_harness`。本轮因此不能承诺从干净环境照上面的命令直接完成真实验收。

| 证据 | 覆盖范围 |
|---|---|
| pytest 离线 Adapter | 实际加载插件；模型桥、Capture/Maintenance、outbox、故障注入 |
| `failure_paths.py` | 服务错误边界；不能算 DSH 端到端证据 |
| `dsh_acceptance.py` | 真实验收的候选脚本：覆盖意图包括跨会话、工具、owner 隔离与整理；A/B/E 判据仍需补强，见下文 |

真实脚本 D 组当前只覆盖服务路径不存在、unknown_session、无模型错误和 manifest；
不应将握手不兼容、模型空回复等离线用例算成该组的真实环境证据。

工程师还应补强以下断言，避免真实脚本自己出现假通过：

- A 组仅看回复中出现“辣/清淡/温和”等常见词，不能排除模型没用记忆也作出类似回答；需补该会话实际召回和注入的证据。
- B 组检查工具注册及数据库中的相关卡，但轮末自动 Capture 也可能写出该卡；需断言真实工具调用及 `source=model_tool` 等可区分的写入证据。
- E 组只看“整理结果”日志及没有 `model_not_configured`，其他错误也可能通过；需检查回执无 error，并核对整理账本/卡片结果（合法 no-op 也需证明账本推进）。

这些断言补齐前，脚本跑绿也不足以证明三个场景全部完成。离线 pytest 的通过范围仍按自身测试报告解释。

真实验收报告需记录 Garden commit、DSH 版本/commit、模型、脚本结果和跳过项。不要保存凭据或真实用户对话。

旧文档记载 2026-09-04 在上述 DSH 基线上跑通过自动落卡、跨会话召回和工具；该历史结果不自动覆盖本次对模型桥及恢复路径的修改。本轮真实 DSH 与模型执行状态见 [STATUS](../../docs/STATUS.md)。

目录中 `dsh_e2e.py` 是带历史本机路径的早期实验，`deepseek_cli.py` 是绕过 DSH
provider 的独立模型桥；两者不作为当前安装入口或 host-driven 验收证据。

## 维护要点

- 模型消息使用分片数组；流式桥须传播文本及截断状态。
- pre-step 保留 waterfall 链；turn-stopping 返回收尾 Promise。
- Capture 窗口按 session/turn 隔离，幂等身份覆盖 tenant/owner/session/turn。
- 处理子进程启动、stderr、退出和超时；缺少记忆时向宿主报告降级。
- 升级 DSH 后同时跑离线回归与真实验收；旧 alpha 的结果不代表新版本兼容。
