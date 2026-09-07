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

它演示 `GardenComponent` 的最小接入，不代表完整持久化链路验收：

```
✅ 使用顶层组件与公开的 selection 策略插口
✅ 自己提供模型和存储 —— 此处的 GardenComponent 不碰数据库
✅ 跑起来不需要 io、不需要 Postgres、不需要 enclave
```

这里的 JsonStore 只实现演示所需的 add/supersede，直接重写 JSON 文件；
没有完整 StoragePort、owner 隔离、CAS、幂等或卡片与整理账本的原子提交。
演示每两轮 Capture 一次，退出前未满两轮的对话也不会自动补落。
生产接入应参考 [MountedGarden 示例](../mount_in_ten_minutes.py) 和
[接入与数据参考](../../docs/INTEGRATION-AND-DATA.md)，不要将本示例的存储复制为生产实现。

## 跑

```bash
export OPENROUTER_API_KEY=...        # 或 DEEPSEEK_API_KEY
python agent.py
```

无 key 时显式加 `--fake`（假模型的记忆内容固定）：

```bash
python agent.py --fake
```

## 结构

```
agent.py       命令行对话和组件接线
store.py       存储 —— 一个 JSON 文件。真实项目里换成你的数据库
model.py       模型 —— 一个 HTTP 调用。key 在这里，Garden 拿不到
memory.json    跑过之后生成的记忆
```
