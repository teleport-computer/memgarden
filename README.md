# Memory Garden

**AI 记忆的编辑判断力。**

给它一段对话，它告诉你：这里面什么值得记、该写成几张卡、归哪个桶、
跟已有的记忆是新增还是覆盖、以及什么时候该把攒下的记忆整理一遍。

**零第三方依赖。** 全链路按明文设计 —— 要不要加密、在哪一层加，由部署环境决定。

包里有两层，按需取：

```
GardenComponent   只判断，不碰存储。想自己接管存储和编排的用这个
MountedGarden     判断 + 存储都接上：load → 判断 → 原子写回 → 回执
                  记忆归属、并发、生命周期、删除这些语义归它管
```

模型调用**始终**是宿主的事：这个包不持有 key，也不知道你用的哪个 provider。

装：

```bash
pip install memgarden
```

装完就有一个可用的 DeepSeek Harness Adapter（随包发布，不是单独的 npm 包 ——
它和这个包共用同一套 wire 协议，拆开会漂）：

```bash
memgarden install-dsh --tenant <租户> --owner <这座花园的所有者>
```

想验来源的话，每次发版的 wheel 都同时挂在 GitHub Release 上，带**构建出处凭证**：

```bash
gh release download v0.12.8 --repo teleport-computer/memgarden --pattern '*.whl'
gh attestation verify memgarden-0.12.8-py3-none-any.whl --repo teleport-computer/memgarden
```

> 每个 wheel 都由 GitHub Actions 从公开 tag 构建，PyPI 走 Trusted Publishing，
> 仓库里不存 token。凭证证明的是**「这份字节确实由那个仓库的那个 workflow 编出来」**
> —— 有仓库写权限的人换掉一个 Release 附件，验证会失败。见 `docs/RELEASING.md`。

```python
from memgarden import (GardenComponent, MountedGarden, CaptureRequest,
                       Scope)
from memgarden.stores.sqlite import SqliteStore

garden = GardenComponent(model=my_model)              # 模型你提供，key 不给它
result = garden.capture(CaptureRequest(
    window="用户：我不吃辣，一吃就胃疼",
    locale="zh-Hans",
))
my_store.apply("acme", result.mutations, owner="user-42",
               idempotency_key="turn_42", expected_revision=None)  # 落库是你的事

# 不想自己编排 load/CAS/幂等/生命周期的话，用 MountedGarden：
garden = MountedGarden(model=my_model, store=SqliteStore("memory.db"))
garden.capture_and_store(
    Scope(tenant_id="acme", memory_owner_id="user-42"),
    CaptureRequest(window="用户：我不吃辣", locale="zh-Hans"),
)                                                      # 读库、判断、原子写回
```

装完也能直接敲命令：

```bash
memgarden manifest                                     # 这东西会做什么
memgarden capture --window-file chat.txt --locale zh-Hans --model-cmd "llm -m gpt-4o"
```

完整的例子在 `examples/`：

```
examples/quickstart.py             最小可运行
examples/mount_in_ten_minutes.py   四个方法各调一次，带注释
examples/demo-agent/               ⭐ 一个真能聊天的命令行 agent，200 行
                                      跟任何宿主无关，自带模型和存储
```

```bash
cd examples/demo-agent && python agent.py --fake     # 不用 key
```

---

## 一、它跟 mem0 / Zep / Letta 不是一回事

```
它们   从对话抽事实 → 存进向量库/图 → 按相似度召回
我们   编辑判断：什么值得记、几张卡、归哪个桶、什么时候该整理
       卡片是给人看的，不是给检索用的向量
```

它们是**记忆的仓库**，这个是**记忆的编辑部**。两者可以叠着用：
让它决定写什么，再交给任何仓库去存。

判断的边界很清楚：

```
在库里          什么值得记 · 怎么归桶起线索 · 模型输出怎么校验 · 怎么去重
                这轮该想起哪几张 · 要不要整理了 · 整理时怎么合并消矛盾

不在库里        调模型 · 加解密 · 存储 · 身份装配 · 权限 · 定时器 · 审计
```

