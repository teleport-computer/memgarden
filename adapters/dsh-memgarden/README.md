# dsh-memgarden

把 Memory Garden 挂到 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 上的薄 Adapter。

> ## ✅ 已端到端跑通（2026-09-04，39 条验收全绿）
>
> 不是「照文档写的骨架」—— 用真实的 DSH 和真实的模型跑过：
>
> ```
> dsh          0.1.2-alpha.4（commit 4e84901e6471b79ec0338099867ebb4606d12bb5）
> 模型          deepseek-v4-flash（落卡侧 deepseek-chat）
> 改 DSH 代码   0 行 —— 只在 profile 的用户层加了一个插件
> ```
>
> **验到的场景**（sevenfloor §8.2 的核心几步）：
>
> ```
> 会话 A  用户：「我不吃辣，一吃就胃疼」
>         → 轮末自动落卡，桶=饮食
>
> 会话 B  全新会话，不复制 A 的任何对话
>         用户：「晚饭吃什么？」
>         → pre-step 自动召回
>         → 模型：「小米南瓜粥搭配清蒸鲈鱼和焯水的西兰花，温和养胃又不辣」
> ```
>
> **模型全程没有主动调用任何记忆工具** —— 它根本不知道有记忆系统存在。
> 这正是「只注册 MCP 工具做不到」的那件事：自动召回、自动落卡不依赖模型
> 记得去查。

## 怎么跑

```bash
# 1. 装 DSH 和 memgarden
npm install @deepseek-ai/dsh@0.1.2-alpha.4
pip install memgarden

# 2. 初始化 profile
export DSH_HOME=/absolute/path/to/dsh-home
npx dsh --profile sdk-minimal --dump-default-config >/dev/null

# 3. 一条命令挂上去 —— 不用手工连 symlink、拼 YAML
npx dsh-memgarden-install --dsh-home "$DSH_HOME" --profile sdk-minimal \
    --tenant acme --owner user-42

# 4. 跑
export DEEPSEEK_API_KEY=...
python e2e/dsh_acceptance.py
```

> 第 3 步以前是「照着下面的示例把插件挂进 cordis.patch.yml」，而真正能跑的
> 步骤（建目录、连 symlink、删掉默认文件里那个 `[]` 占位再拼 YAML）藏在
> E2E 脚本里。陌生工程师照 README 做不出来 —— 那说明这个 Adapter 还不算
> 能被别人装。现在装机脚本自己处理这些。

### `--owner` 为什么没有默认值

```
tenant    账户 / 组织 / 部署的安全边界
owner     一座长期花园的稳定所有者          ← 这个
agent /   此刻在执行的是谁（来源身份，不是归属）
session
```

给默认值的话，同一个 tenant 下所有用户共用一座花园，而且不报错 ——
`agent-private` 这个名字就没有意义了。拿不到 owner 时插件**直接不启用记忆**
（也不会起子进程、不建库），并在日志里说明原因。

**别拿 session 当 owner**：用户换个设备、重开一轮就会拿到一座空花园，
而这在测试里看不出来 —— 测试都是新建的。

装出来的 `cordis.patch.yml` 长这样：

```yaml
- insert:
    - id: memgarden
      name: 'dsh-memgarden'
      inject: [tools, llm]
      config:
        bin: /path/to/memgarden          # pip 装出来的可执行文件
        storage: 'sqlite:////path/to/garden.db'
        tenant: 'acme'                   # 🔴 来自你的可信上下文
        memoryOwner: 'user-42'           # 🔴 同上，且必填
        locale: 'zh-Hans'
```

注意**没有 `model`**：模型调用走 DSH 自己的 provider（`inject: [llm]`），
Garden 不持有 key，也不知道你用的哪个 provider。

## 接线点（都是 DSH 的正式扩展点）

| 时机 | DSH 扩展点 | 做什么 |
|---|---|---|
| 每次请求模型前 | `agent/pre-step`（waterfall） | 召回相关记忆，注入本轮上下文 |
| 一轮结束 | `agent/turn-stopping` | 落卡。**会让 turn 的结束等它做完**（几秒），但回复早已生成并发给用户了。不等的话进程可能随即退出，落卡在半路被杀且不报错 |

