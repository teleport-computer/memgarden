# DSH 多供应商真实验收收口

日期：2026-10-07

## 目标

真实验收要证明的是 MemGarden 与 DSH 的宿主合同：DSH 负责模型、凭据、重试和
Tool Loop，MemGarden 负责召回、Capture、工具写入、隔离和 Maintenance。模型供应商
不是这条合同的一部分。

之前：验收脚本和 `install-dsh` 把 provider 固定为 `deepseek-official`；DeepSeek
账号欠费时，即使环境里有其他可用凭据，也无法验证通用宿主链路。

之后：安装器可显式写入 DSH provider/model，验收脚本可显式选择 provider/model
和对应 DSH patch；默认值仍是历史基线，不改变已有用法。

## 合同与边界

- `MEMGARDEN_ACCEPTANCE_PROVIDER` 和 `MEMGARDEN_ACCEPTANCE_MODEL` 是本次验收的
  显式模型路由事实。
- `MEMGARDEN_ACCEPTANCE_DSH_PATCH` 只负责让固定版 DSH 注册对应 provider；凭据仍由
  DSH 从 provider 指定的环境引用读取，不进入 MemGarden 配置或日志。
- `install-dsh --provider/--model` 把同一份路由写给 Adapter 的 Capture/Maintenance
  调用，避免主 Agent 用一个 provider、记忆后台调用却偷偷回落到 DeepSeek。
- 替代 provider 的结果证明通用 DSH 合同，不冒充
  `deepseek-official/deepseek-v4-flash` 历史基线复测。

## 验证结果

版本：

- DSH `0.1.2-alpha.4`，官方 commit `4e84901e6471b79ec0338099867ebb4606d12bb5`
- MemGarden 基线 `main@dca691d3`
- Provider/model：`openai/gpt-5.4-mini`

自动测试：`1146 passed, 2 xfailed`。

真实 A–E：完整运行中 A–D 全部通过，E 进入 Maintenance、模型调用和账本写入成功，
但第一次没有产生 Dream/supersede 卡链，因此总结果为 `14/15`。按脚本的失败后单组
复跑规则只重跑 E，第二次 `2/2` 通过并产生 `m_11` Dream 卡，10 张 seed 卡全部归档并
指向 `m_11`。

## 未关闭项

- DeepSeek 账号推理返回 HTTP 402，历史 DeepSeek 基线本轮未复测。
- `gpt-5.4-mini` 的 E 场景出现一次模型语义波动：链路可用已经证实，但不能宣称该模型
  每次都会把这组重复卡合并。判据没有为追求绿灯而放宽。
- 本改动尚未提交、未开 PR、未合并、未发布。