---

## 二、一分钟跑通

```bash
uv run python examples/quickstart.py
```

不联网、不需要 API key（模型那步用一段假回复代替）。五步走完：
档位 → 生成提示词 → 解析模型回答 → 存进 SQLite → 换一套字段映射接别的库。

最小代码：

```python
from memgarden.prompts.capture import build_capture_prompt, parse_capture_cards
from memgarden.stores.sqlite import SqliteStore

# 1. 库告诉你该问模型什么（指令是英文，桶名是 locale 那套）
prompt = build_capture_prompt(
    ai_name="io", user_name="老王",
    window="用户：我不吃辣，一吃就胃疼\n我：记住了",
    buckets="", threads="", identity="", cards="",
    locale="zh-Hans",          # 必填：这个花园用哪套桶名
)

# 2. 你自己调模型 —— 用什么模型、怎么调，库不管
raw = your_llm(prompt)

# 3. 库把回答解析成卡，并按档位裁剪
cards, err = parse_capture_cards(raw, policy="conversation_capture")

# 4. 存哪由你定
store = SqliteStore("memory.db")
store.apply("user_1", [{"op": "add", "card": c} for c in cards],
            # owner 是这座花园的稳定所有者。必填 —— 只用 tenant 的话，
            # 同一个账户下的两个 agent 会互相读到对方的 agent-private。
            owner="user_1", idempotency_key="turn_42")
```

---

## 三、库替你做的五个判断

### 1. 三个档位：同一套判断，三把尺子

```python
from memgarden.policies import get_policy

get_policy("conversation_capture")   # 最多 2 张厚卡    聊一晚上不该冒出 20 张碎卡
get_policy("history_import")         # 过滤一次性事件   「昨天吃火锅」丢；「我不吃辣」留
get_policy("curated_archive")        # 全留，不设上限   用户手打的 100 条一条都不能丢
```

同一份素材，导入档滤到 0 张、档案档 12 条零丢失 —— 这是同一套判断在不同尺子下的结果。

> 大多数「记忆总是记太多 / 记太碎」的抱怨，根子都在**没分场景，用了同一把尺子**。

### 2. 超额了怎么办：打回重问，不是悄悄砍掉

```python
cards, err = parse_capture_cards(raw, policy="conversation_capture")
# strict=True（默认）：模型给了 3 张 → 整批打回，err="too_many_cards"
#                       拿着这个反馈重问一次，让模型自己挑哪 2 张最值得留

cards, err = parse_capture_cards(raw, policy="conversation_capture", strict=False)
# 保底：重问之后还是给多了，留前 N 张，不让这一轮白跑
```

由模型自己挑，比我们从前两张硬切质量高得多。

### 3. 这轮该想起哪几张（挑卡）

```python
from memgarden.selection import Chain, RoleStage, RecentStage, RelevanceStage

policy = Chain(stages=(
    RoleStage("turning_point", limit=3),   # 3 张转折点
    RecentStage(limit=2),                  # 2 张最近的
    RelevanceStage(limit=3, any_score=True),  # 3 张跟当前问题相关的
))
result = policy.select(cards, query="我的狗是什么品种", limit=8)
```

**这只是一个默认组合，不是限制** —— 见第四节。

### 4. 什么时候该整理一次（做梦）

`dreaming.py` 判断攒够了没：新卡计数、快照签名、幂等键。
「多久整理一次、夜里几点跑、失败怎么退避」是宿主的调度策略，不在库里。

### 5. 卡该怎么写

`text/` 里是一组硬规则：不许留占位符、不许把协议残片写进正文、
一张卡只讲一件事、别在卡里管用户叫「用户」。

---

## 四、四个插口：能换掉的，不只是参数

设计原则：**在库里写死一个某产品专用的值，比放在产品里更糟** ——
放产品里别人看不见，写死在库里别人会继承它、还不知道为什么。

