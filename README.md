# Memory Garden

**让 Agent 不只记住说过的话，还能持续整理、更新，并在需要时想起来。**

[English](README.en.md) · [开始接入](docs/GETTING-STARTED.md) · [召回与混合检索](docs/RETRIEVAL.md) · [DeepSeek Harness](adapters/dsh-memgarden/README.md)

Memory Garden（Python 包名 `memgarden`）是可嵌入 Agent Runtime 的记忆编辑引擎。它把对话中的经历、偏好、关系和决定整理成可读的记忆卡，按本轮话题召回，并随着新信息合并、补充或取代旧记忆。

你提供模型、可信用户身份和存储；Garden 提供 Capture、召回、Dream 整理及持久化编排。可以从一个 SQLite 文件开始，也可以实现自己的 StoragePort。它不要求使用某一种 Agent 框架、模型服务或向量数据库。

Python ≥ 3.10 · 运行包零第三方依赖 · Apache-2.0 · 支持 Python SDK / JSON Lines / DeepSeek Harness Adapter

## 当前版本与适用范围

当前发布为 **0.23.0**：已有可独立接入的 Capture → 存储 → 召回 → Dream 工作流，不再是只有接口定义的拆分草稿。0.23.0 进一步要求模型保留具体经历中的名字、物品、金额和日期；这不是无损抽取或事实正确性的保证。

**当前源码包含尚未发布的收尾修复。** 已补并发快照、Capture 请求级重放、重新保存、全文读取和增量整理；`pip install memgarden` 的0.23.0不包含这些修改。仍是 Beta，真实宿主和模型联调边界见[当前状态](docs/STATUS.md)。

## 为什么是 Garden

对话日志记录“说过什么”；Garden 维护“以后值得记住什么”。

例如，一次对话里提到“我不吃辣，因为吃辣会胃疼”，以后又补充“微辣现在可以接受”：系统需要保留原因，辨别补充与矛盾，并在聊晚饭时带回相关记忆，而不是每轮追加一份摘要。Garden 为这条工作流提供以下能力，模型判断的实际质量仍需用你的场景评测。

同样，“5月2日花48美元给 Nora 买了一条黄色连衣裙”不应只剩“会给家人买礼物”。卡片正文用于保留具体经历；概括性的理解不能替代这些细节。**存下细节和把细节交给 Agent 是两步**：默认先召回摘要，再用 `memory_read` 按 ID 读取正文；无需把整座花园塞进上下文。

| 特点 | 对 Agent 的意义 |
|---|---|
| **可读的厚卡片** | 短摘要用于浏览，正文保留背景、原因和细节；不是只有关键词或 embedding |
| **三种写入意图** | 日常对话克制筛选；历史导入按更完整的尺度提取；用户明确保存不再被“值不值得记”拦下 |
| **自动记忆与主动工具分开** | Runtime 在轮末调用 Capture，不必等模型主动执行 `memory_write`；模型仍可主动搜索或写入 |
| **记忆会整理，不只会增加** | Maintenance / Dream 可以合并、补充、取代旧卡；旧卡关系可追溯，用户删除与整理归档分开 |
| **召回策略可换** | 可以组合现有策略、使用有相关性门槛的软配额，或由宿主提供向量做混合检索；不为凑名额强塞无关卡 |
| **写入有明确语义** | 两个内置 Store 提供 owner 隔离、幂等、并发版本检查及卡片/整理账本原子提交；修复与验收范围见状态页 |

Garden 保留自己的记忆判断能力。它不是把其他记忆产品统一成 CRUD 的通用门面，也不是向量数据库、完整 Agent Runtime 或聊天记录备份系统。

## 一轮对话如何接起来

```text
用户输入 → Garden 召回 → Runtime 把记忆作为上下文交给 Agent → Agent 回复
                                                                  │
                                   本轮对话 → Capture 判断 → 写入记忆卡
                                                                  │
                                   Runtime 选择时机 → Dream 整理 → 更新卡与账本
```

Garden 返回上下文块、来源 ID、改动和回执；Runtime 控制模型调用、上下文注入、调度和对外响应。记忆是用户数据，不是系统指令，不能据此绕过工具权限。

## 五分钟跑通

```bash
python -m pip install memgarden
memgarden manifest
```

