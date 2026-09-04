# dsh-memgarden

把 Memory Garden 挂到 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 上的薄 Adapter。

> ## ✅ 已端到端跑通（2026-09-04）
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

# 3. 把插件挂进 $DSH_HOME/profiles/sdk-minimal/cordis.patch.yml
#    （见下面的配置示例）

# 4. 跑
export DEEPSEEK_API_KEY=...
python e2e/dsh_e2e.py
```

`cordis.patch.yml`：

```yaml
- insert:
    - id: memgarden
      name: 'dsh-memgarden'
      inject: [tools]
      config:
        bin: /path/to/memgarden          # pip 装出来的可执行文件
        storage: 'sqlite:////path/to/garden.db'
        tenant: 'user-1'                 # 🔴 来自你的可信上下文
        locale: 'zh-Hans'
        model: 'python3 e2e/deepseek_cli.py'
```

## 接线点（都是 DSH 的正式扩展点）

| 时机 | DSH 扩展点 | 做什么 |
|---|---|---|
| 每次请求模型前 | `agent/pre-step`（waterfall） | 召回相关记忆，注入本轮上下文 |
| 一轮结束 | `agent/turn-stopping` | 后台落卡，不阻塞回复 |

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

## 还没做的

- **模型调用没走 DSH。** 现在是 `memgarden serve --model <命令>` 自己调
  DeepSeek。按 sevenfloor §5.4，正确形态是 host-driven session：DSH 用自己的
  provider 调模型（它持有 key、路由、用量、超时、取消、重试），Garden 只决定
  问什么、怎么解析、要不要重问。那需要服务侧提供 `capture.begin` / `capture.feed`。
- **失败路径没试**：子进程不存在、握手不兼容、ModelPort 超时、capture 中途
  退出、hot reload、两轮快速连续到达、整理与前台并发。
- **模型工具没注册**：`memory_search` / `memory_write` 还没接进 DSH 的 Tool
  Registry（自动召回不依赖它们，但模型主动查还需要）。
- **多 agent 隔离没验**：`agent-private` 的跨 agent 负向测试在 Python 侧有，
  但没在 DSH 上验过。

## 版本纪律

DSH 处于 pre-release，官方说明允许 breaking changes。

- Adapter 必须 pin 确切 tag + commit，**不能跟随 `master` 浮动**；
- 升级必须重跑完整验收，不能凭「上一版能跑」推断；
- 「在 alpha.4 验证通过」不自动代表未来 release 兼容。