> ⚠️ **先说清楚这四个插口换的是什么。**
>
> ```
> 换得掉    存储后端、字段映射、挑卡策略、来源标签
>           —— 判断逻辑全程还是这个库的：卡还是按它的规矩写、
>              桶还是它的桶、挑卡还是它的算法
>
> 换不掉    整套记忆系统。换掉整个 Garden 属于 Runtime 侧插件接口的事，
>           不在这个库的第一阶段目标里
> ```
>
> 换句话说：**这里证明的是「Garden 可以换数据库」，不是「宿主可以换记忆组件」。**
> 第一阶段的目标是前者 + 让 Garden 能快速插进任意 Runtime（见 §二的
> `GardenComponent`），不是成为兼容所有第三方记忆系统的通用框架。

### 插口 1：存储

实现三个方法（`storage.StoragePort`）：

```python
capabilities() -> Capabilities                    # 你支持什么、不支持什么
load(tenant, *, owner, **filters) -> Snapshot     # 卡片 + 版本号（用于 CAS）
apply(tenant, mutations, *, owner, idempotency_key,
      expected_revision, maintenance_state=None) -> ApplyResult
maintenance_state(tenant, *, owner, mount) -> dict     # 整理账本（可选）
```

🔴 **`owner` 必须落到查询条件里**，不能读回整个 tenant 再由调用方过滤。
两者在正常情况下结果一样，差别只在出错时才看得见：漏一处过滤，
前者读不到、后者读得到 —— 而后者不会报错。

**四条硬要求**，缺了会被拒绝而不是降级：

```
supersede        更新记忆必须是「旧的归档 + 新的写入」，不许硬删
atomic_batch     一批要么全成、要么全不成，不许留半截
hard_delete      用户说删就真删。降级成归档 = 界面说删了、库里还在
owner_scoping    查询层能限制在一个 owner 内。降级成事后过滤 = 越权读
```

`Capabilities` **没有默认值**，每一项都要显式声明；不实现 `capabilities()`
的存储会被当作**全部不支持**（fail closed）。

其余能力缺了会**显式降级并说明代价**，不会静默变差 ——
`describe_for_user(caps)` 直接给出人话说明。

`stores/memory.py` 与 `stores/sqlite.py` 是两个参考实现，也是接口的活文档：
两者跑**同一套契约测试**（`tests/test_store_contract.py`），
六个 mutation 也走同一份执行器（`stores/_ops.py`）——
各写一份的话行为会漂，而漂了不报错。

### 插口 2：字段映射（你的卡片长得不一样）

内核只认一种形状（`summary` / `content` / `bucket` / `threads`）。
翻译归你，但套路是现成的：

```python
from memgarden.adapt import FieldMap, to_card

notion = FieldMap(
    summary_fields=("Name",),          # 可公开的摘要从哪来
    text_fields=("Name", "Notes"),     # 参与搜索的全部字段
    private_fields=("Notes",),         # 参与搜索，但绝不进摘要
)
card = to_card({"id": "n1", "Name": "养狗", "Notes": "养了一只柯基"}, notion)
```

⚠️ **`summary` 和 `search_text` 必须分开**，这是踩出来的：

```
summary       给人看的 —— 会进日志、可能返回给客户端
search_text   给机器比对的 —— 只在内部用
```

宿主 io 翻译时只给了 summary/content，老卡的标题「那次你说想学吉他」
退出了匹配范围 —— 用户问「吉他」直接召不回来。

### 插口 3：挑卡策略

不是给几个旋钮，而是**整个换掉**：

```python
class MyPolicy:
    def select(self, cards, query, *, limit):
        ...  # 完全自己写，或者用 Chain 组合内置的几段
```

三条约束（都是踩出来的）：

```
注入点在顶层        否则宿主以为换掉了「怎么查」，实际只换了一半流量
只返回 card_id      返回整张卡的话，第三方策略可以篡改或伪造候选
生命周期过滤在宿主   内核不认识你的 is_archived / 权限字段
```