下面的示例可独立运行，使用固定回复验证接线，**不调用真实模型**。它会在当前目录创建明文 `garden.db`；不要提交这个文件。换成自己的模型调用见[接入指南](docs/GETTING-STARTED.md)。

```python
import json
from memgarden import CaptureRequest, MountedGarden, Scope, SqliteStore
from memgarden.selection import Chain, RelevanceStage

class DemoModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        # Plumbing demo only. Replace this with your runtime's model call.
        return json.dumps({"cards": [{
            "action": "add", "summary": "Avoids spicy food",
            "content": "Spicy food causes stomach pain; choose mild dishes.",
            "bucket": "Food", "threads": ["diet"],
        }]})

garden = MountedGarden(
    model=DemoModel(),
    store=SqliteStore("garden.db"),
    selection_policy=Chain(stages=(RelevanceStage(limit=4),)),
)
# Build this from authenticated runtime state, never from model arguments.
scope = Scope(tenant_id="demo", memory_owner_id="user-42")
receipt = garden.capture_and_store(scope, CaptureRequest(
    window="User: Spicy food gives me stomach pain.",
    locale="en", idempotency_key="conversation-1:turn-1",
))
assert receipt.error is None, receipt.error
assert receipt.written

context = garden.context_for_turn(scope, "Can I eat spicy food?")
assert context.record_ids
for block in context.blocks:
    print(block["text"])
# Pass these blocks to your Agent as memory context before its next reply.
```

你应该看到 `Avoids spicy food` 这条记忆摘要。重新打开同一个 SQLite 文件仍可读到卡片；这个固定回复示例可重放而不重复写入。当前源码会按原始请求的稳定身份查询已提交回执，再决定是否调用模型；同一 key 换输入仍报冲突。这不代表任意宿主都已完成崩溃恢复接线。`created_at` 和 `updated_at` 由 Store 自动维护，`occurred_at` 单独表示素材中的事情发生时间。

### 摘要召回不等于全文读取

`context_for_turn` 返回摘要块和 `record_ids`；`search` 返回命中 ID 与分数；`memory_search` 给摘要和 ID。需要细节时调用 `memory_read(record_id, cursor?)`；SDK 为 `read_record`，wire 为 `records.get`。

完整卡片被序列化为 JSON，按 Unicode 字符分段返回；顺序拼接 `text`，续读 `next_cursor` 直到为空。默认每段5000字符，SDK/wire可配置1–20000字符；读取途中卡片修改则要求从头重读。只读当前 Scope 的有效卡，历史记录走显式 export。DSH 随工具列表自动注册 `memory_read`。

从源码运行完整的落卡→召回→工具→整理→重开数据库演示：

```bash
git clone https://github.com/teleport-computer/memgarden.git
cd memgarden
uv run python examples/mount_in_ten_minutes.py
```

## 选择你的接入方式