Adapter **只做翻译和接线**，不复制任何提示词 / 解析 / 挑卡 / 整理逻辑 ——
判断一律回到 Python 那边（`memgarden serve`）。复制过来最省事也最致命：
两边会慢慢漂，而漂了不报错，表现是「同样的对话，DSH 上记的东西和别处不一样」。

## 真跑才抓到的三件事

写这个 Adapter 的过程本身就说明了「照文档写」和「真跑一遍」的差距。

**① `content` 必须是分片数组，不能是字符串**

```js
// ❌ 整轮对话直接失败
{ role: 'user', content: '[记忆]\n- 不吃辣' }

// ✅
{ role: 'user', content: [{ type: 'text', text: '[记忆]\n- 不吃辣' }] }
```

传字符串时 DSH 内部会 `content.some(...)`，报
`content.some is not a function` —— **这句话和「记忆注入」看不出任何关系**。
类型检查发现不了，文档也没写死这一点。

**② `agent/pre-step` 是 waterfall，必须先 `await next()` 再追加**

自己造一份 `messages` 返回，会把别的插件加的东西悄悄丢掉，而且不报错。

**③ 子进程的 stderr 必须自己落一份**

Python SDK 会吞掉它。吞掉之后「插件没跑」和「跑了但报错」区分不开 ——
而这两件事的处置完全不同。

## 验收

```bash
# 真模型的正向场景（会花钱，慢）
export DEEPSEEK_API_KEY=...
python e2e/dsh_acceptance.py

# 坏情况（不联网、不花钱，秒级，随便跑）
python e2e/failure_paths.py           # 25/25
```

⚠️ **这两组不是一回事，别合并成一个数字报**：`failure_paths.py` 直接对
`memgarden serve` 发请求，**不穿过 DSH Adapter**。把它算进「DSH 端到端
证据」会高估覆盖 —— 它证明的是服务在坏情况下的行为，不是 Adapter 的。

    dsh_acceptance.py   DSH 正向 / 部分失败验收（真 DSH + 真模型）
    failure_paths.py    MemGarden Service 离线失败路径（不经过 DSH）

| 组 | 验的是 |
|---|---|
| A 自动落卡 + 跨会话召回 | 会话 A 说「不吃辣」→ 全新会话 B 问「晚饭吃什么」→ 模型答「温和养胃又不辣」。**模型全程没主动调任何记忆工具** |
| B 模型主动调工具 | `memgarden_memory_search` / `memgarden_memory_write` 注册进 DSH 的 Tool Registry，模型调了、真的落库 |
| C 同租户跨 owner 隔离 | **同一个 tenant、同一个 SQLite 文件**，`user-42` 写的 `user-99` 读不到（召回 0 条）；不配 owner 时记忆不启用、对话照常 |
| D 失败路径 | 服务起不来 / 中途退出 / 会话过期 / 越权挂载 / 卡住不回 / 快速两轮 / 整理与前台并发 / 幂等重放 |

模型调用**全部走 DSH 的 provider**（`ctx.llm.stream`）——
服务端启动时不带 `--model`，Garden 全程不碰 key。

## 真跑才抓到的这些

写这个 Adapter 的过程本身就说明了「照文档写」和「真跑一遍」的差距。
下面每一条都是**先跑绿了、才发现是错的**那一类。

**① `content` 必须是分片数组，不能是字符串**

```js
{ role: 'user', content: [{ type: 'text', text: '[记忆]\n- 不吃辣' }] }   // ✅
{ role: 'user', content: '[记忆]\n- 不吃辣' }                              // ❌ 整轮失败
```

传字符串时 DSH 内部会 `content.some(...)`，报
`content.some is not a function` —— **这句话和「记忆注入」看不出任何关系**。

**② `agent/pre-step` 是 waterfall，必须先 `await next()` 再追加**

自己造一份 `messages` 返回，会把别的插件加的东西悄悄丢掉，而且不报错。