分层：

```
宿主过滤 ─▶ 宿主翻译（含 search_text）─▶ SelectionPolicy ─▶ 宿主回填原卡 + 渲染 trace
                                          ▲
                                       这里是插口
```

### 插口 4：来源标签

「这张卡打哪来」是开放字符串，具体取值由你定 ——
内核不该内置某个产品的 17 个来源枚举。

---

## 五、数据边界

`observability.py` 把挑卡过程压成一条**不含任何正文**的可落库记录：

```
记      计数 · id · 拒绝理由标签 · 耗时 · 走了哪套规则
不记    摘要 · 正文 · 桶名 · 线索名 · 查询原文（只留 12 位指纹）
```

查询指纹能跨轮次对上号（统计「同一个问法反复召不回」），但复原不出原文。
`assert_content_free(record)` 是配套的守卫。

> 为什么专门做这个：宿主 io 排查「旧记忆想不起来」时，因为没有这条记录，
> 靠推断得出了一个错误结论，后来才被对照实验推翻。

---

## 六、现状与已知问题

**能用的**：全量测试绿（`pytest -q`），示例可脱离任何宿主独立跑通。
生产验证来自宿主 io（陪伴类 App），线上跑着。DeepSeek Harness 上有一个
真实跑通的 Adapter，见 `adapters/dsh-memgarden/`。

### 🔴 关于「可替换性」，必须说清的边界

```
已验证        判断逻辑可以独立发布、由外部宿主复用
              数据可以换一种保存方式（InMemory / SQLite 跑同一套契约测试，
              六个 mutation 走同一份执行器）
              同租户不同 memory owner 的隔离（两个 Store 各跑一遍负向测试）
              在 pinned DSH 0.1.2-alpha.4 上不改 core 即可挂载，
              跨 Session 自动召回、轮末自动落卡、模型工具真实注册

尚未验证      任意完整记忆系统替换掉这个库
              不同能力按插槽组合、缺某项能力时宿主仍能正确运行
              大规模数据下的表现（SQLite 参考实现会把一个 owner 的卡
              读进内存，它面向「开箱即用」，不面向超大库）
```

**尚未验证的那几条目前只是设计目标，不能称为已经实现的事实。**

### 这一版还没做的

- **SQLite 参考实现会把一个 owner 的卡整批读进内存**（每轮对话一次）。
  这是有意的取舍：它的定位是「开箱即用」，不是生产后端。实测：

  ```
     1000 张卡     5ms 每轮   峰值内存  1.4MB
    10000 张卡    40ms 每轮   峰值内存 14.3MB
    50000 张卡   270ms 每轮   峰值内存 73.4MB      ← 用户能感觉到变慢
  ```

  现实规模（一个人用一两年，几百张）完全够用。**过一万张就该换后端**，
  尤其是打算大批做历史导入的话 —— 导入三年聊天记录一次可能就是几千张。
  换的方式是自己实现 `StoragePort`（见「插口 1：存储」），
  这个抽象就是为这件事留的。
- **部分失败已有类型（`PartialFailure`），但两个官方 Store 都不会抛它**：
  它们是原子的。这个类型是给声明 `supports_atomic_batch=False` 的
  外部适配器用的，我们自己没有可以跑它的后端。

---

## 七、目录

```
src/memgarden/
  policies.py        三个档位
  prompts/           该问模型什么 + 怎么解析回答（capture / dream / migrate / buckets）
  scoring/           相关性打分
  selection.py       挑卡插口 + 三个内置段
  dreaming.py        该不该整理一次
  text/              卡片文本规范
  adapt.py           字段翻译助手
  storage.py         存储接口 + 能力声明
  stores/            memory / sqlite 两个参考实现
  observability.py   内容无关的可落库记录
  guards/            做梦的闸
```

---

## 许可

Apache-2.0
