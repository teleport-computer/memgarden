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

在 <https://pypi.org/manage/account/publishing/> 各加一次，**两个包都要**，
注意 Environment name **两条不一样**：

| 字段 | memgarden | agent-protocol-core |
|---|---|---|
| PyPI Project Name | `memgarden` | `agent-protocol-core` |
| Owner | `teleport-computer` | `teleport-computer` |
| Repository name | `memgarden` | `memgarden` |
| Workflow name | `release.yml` | `release.yml` |
| **Environment name** | **`pypi-memgarden`** | **`pypi-core`** |

### ⚠️ Environment 不能留空，两条也不能一样

PyPI 的 Trusted Publisher 按「owner + 仓库 + workflow + environment」匹配。
两个包在**同一个仓库、同一个 workflow**里 —— 都不填 environment 的话，
两条配置除了项目名完全一样，PyPI 判为歧义，直接拒绝注册第二条：

```
A pending trusted publisher matching this configuration has already been
registered for a different project name.
```

这就是为什么 `release.yml` 把 PyPI 发布拆成了 `publish-core` 和
`publish-memgarden` 两个 job，各带一个 environment。改 environment 名字的话
两边要一起改，否则匹配不上（表现是 `invalid-publisher`）。

顺带解决了发布顺序：`publish-memgarden` 的 `needs` 指向 `publish-core`，
core 一定先上 PyPI —— memgarden 依赖它的精确版本，反过来的话
PyPI 上会短暂存在一个装不上的 memgarden。

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
