# 发版

打 tag 就发。CI 会跑测试、构建两个包、生成构建出处凭证、发 PyPI 和 GitHub Release。

```bash
# 两个包的版本必须一致 —— 发布闸会检查，不一致直接失败
sed -i '' 's/^version = ".*"/version = "0.13.0"/' \
  pyproject.toml packages/agent-protocol-core/pyproject.toml
git commit -am "v0.13.0: ..." && git tag v0.13.0 && git push origin HEAD --tags
```

## PyPI：一次性配置（还没做）

用的是 **Trusted Publishing（OIDC）**，仓库里**不存任何 token**。

> 为什么不用 API token：token 是一份长期有效的发布凭据，躺在 GitHub secrets 里。
> 泄露一次，任何人都能往这两个包名下推任意代码 —— 而下游是靠包名信任的，
> 装的时候不会去核对是谁发的。OIDC 每次签发短期凭证，作用域限定到
> 「这个仓库的这个 workflow」。

在 <https://pypi.org/manage/account/publishing/> 各加一次，**两个包都要**：

| 字段 | 值 |
|---|---|
| PyPI Project Name | `memgarden`（另一次填 `agent-protocol-core`） |
| Owner | `teleport-computer` |
| Repository name | `memgarden` |
| Workflow name | `release.yml` |
| Environment name | 留空 |

配之前 PyPI 那一步会失败，但**失败方向是安全的**：发不出去，
而不是用一份错凭据发出去。GitHub Release 那步在它后面，不受影响
（`continue-on-error: true`）。

### 第一次发成功之后

改 `README.md` 的安装说明为 `pip install memgarden`，
并把 `tests/test_purity.py::test_the_readme_install_command_matches_how_we_actually_publish`
的断言改成认 PyPI。

**在那之前 README 不许写 `pip install memgarden`** —— 照着做的人第一步就失败，
而那是别人对这个项目的第一印象。这条有测试守着。

## 顺序：core 必须先发

`memgarden` 依赖 `agent-protocol-core` 的**精确版本**。反过来发的话，
PyPI 上会短暂存在一个装不上的 `memgarden`。

CI 里 `packages-dir: out/` 一次上传两个，PyPI 按依赖解析，不用管顺序；
手工发的话要注意。

## 版本锁步

两个包永远同版本号，发布闸强制。它们共享内部约定，不保证跨版本兼容 ——
所以 `memgarden` 对 core 的依赖写的是 `==`，不是 `>=`。

看起来严苛，但代价对比很清楚：多发一个版本号 vs 用户装到不匹配的组合、
在运行时才炸。
