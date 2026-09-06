# 发版

打 tag 就发。CI 会先跑**发布闸**（全量测试 + 版本一致性），过了才构建、
生成构建出处凭证、发 PyPI 和 GitHub Release。

```bash
sed -i '' 's/^version = ".*"/version = "0.17.0"/' pyproject.toml
uv lock                      # 锁文件也要跟上，闸会检查
git commit -am "v0.17.0: ..." && git tag v0.17.0 && git push origin HEAD --tags
```

## 🔴 发布必须过测试闸

`release.yml` 的 `build` job `needs: gate`，gate 跑全量 pytest 和
`scripts/check_version_consistency.py`（tag / pyproject / uv.lock 三者一致）。

**这条是 2026-09-06 补的，因为出过反例**：`tests` workflow 因为还在
`cd` 一个已经删掉的目录而长期失败，而 release 不依赖它，照样把包发上了
PyPI。那不只是流程瑕疵 —— 红灯一旦变成常态，它就不再是信号，
后面真正该拦下的那次也拦不住。

## PyPI：一次性配置（还没做）

用的是 **Trusted Publishing（OIDC）**，仓库里**不存任何 token**。

> 为什么不用 API token：token 是一份长期有效的发布凭据，躺在 GitHub secrets 里。
> 泄露一次，任何人都能往这两个包名下推任意代码 —— 而下游是靠包名信任的，
> 装的时候不会去核对是谁发的。OIDC 每次签发短期凭证，作用域限定到
> 「这个仓库的这个 workflow」。

在 <https://pypi.org/manage/account/publishing/> 加一次：

| 字段 | 值 |
|---|---|
| PyPI Project Name | `memgarden` |
| Owner | `teleport-computer` |
| Repository name | `memgarden` |
| Workflow name | `release.yml` |
| **Environment name** | **`pypi-memgarden`** |

> 历史：这里曾经要配**两个**包（`memgarden` + `agent-protocol-core`）。
> 后者在 0.16.0 被移回宿主 io —— 它做的是宿主协议解析（剥模型的思维链），
> 不是记忆判断，不该让每个接 Garden 的人都吃下 io 的协议假设。
> 现在这个包**零第三方依赖**。

> 包已经发在 PyPI 上，README 里的 `pip install memgarden` 是有效的。
> （这段以前写着「在那之前 README 不许写 pip install」—— 那是首发之前的
> 状态，现在不成立了。）

## 🔴 不要给 publish 步骤加 `continue-on-error`

`continue-on-error: true` 会让 job 在步骤失败时仍判定为 success，
下游 `needs` 照样放行 —— **闸被直接架空**。

v0.12.2 就是这么翻车的：当时还有两个包，core 因为 PyPI 侧没注册而发布失败，
job 却是绿的，memgarden 照发不误，结果 PyPI 上躺了一个装不上的版本。

那个具体形状（双包依赖）已经不存在了，但**教训对现在的 gate 一样成立**：
让它红是安全的，GitHub Release 在 `build` job 里已经发完，publish 失败不影响它。

## 发版前自查

- `pytest -q` 全绿
- `python scripts/check_version_consistency.py` 通过
- 全新环境只装 wheel 能跑：`pip install --no-index --find-links dist memgarden`
  之后 `memgarden manifest` 和 `from memgarden import GardenComponent` 都要成
- 动过 Adapter 的话，`adapters/dsh-memgarden` 在 pinned DSH 上跑一遍真机验收
