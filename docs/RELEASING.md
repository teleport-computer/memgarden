# 发版指南

发布行为由 [release.yml](../.github/workflows/release.yml) 定义。本页说明当前流程，发布结果以对应 workflow run、Release 和 PyPI 为准。当前代码的真实环境验收及未发布修复，见 [STATUS](STATUS.md)。

## 发布前

1. 在 PR 中完成代码/文档审核，检查待发布 commit 的 CI。
2. 在 `pyproject.toml` 设置计划发布的新版本，运行 `uv lock`，检查 `uv.lock` 同步。已发布版本不可用新内容覆盖发布。
3. 运行下列验证；修改 Adapter 时还应完成 pinned DSH 的真实验收并保存结果。
4. 合并后，确认版本提交和验证证据对应，再创建匹配的 `v<版本>` tag 并推送该 tag。

```bash
uv run --extra dev pytest -q
uv run --python 3.12 --extra dev python scripts/check_version_consistency.py
uv run --extra dev python evals/run.py --baseline evals/baseline.json
uv run python examples/quickstart.py
uv run python examples/mount_in_ten_minutes.py
uv run python examples/wire_capture.py
uv run python examples/retrieval_runtime.py
uv build
```

干净 venv 中安装这次构建的**确切 wheel 文件**，再运行 `memgarden manifest` 和顶层 SDK import。不要同时安装 dist 中多个旧 wheel。模型质量评测按 [Evals](../evals/README.md) 执行；无凭据跳过不等于验证通过。

公开发布前还应检查 README 的接入示例、已知限制和 [Security](../SECURITY.md) 的报告渠道。维护者应启用 GitHub 私密漏洞报告或提供确实可用的私密渠道；提交 `SECURITY.md` 本身不会开启仓库设置。不要把公开仓库可访问、代码已合并、版本已发布和功能验收通过当作同一件事。

## 当前 workflow 实际执行什么

| 阶段 | 检查或产物 |
|---|---|
| `gate` | 全量 pytest、tag / pyproject / uv.lock 版本一致性 |
| `build` | 版本复核、pytest、quickstart、构建 wheel/sdist |
| 构建后 | 生成 provenance attestation、产物摘要，上传 GitHub Release |
| `publish-memgarden` | 下载构建产物，以 Trusted Publishing 发布 PyPI |

主 PR tests workflow 另有 Python 兼容矩阵、确定性 eval、wheel 安装等检查；release gate 当前不自动重跑其中每一项，也不运行真实 DSH 验收。发版前需要核对同一代码的这些证据，不能只凭 release gate 绿色代替全部验收。

GitHub Release 上传发生在 PyPI 发布之前，因此 PyPI 失败时可能已经存在 Release。检查完整 workflow 的发布结果，不能仅看到 Release 就宣布 PyPI 发布成功。不要给发布步骤加 `continue-on-error`。

手工 `workflow_dispatch` 重跑时指定已有 tag；不要把分支名当发布版本。对已发布 tag 的修改/重传会破坏版本可追溯性，修复应使用新版本。

## PyPI 配置与产物来源

当前 workflow 使用 OIDC Trusted Publishing，不使用长期 PyPI token。维护者应核对 PyPI 上的 Trusted Publisher 与下列配置一致；仓库里的配置无法单独证明外部账户设置仍然有效。

| 字段 | 值 |
|---|---|
| Project | `memgarden` |
| GitHub owner / repository | `teleport-computer` / `memgarden` |
| Workflow | `release.yml` |
| Environment | `pypi-memgarden` |

运行包只有 memgarden，DSH Adapter 随其 wheel 发布；没有第二个 npm 包或独立 agent-protocol-core 发布步骤。

下载和验证某次发布的 wheel：

```bash
gh release download <tag> --repo teleport-computer/memgarden --pattern '*.whl'
gh attestation verify <downloaded-wheel.whl> --repo teleport-computer/memgarden
```

attestation 绑定仓库、构建流程、commit 与产物摘要，证明产物来源；它不替代代码审核或功能验收。