**③ `agent/turn-stopping` 必须 `return` 那个 promise**

fire-and-forget（`void promise`）的话，turn 立刻结束、进程随即退出，落卡在
半路被杀掉：**没有报错，只是那条记忆没了**。宿主驱动比单次调用多两次往返，
这个竞态每次都稳定命中，花园里 0 张卡。

**④ 流分片的字段是 `text`，不是 `delta`**

写成 `chunk.delta` 时它永远 `undefined`，拼出来是空串 —— 空串让 Garden 解析
失败、产出 0 张卡，而**链路上每一步都「成功」**。现在空回复会当失败抛出来。

**⑤ `spawn` 的 `error` 事件没人听 → 整个宿主进程崩**

memgarden 没装、路径写错、没执行权限，后果都是「用户的 agent 起不来」，
报错还和记忆看不出关系。**记忆是增强不是依赖**：挂了只该退化成没有记忆。
这条是 D 组第一次跑就抓到的。

**⑥ 子进程的 stderr 必须自己落一份**

Python SDK 会吞掉它。吞掉之后「插件没跑」和「跑了但报错」区分不开 ——
而这两件事的处置完全不同。

**⑦ `truncated` 读的是字符串的属性 → 恒为 `undefined`**

```js
const reply = await callModel(...)     // 返回的是**字符串**
capture.feed({ truncated: reply.truncated === true })   // ❌ 永远 false
```

模型输出被截断时，内核以为拿到的是完整回复，把半个 JSON 当成「没什么可记」，
而不是重问一次。表现是长对话偶尔莫名其妙什么都没记住 —— 每一步都「成功」。
现在模型桥返回 `{ text, truncated, finishReason }`。

**⑧ 幂等键里没有 session → 两个会话的第一轮撞键**

`tenant + ':dsh:' + turn` 这个键，在两个会话都从 turn 1 开始时是同一个。
第二个会话的第一轮被当成第一个会话的重放：什么都不写，还回「成功」。
现在键是 `tenant + owner + session + turn`。

**⑨ 一个模块级 `turnText` → 两个会话串台**

`let turnText = ''` 是整个插件实例共用的。两个会话并发时，A 的 pre-step 会
覆盖掉 B 刚存的文本，于是 B 的落卡记的是 A 说的话。不报错，只是记忆里出现
「用户从没说过的事」。现在按 `(session, turn)` 存。

**⑩ 装机脚本生成了非法 YAML**

dsh 的默认 `cordis.patch.yml` 里有一个空数组字面量 `[]`。直接往后追加
`- insert:` 得到的是「一个文档里既有 flow 序列又有 block 序列」，
启动直接失败，而报错完全看不出跟装记忆插件有关：

```
failed to parse overlay ...: end of the stream or a document separator
is expected (5:1)
```

**⑪ 子进程在 owner 检查之前就起了**

没配 owner 的部署本该「什么都不做」，实际却 spawn 了一个 memgarden 进程、
建出一个空库，然后因为直接 `return` 而**永远没人关掉它** —— 泄漏一个进程，
还留下一个会让人以为「记忆在工作」的 db 文件。

## 还没做的

- **崩溃恢复只做到「不重复写」**。进程在 turn 中途被杀时，那一轮的落卡会丢。
  幂等键保证重放不写第二遍，但没有持久 outbox 把它补回来。正确做法是
  accepted 的落卡先落一条 cursor 再异步执行 —— 下一阶段。
- **大规模数据**。SQLite 参考实现会把一个 owner 的卡读进内存；它面向
  「开箱即用」，不面向超大库。
- **History Import**。Garden 侧声明为 `false`，这边也就没有入口。

## 版本纪律

DSH 处于 pre-release，官方说明允许 breaking changes。

- Adapter 必须 pin 确切 tag + commit，**不能跟随 `master` 浮动**；
- 升级必须重跑上面两个验收脚本，不能凭「上一版能跑」推断；
- 「在 alpha.4 验证通过」不自动代表未来 release 兼容。
