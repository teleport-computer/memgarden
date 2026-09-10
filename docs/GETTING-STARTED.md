# 把 Memory Garden 接入你的 Runtime

[概览](../README.md) · [字段与存储](INTEGRATION-AND-DATA.md) · [召回策略](RETRIEVAL.md) · [DSH](../adapters/dsh-memgarden/README.md)

目标是一条可核验的闭环：**读到记忆 → 给 Agent 使用 → 本轮结束后判断是否该记 → 检查写入回执 → 下轮仍能读到**。Garden 不接管你的 Agent 主循环。

## 1. 安装与选择入口

需要 Python 3.10 或以上。安装运行包不需要模型 SDK 或向量数据库：

```bash
python -m venv .venv
# macOS / Linux；Windows 使用 .venv\Scripts\activate
source .venv/bin/activate
python -m pip install memgarden
memgarden manifest
```

部署时固定你审核过的版本。若在审核本 PR 的未发布代码，在仓库根目录使用 `python -m pip install -e .`，或安装本次构建的确切 wheel；不要把旧发行包的测试当成本分支证据。

| 入口 | 使用场景 |
|---|---|
| `MountedGarden` | Python Runtime，想直接完成“判断＋存储”；一般从这里开始 |
| `GardenComponent` | 已有自己的持久化编排，只需要 Garden 判断和 mutations；你必须执行并核验这些改动 |
| `memgarden serve` | TypeScript 等非 Python Runtime；通过标准输入输出调用 JSON Lines |
| DSH Adapter | 使用 DeepSeek Harness；由 Adapter 接好 hooks 和模型往返 |

## 2. Python Runtime

<a id="python-runtime"></a>

