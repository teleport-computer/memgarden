# garden-demo-agent

一个**从零开始、跟 io 毫无关系**的小聊天 agent，用来验证一件事：

> 陌生 Runtime 能不能十分钟接上 Memory Garden，而不需要读它的源码。

## 它是什么

一个命令行聊天机器人。你跟它说话，它记得你说过什么。

```
你 > 我不吃辣，一吃就胃疼
io > 记住了。

你 > 晚上想吃点什么
io > （想起：他不吃辣）建议避开火锅…
```

## 为什么这个 demo 有意义

它是**接入面的验收标准**，不是玩具：

```
✅ 不 import memgarden 的任何内部模块（只用顶层那几个名字）
✅ 自己提供模型和存储 —— Garden 不持有 key、不碰数据库
✅ 全部代码约 200 行，其中真正跟 Garden 打交道的不到 30 行
✅ 跑起来不需要 io、不需要 Postgres、不需要 enclave
```

只要这个 demo 还能跑，「陌生 Runtime 接得上」就是**被验证过的事实**，
而不是 README 里的一句宣称。

## 跑

```bash
export OPENROUTER_API_KEY=...        # 或 DEEPSEEK_API_KEY
python agent.py
```

不给 key 也能跑（走一个假模型），只是记忆内容是固定的：

```bash
python agent.py --fake
```

## 结构

```
agent.py       整个 agent，200 行
store.py       存储 —— 一个 JSON 文件。真实项目里换成你的数据库
model.py       模型 —— 一个 HTTP 调用。key 在这里，Garden 拿不到
memory.json    跑过之后生成的记忆
```
