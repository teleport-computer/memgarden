# Memory Garden

**AI 记忆的编辑判断力。**

给它一段对话，它告诉你：这里面什么值得记、该写成几张卡、归哪个桶、
跟已有的记忆是新增还是覆盖、以及什么时候该把攒下的记忆整理一遍。

它**不调模型、不碰存储、不做加密** —— 那些是宿主的事。
整个包是纯函数，零第三方依赖。

装：

```bash
pip install memgarden
```

`agent-protocol-core` 会跟着装上 —— memgarden 依赖同源的它。

想验来源的话，每次发版的 wheel 都同时挂在 GitHub Release 上，带**构建出处凭证**：

```bash
gh release download v0.12.8 --repo teleport-computer/memgarden --pattern '*.whl'
gh attestation verify memgarden-0.12.8-py3-none-any.whl --repo teleport-computer/memgarden
```

> 每个 wheel 都由 GitHub Actions 从公开 tag 构建，PyPI 走 Trusted Publishing，
> 仓库里不存 token。凭证证明的是**「这份字节确实由那个仓库的那个 workflow 编出来」**
> —— 有仓库写权限的人换掉一个 Release 附件，验证会失败。见 `docs/RELEASING.md`。

```python
from memgarden import GardenComponent, CaptureRequest

garden = GardenComponent(model=my_model)              # 模型你提供，key 不给它
result = garden.capture(CaptureRequest(
    window="用户：我不吃辣，一吃就胃疼",
    locale="zh-Hans",
))
my_store.apply(result.mutations)                       # 落库是你的事
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
            idempotency_key="turn_42")
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
capabilities() -> Capabilities        # 你支持什么、不支持什么
load(tenant, **filters) -> Snapshot   # 卡片 + 一个版本号（用于 CAS）
apply(tenant, mutations, *, idempotency_key, expected_revision) -> ApplyResult
```

**两条硬要求**，缺了会被拒绝而不是降级：

```
supersede      更新记忆必须是「旧的归档 + 新的写入」，不许硬删
atomic_batch   一批要么全成、要么全不成，不许留半截
```

其余能力缺了会**显式降级并说明代价**，不会静默变差 ——
`describe_for_user(caps)` 直接给出人话说明。

`stores/memory.py` 与 `stores/sqlite.py` 是两个参考实现，也是接口的活文档：
两者跑**同一套契约测试**（`tests/test_store_contract.py`，24 条）。

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

**能用的**：24 条契约测试绿，示例可脱离任何宿主独立跑通。
生产验证来自宿主 io（陪伴类 App），线上跑着。

### 🔴 关于「可替换性」，必须说清的边界

```
已验证        判断逻辑可以独立发布、由外部宿主复用
              数据可以换一种保存方式（InMemory / SQLite 跑同一套契约测试）

尚未验证      任意 Agent 通过统一协议快速挂载
              任意完整记忆系统替换掉这个库
              不同能力按插槽组合、缺某项能力时宿主仍能正确运行
```

**尚未验证的那几条目前只是设计目标，不能称为已经实现的事实。**

原因是具体的，不是谦虚：

```
1  ~~这个包没有顶层组件接口~~ —— 2026-08-29 已补上 ``GardenComponent``
   （capture / build_context / tools / invoke_tool / run_maintenance），
   顶层导出 16 个名字，配 CLI 和 MCP 外壳。
   宿主 io 的调用点收口仍在进行中

2  从来没有第二个真实实现接进来验证过
   两个参考存储（InMemory / SQLite）是同一个判断内核的两种保存方式，
   不是两套记忆系统

3  存储契约里有一部分动作，加密存储物理上做不到
   （比如「按元数据排序」—— 服务端只看得见密文）
   契约还没有拆成「必需 / 可选」，所以这类宿主只能返回不支持
```

补齐顺序在 `ROADMAP` 里（顶层组件接口 → Manifest → CLI → 第二个真实实现验证）。

**2026-08-23 起，全部提示词都是英文指令**：

```
✅ 指令是英文        capture / dream / migrate 的模板正文、三条「什么值得记」的
                    尺子、语言规则、称谓规则 —— 全部英文
✅ 桶名按语言取一套   locale="zh-Hans" 或 "en"，两套桶不会同时出现
✅ 举例和占位符也跟着语言走
```

英文花园的提示词里只剩两个刻意保留的 CJK **反例**（演示什么叫「双语斜杠串」、
以及「跟着素材语言走」是什么意思）—— 翻译了反而教错模型。有测试钉死这个状态。

`locale` **必填、没有默认值**。这是刻意的：默认成某种语言，等于把一套分类法
硬塞给所有使用者，而他们不会知道为什么自己的库里长出了中文桶。

> 只发一套是重点，不是省事。旧做法把两套桶都塞进去让模型挑一边，实测约 1/3
> 的中文记忆被贴上英文桶 —— 给模型一个它不该做的选择题，它就会做错。

> 指令是英文，**产出跟花园语言走**：中文对话产出中文卡、中文桶；英文对话产出英文。
> 指令语言和卡片语言是两件事。

**次要**：`selection.RelevanceStage` 的阈值是对内置打分算法校准的 ——
换了打分实现，这些数字就没有意义。

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
packages/agent-protocol-core/   聊天与记忆共用的纯标准库小包
```

---

## 许可

Apache-2.0
