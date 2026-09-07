# Memory Garden

可插拔的 AI 记忆库：把对话整理成可读的记忆卡，在后续对话中召回，并维护记忆的更新、归档和删除。

保留 Garden 自己的 Capture、挑卡和 Maintenance/Dream 能力，通过 SDK 或 JSON 协议接入 Runtime。存储后端可以替换；兼容其他记忆系统整套算法不属于本库目标。运行包零第三方依赖，全链路明文，不包含加解密或密钥管理。

## 文档入口

| 想了解什么 | 阅读位置 |
|---|---|
| 产品范围、快速接入、宿主分工 | 本 README |
| 字段、实际存储表、生命周期、接口与恢复规则 | [接入与数据参考](docs/INTEGRATION-AND-DATA.md) |
| 哪些已验证、哪些仍待验收、工程师下一步检查什么 | [当前验收状态](docs/STATUS.md) |
| DeepSeek Harness 安装、配置与验收 | [DSH Adapter](adapters/dsh-memgarden/README.md) |
| 判断质量评测 / 发布流程 | [Evals](evals/README.md) · [发版指南](docs/RELEASING.md) |

文档描述当前 checkout。PR 中的改动在合并、发版前，不等于 PyPI 已发布版本具备这些行为；验证基线和证据见当前验收状态。

## 分工与接入方式

| 层 | 负责什么 | 接入方提供什么 |
|---|---|---|
| `GardenComponent` | 提示词、解析校验、记忆编辑判断、挑卡与整理计划 | 模型、候选卡、读写与权限编排 |
| `MountedGarden` | 在判断层上接好读库、owner/mount 检查、CAS、幂等写入、维护账本和回执 | 可信 Scope、模型、满足 StoragePort 的存储 |
| `memgarden serve` | 把 MountedGarden 暴露成 JSON Lines 请求/响应 | 子进程管理、认证边界、模型调用方式 |
| DSH Adapter | DSH 每轮召回、轮末 Capture、Maintenance、工具与待办恢复 | DSH provider、稳定 owner、持久数据库和 stateDir |

Runtime 负责身份认证、凭证、模型超时/取消/用量、任务调度、备份与部署。Garden 负责记忆业务规则。JSON 是接入协议；单独放一个 JSON 文件不会执行这些能力。

## 快速开始

安装已发布包：

```bash
pip install memgarden
memgarden manifest
```

审核当前源码时，在仓库根目录运行：

```bash
uv run python examples/quickstart.py
uv run python examples/mount_in_ten_minutes.py
```

两个示例都使用假模型，无需联网或凭据。以下 SDK 示例也可直接执行：

```python
import json
from memgarden import CaptureRequest, MountedGarden, Scope
from memgarden.selection import Chain, RecentStage
from memgarden.stores.memory import InMemoryStore

class ExampleModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        # 教学用固定回复；接入时换成宿主自己的模型调用。
        return json.dumps({"cards": [{
            "action": "add", "summary": "饮食清淡",
            "content": "对方不吃辣，一吃就胃疼。",
            "bucket": "饮食", "threads": ["饮食习惯"],
        }]}, ensure_ascii=False)

garden = MountedGarden(
    model=ExampleModel(),
    store=InMemoryStore(),  # 进程内演示；持久化可用 SqliteStore("memory.db")
    selection_policy=Chain(stages=(RecentStage(limit=4),)),
)
scope = Scope(tenant_id="example", memory_owner_id="user-42")
receipt = garden.capture_and_store(scope, CaptureRequest(
    window="对方：我不吃辣，一吃就胃疼。", locale="zh-Hans",
    idempotency_key="conversation-1:turn-1",
))
assert receipt.error is None, receipt.error
assert receipt.written
context = garden.context_for_turn(scope, "晚饭吃什么？")
assert context.record_ids
print(context.blocks)
```

`GardenComponent.capture()` 只返回建议的 mutations；`MountedGarden.capture_and_store()` 才返回存储回执。收到 RPC 响应后还要检查业务结果的 `error`，不能仅凭请求成功就告诉用户“记住了”。

## 主要能力

| 能力 | 当前行为 |
|---|---|
| 自动 Capture | 按对话策略筛选、写卡；支持新增和取代旧卡、解析失败反馈与重试 |
| 上下文召回 | 从可见且有效的卡中挑选，返回上下文块与来源 ID；挑卡策略可注入 |
| Maintenance / Dream | 判断是否需要整理，将多张旧卡合并成新卡，原子保存结果和整理进度 |
| 用户明确写入 | 直接保存用户选择的内容，不再判断“值不值得记” |
| History Import | 串行分批、每批重读已有记忆、游标续传与失败回执；当前需要服务侧注入模型 |
| 浏览 / 导出 | 分页读取；导出默认包含归档及被取代的记忆 |
| 归档 / 取代 / 删除 | 归档和取代保留内容；用户删除从当前 Store 移除目标卡 |
| Promote / Migrate | 经宿主授权移动 mount；升级已有卡字段并写回 |
| 模型工具 | 注册 `memory_search` / `memory_write`；自动 Capture 不依赖模型主动调用工具 |

具体运行能力取决于所接存储和模型。连接服务后读取 `manifest.get`，它会返回真实能力和降级信息；独立 CLI `memgarden manifest` 是包的静态声明。

## DeepSeek Harness

Adapter 随 Python wheel 分发，与 Garden 共用版本；安装后用 `memgarden install-dsh` 拷入 DSH profile。升级包后需要重新运行安装命令更新副本。

Capture 与 Maintenance 都支持宿主驱动：Garden 返回提示词，DSH 用自己的 provider 调用模型，再把结果交回 Garden。当前 DSH 默认无模型服务不提供 History Import / Migrate。详细步骤及 pinned DSH 版本见 [Adapter 文档](adapters/dsh-memgarden/README.md)。

## 存储与适用范围

内置 `InMemoryStore` 和 `SqliteStore` 两个参考实现，共用 mutation 执行语义和契约测试。SQLite 存储的是明文 JSON 卡片及并发、幂等、整理辅助状态；[数据参考](docs/INTEGRATION-AND-DATA.md)列出了六张表与真实字段形状。

新卡自动记录创建／更新时间；实际修改才更新修改时间，读取和幂等重放不刷新。事情发生时间 `occurred_at` 与这两个写入时间分开；旧卡缺失的历史创建时间不补造。

当前读写路径会加载 owner 的卡片集合。接口分页限制响应大小，不代表数据库读取已按页执行；大规模导入、并发导出和跨进程共享 outbox 需要另外验证。[当前验收状态](docs/STATUS.md)集中记录这些边界。

## 开发验证

```bash
uv run --extra dev pytest -q
uv run --extra dev python evals/run.py --baseline evals/baseline.json
uv run python examples/quickstart.py
uv run python examples/mount_in_ten_minutes.py
uv build
```

DSH 离线 Adapter 回归包含在 pytest 中，需要 Node；CI 固定 Node 20，并覆盖 Python 3.10–3.13。真实 DSH 和真实模型验收单独执行，未运行或 SKIP 不能算通过。

Apache-2.0，见 [LICENSE](LICENSE)。