先运行 [README 的完整示例](../README.md#五分钟跑通)。它使用可重复的固定模型回复；这证明接线，不证明模型判断质量。要接真实模型，实现唯一的方法 `complete(prompt, *, purpose="")`：

```python
class RuntimeModel:
    def __init__(self, complete_text):
        self.complete_text = complete_text

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        # complete_text is YOUR callable, not a MemGarden API.
        # It owns credentials, deadlines, cancellation and usage accounting.
        return self.complete_text(prompt, purpose=purpose)
```

宿主函数返回正文字符串，或 Garden 支持的最小 `text`/`truncated` 信封；不要原样返回整个 provider 响应对象，也不要把 API key 拼进 prompt。`purpose` 区分 `capture`、`dream`、`migrate`。如宿主检测到截断，SDK 可返回 `{"text": "...", "truncated": True}`；不能把网络错误包装为“空 cards”。

异步模型可用于 `GardenComponent.acapture`。`MountedGarden.capture_and_store` 是同步入口；异步 Runtime 若需要自己管理完整生命周期，使用下文 begin/feed，不要把 `async complete` 直接塞进同步入口。

### 先确定三个宿主输入

| 输入 | 规则 |
|---|---|
| 模型 | 由你选择 provider、凭据、预算、超时；Garden 不保存 key |
| `Scope` | `tenant_id` 为安全边界，`memory_owner_id` 为稳定花园所有者；从认证结果构造，不从模型参数或每轮 session ID 构造 |
| Store 与策略 | 先用 `SqliteStore("garden.db")`；显式传入 `selection_policy`，不传不会自动产生上下文召回 |

`actor` 记录谁操作，`allowed_mounts` 限定可见范围；它们不是自动家庭/团队成员管理。相同用户跨会话想共享记忆，保持 tenant/owner 不变；不同所有者使用不同 owner。

### 在你的主循环接三处

以下使用 README 中已经构造的 `garden` / `scope`；`latest_user_text`、`turn_text`、`turn_id` 来自你的 Runtime：

```python
# Before the Agent replies: obtain bounded memory context.
context = garden.context_for_turn(scope, latest_user_text, limit=8)
memory_texts = [block["text"] for block in context.blocks]
# YOUR runtime inserts these as delimited, untrusted memory data.
# Keep context.record_ids for provenance; then run your Agent as usual.

# After the turn: turn_text contains the relevant conversation, not credentials.
receipt = garden.capture_and_store(scope, CaptureRequest(
    window=turn_text, locale="en", idempotency_key=turn_id,
))
if receipt.error:
    raise RuntimeError(receipt.error)  # production: retain durable retry work
```

必须区分三个结果：

- `error` 有值：失败；不要把素材标成已处理，不要告诉用户“已记住”。
- 无 error 且 `written=True`：写入成功或返回已提交的幂等回执。
- 无 error 且 `written=False`：可能是合法的“无需记忆”；检查 `reason`，不要假造卡片。

`idempotency_key` 标识同一份业务输入，例如会话＋turn 的稳定 ID。重试不能生成新 key；同一个 key 也不能拿来写不同材料。原始对话由 Runtime 先持久保存，Garden 的卡片不替代原始材料备份。

### 维护、明确保存和管理

| 需求 | SDK 入口 | 必要注意 |
|---|---|---|
| 用户说“请记住” | `write_one(scope, CuratedWriteRequest(text="...", locale="en"))` | 绕过自动价值筛选，但仍走授权和写入检查 |
| 给模型工具 | `garden.tools()`、`garden.invoke_tool(scope, call)` | 宿主绑定 Scope；模型不能自行扩大权限 |
| 检查是否该整理 | `check_maintenance(scope)` | 不调模型；同时检查 `.error` |
| 执行整理 | `run_and_store_maintenance(scope, MaintenanceRequest(locale="en"))` | Runtime 安排运行时机；Garden 提交卡片与账本，检查返回 `.error` |
| 导入旧材料 | `import_history(...)` | 原始材料和 `ImportProgress` 由宿主持久保存；变更材料/策略不能沿用旧进度 |
| 浏览 / 导出 | `browse(...)` / `export(...)` | 循环读 `next_cursor`；导出与列表投影不是同一形状 |
| 删除 | `delete_record(...)` | 指定卡真删；原材料、备份和向量缓存清理由宿主协调 |

参数和实际表结构见[数据参考](INTEGRATION-AND-DATA.md)，完整可运行演示：

```bash
uv run python examples/mount_in_ten_minutes.py
```

## 3. JSON Lines：复用宿主的模型循环

<a id="json-lines"></a>

启动一个子进程：

```bash
memgarden serve --storage sqlite:///garden.db
```

不是 HTTP 服务。stdin 每行一个 JSON 请求，stdout 每行一个响应；诊断读取 stderr。宿主管理进程、请求 ID、deadline、取消和重启。不要把标准输出当普通日志写入。

首先握手并查看实际能力：

```json
{"id":"hello","method":"manifest.get","params":{}}
```

没有配置服务侧模型时，使用以下协议往返，而不是 `capture.run`：

```text
capture.begin(scope, window, locale, idempotency_key)
  → status=needs_model, session_id, next_prompt
  → Runtime 用自己的 provider 调 next_prompt
capture.feed(session_id, reply, truncated)
  → needs_model：内核要求一次修正，继续往返
  → completed：检查 result.error / result.written / result.reason
```

请求顶层示例：

```json
{"id":"begin","method":"capture.begin","params":{"scope":{"tenant_id":"demo","memory_owner_id":"user-42"},"window":"User: I avoid spicy food.","locale":"en","idempotency_key":"conversation-1:turn-1"}}
```

不要预先猜测 `session_id`，使用 begin 返回的值。服务重启或会话过期后，重新 begin；使用相同业务幂等身份避免重复写卡。宿主终止模型调用时可 `capture.cancel`；整理使用同构的 `maintenance.begin/feed/cancel`。取消临时会话不是撤销已经提交的记忆。

**先检查响应顶层 `ok/error`，再检查业务 `result.error`。** `completed` 只表示状态机结束，不等于已保存。服务未配模型时，History Import / Migrate 仍没有宿主驱动入口，manifest 会明确禁用。

从源码运行带进程 deadline 的协议示例（模型回复固定、不付费）：

```bash
uv run python examples/wire_capture.py
```

它实际启动 `memgarden serve`，完成 begin/feed、存储、导出与召回。完整请求 schema 通过 `schema.get` 获取。

## 4. DeepSeek Harness

使用[专门的安装指南](../adapters/dsh-memgarden/README.md)。与自行接线相比，Adapter 已承担轮前召回、轮末 Capture、整理、工具注册和 outbox 恢复；你仍需配置兼容的 DSH/provider、稳定身份、数据库及持久 stateDir。

Adapter 随 wheel 分发；升级 Python 包后重新执行 `install-dsh` 更新插件副本，并检查已有 YAML（安装器不会覆盖你原有的配置）。DSH 安装闭包必须匹配验收基线，不能只固定顶层 npm 版本。

## 5. 接入完成怎么验收

1. 同一用户换新会话仍能读到卡；另一个 owner 读不到。
2. 同一 turn 重放不重复写入；真实失败后素材保留，不能推进为成功。
3. 上下文确实传给 Agent，而不仅仅是打印或写进数据库。
4. 需要整理时，卡片改动和整理账本同时提交；模型失败不推进账本。
5. 删除后的卡不能再召回；按你的产品规则清理原材料、索引和备份。
6. 在你的模型、语言、embedding 和材料上测召回与写入质量，而不只跑管道测试。

当前 SQLite 的 owner 集合读取、分页一致性及 outbox 部署限制见 [Status](STATUS.md)。切换 StoragePort 必须通过[公共 Store 契约测试](../tests/test_store_contract.py)，不能只完成表字段映射就宣布兼容。