| 你的 Runtime | 从哪里开始 | 你需要接什么 |
|---|---|---|
| Python Agent | [Python SDK 指南](docs/GETTING-STARTED.md#python-runtime) | `ModelPort.complete`、可信 `Scope`、Store、轮前召回和轮后 Capture |
| 已有模型循环 / 非 Python Agent | [JSON Lines 与宿主驱动](docs/GETTING-STARTED.md#json-lines) | 管理服务子进程；按 begin/feed 往返调用自己的模型；检查业务回执 |
| DeepSeek Harness | [Adapter 安装指南](adapters/dsh-memgarden/README.md) | 兼容的 DSH、provider、稳定 owner、数据库和持久 stateDir；轮次 hooks 由 Adapter 接好 |
| 自己的数据库 | [StoragePort 与数据结构](docs/INTEGRATION-AND-DATA.md) | 实现接口及原子性、幂等、owner 隔离等行为；不仅是字段映射 |
| 自己的检索 / embedding | [召回接入指南](docs/RETRIEVAL.md) | 授权候选、搜索文本和角色投影；可选的向量、模型标识及校准阈值 |

日常接入推荐 `MountedGarden`：它把读库、权限范围检查、判断、CAS 和写回接在一起。`GardenComponent` 是只返回建议、不写库的低层入口。JSON 是调用协议，不是放入一个 JSON 配置文件就会自动工作的插件。

## 能力与边界

| 能力 | 当前提供什么 |
|---|---|
| Capture | 对话筛选、新增或取代；格式修正和有限重试；生成可选检索提示词 |
| 召回 | 上下文块与来源 ID；可插拔 SelectionPolicy；自动想起与主动搜索共用一个 BM25 排序器（分词器可注入）；可选 hybrid |
| Maintenance / Dream | 是否需要整理的确定性检查；模型提出合并等建议；原子更新卡与账本 |
| 明确保存与搜索 | `write_one`、`search` / `records.search`（只返回真实命中，无命中为空）、模型工具 `memory_write` / `memory_search` |
| 关联读取 | 取回卡时给出一跳邻居（显式链接、同线索）：`related` / `records.related` |
| History Import | 分批、游标续传、素材与语义指纹、失败可恢复；一次性导入需要服务侧模型，宿主驱动导入（`import_session` / `history.import_*`）由宿主调模型 |
| 浏览、导出、删除 | 分页读取；导出含归档和取代历史；指定卡真删，不用归档冒充删除 |
| Promote / Migrate | 经宿主授权移动可见范围；升级旧卡字段 |

SDK、JSON Lines 与 DSH Adapter 接通的能力**并不相同**（例如 DSH Adapter 没接关联读取和导入）。逐项对照见[数据参考](docs/INTEGRATION-AND-DATA.md#各接入面接通了什么)，代码里用 `memgarden.surfaces.surface_capabilities()` 查询。

**你的 Runtime 仍负责**身份认证、模型凭据与超时/取消/用量、调度、embedding、备份和全域数据删除。模型和 Store 都可替换，不表示这些宿主工作会消失。

请留意：

- 全链路明文，不包含加解密或密钥管理。数据库、待办和部分检索 trace 都可能含私密内容。
- `InMemoryStore` 用于测试；`SqliteStore` 是可直接使用的参考存储，但当前会读取 owner 的卡片集合。响应分页不等于数据库分页或快照导出。
- 默认 `RelevanceStage` 和主动搜索使用 BM25；自动上下文仍须显式配置 `selection_policy`。向量混合检索需要宿主主动接线，Garden 不生成或存储向量。
- Dream 默认每批最多60张卡：待处理卡与相关旧卡共同参与，按内容版本持久化进度。单批完成不代表全库完成；Runtime 后续继续调度，才能覆盖存量。预算不足明确返回错误，不会把未读卡标成已完成。
- 默认 DSH 服务使用宿主模型，只接通 Capture / Maintenance 的 begin/feed；没有 History Import / Migrate 的管理入口。安装后需检查运行时 `manifest.get`，不能只看静态 CLI 声明。
- 指定卡删除不自动删除宿主原始材料、备份、向量缓存和其他派生卡。DSH outbox 不应被多进程共用。
- 真实模型和 DSH 结果绑定具体 commit、模型和宿主版本；“单元测试通过”不代表“所有模型效果已验证”。见[验收状态](docs/STATUS.md)。

## 文档、开发与反馈

| 目的 | 文档 |
|---|---|
| 从安装到接入自己的 Agent | [Getting started](docs/GETTING-STARTED.md) |
| 选择和调试召回策略 | [Retrieval](docs/RETRIEVAL.md) |
| 字段、六张 SQLite 表、生命周期和恢复 | [接入与数据参考](docs/INTEGRATION-AND-DATA.md) |
| DSH 安装与 pinned 验收 | [DSH Adapter](adapters/dsh-memgarden/README.md) |
| 当前验证证据、已知限制 | [Status](docs/STATUS.md) |
| 开发、测试、提交 PR | [Contributing](CONTRIBUTING.md) |
| 判断质量与发版 | [Evals](evals/README.md) · [Releasing](docs/RELEASING.md) |
| 敏感信息与漏洞报告 | [Security](SECURITY.md) |

```bash
uv run --extra dev pytest -q
uv run python evals/run.py --baseline evals/baseline.json
uv build
```

运行包不依赖模型 SDK；开发测试使用额外依赖，DSH Adapter 离线测试需要 Node.js（CI 使用 Node 20）。源码文档可能包含尚未发布的修复，部署应固定已审核的 tag / wheel；不要覆盖已发布版本。

欢迎用[合成数据的最小复现提交 Issue](https://github.com/teleport-computer/memgarden/issues)。不要附上真实用户记忆或凭据。许可证：[Apache-2.0](LICENSE)。
