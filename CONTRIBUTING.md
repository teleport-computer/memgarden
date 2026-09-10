# Contributing / 参与开发

欢迎修复 bug、补充可复现测试、改善文档和接入示例。范围见 [README](README.md)，接口和数据语义见[数据参考](docs/INTEGRATION-AND-DATA.md)，已知边界见 [Status](docs/STATUS.md)。

## 本地验证

Python 3.10+；Adapter 离线测试需要 Node.js（CI 使用 Node 20）。

```bash
uv sync --extra dev
uv run pytest -q
uv run python scripts/check_version_consistency.py
uv run python evals/run.py --baseline evals/baseline.json
uv run python examples/mount_in_ten_minutes.py
uv run python examples/wire_capture.py
uv run python examples/retrieval_runtime.py
uv build
git diff --check
```

真实模型评测需要宿主凭据，单独按 [Evals](evals/README.md) 和 [DSH](adapters/dsh-memgarden/README.md) 执行。没有运行或 SKIP 必须明确记录；普通 PR 测试不应依赖付费服务。

## 提交可审核的 PR

- 从最新 `main` 创建独立分支，说明用户可见问题、最小复现、改动与兼容影响，不混入本机配置。
- 修 bug 先补会失败的回归；存储改动覆盖两个 Store 和真实事务边界，适配器改动实际经过插件，不仅测试假协议。
- 改字段时检查 Card、JSON Schema、解析、typed mutation、Store、导出和示例，不只验证模型输出。
- 改提示词、排序或 embedding 参数时，区分“协议/数学测试”和“实际质量证据”，记录模型、数据集、版本及未验证项。
- 运行包保持零第三方依赖；新能力或兼容性变更先与维护者对齐，不为潜在宿主扩展通用插件框架。
- 更新受影响指南、示例和 `docs/STATUS.md`，不新增另一份相互矛盾的“当前审查报告”。
- 不提交 API key、真实对话、私密记忆、数据库、outbox、cookie 或机器绝对路径。安全问题按 [SECURITY](SECURITY.md) 处理。

合并由维护者决定。普通修复不自动发版或覆盖已发布 tag；版本变更和发布按[发版指南](docs/RELEASING.md)单独审核。

Issues and PRs in English or Chinese are welcome. Include a synthetic minimal reproduction, expected versus observed behavior, package/runtime versions, and the exact checks you ran. Never attach private user data or credentials.
