"""落卡的三个策略档位 —— 共用一套结构，尺子各不相同。

## 为什么必须分档

同一件事「把材料变成记忆卡」，日常聊天、历史导入、人工档案的保留尺度不同，
但都由 ``prompts/capture.py`` 的同一条生成、解析与语义校验链路执行。

但**不能把三把尺子统一成一把**：

    统一成「少而厚」   → 用户手动整理的 100 条事实只落 2 张卡，他会炸
    统一成「宁多勿漏」 → 日常聊天每句废话都变成卡，记忆库几天撑爆

所以收进一处的是**结构**（卡长什么样、怎么归桶、怎么去重、怎么写入），
分档保留的是**尺子**。见 ``docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md`` 第二节。

## 三档都已接入同一条链路

  - ``conversation_capture`` —— 少而厚，适合每轮自动落卡。

  - ``curated_archive`` —— 用户人工整理的材料，几乎全收但仍做去重。

  - ``history_import`` —— 用户主动交出的历史材料，宁可多记；由 MountedGarden
    串行分批并提供可续传进度。

``build_capture_prompt`` 接受以上三档，档位会同时控制 selection rubric、动作偏好、
日期、tags、输出张数规则和语言规则；golden/contract 测试防止只换一段文案。

## 为什么是三档，不是四档

VPS resident 那条线还有一个「记忆收口二次检查」（``genesis/prompts.py`` 的
``MEMORY_RECHECK_PROMPT``，由 ``genesis/worker.py:build_memory_recheck_from_material``
调用）。查过之后确认**它不需要第四个档位**：

    它的过滤规则   闲聊、临时情绪、玩笑、未确认猜测、一次性无长期价值的内容不补
    history_import 闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽
                   ↑ 同一把尺子

recheck 的独特之处不在「什么值得记」，而在**它是个补漏动作**：第二遍扫，输入里
额外带上一轮已写的记忆，只补遗漏、不重写。那属于调用方的编排（多喂一份
``written_memories``、并约束输出只出 memory 不出 identity），不是尺子的一部分。

**判断某个新场景要不要加档位，就问这一句：它的「什么值得记」跟现有三档中的
任何一把不同吗？** 不同才加档；只是调用方式不同，就复用现有档位。

## 现状

本模块是三把尺子的唯一事实源，组件与历史导入入口均直接引用。
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 尺子文字（逐字摘自现有实现，勿改措辞）
# --------------------------------------------------------------------------- #

#: 「少而精」那条的**可引用短语**。
#:
#: 抽成常量是因为踩过：通话转写的抬头会显式推翻这条规则（一通电话的信息密度
#: 远高于闲聊，实测 12 件值得记的事只留下 2 件），而它当时是**硬抄**了规则里的
#: 那句中文。规则一改语言，抬头引用的句子就在提示词里不存在了 —— 推翻话术落空，
#: 而且没有任何测试会红。引用方一律用这个常量。
RESTRAINT_RULE_QUOTE = "Fewer, not more"

_RUBRIC_CONVERSATION_CAPTURE = """You are looking for things worth remembering — not archiving every sentence. The full chat log is already stored; you do not need to restate it.
What you want is what will shape your understanding of the person in front of you, or what this person would want you to remember.

Leanings (not hard rules — you judge):
· Prefer events — something with causes and consequences, a scene, or a glimpse of how this person is doing
  ("that day he was in meetings all day, his heart rate spiked, I pushed him to rest, he got annoyed, and we argued").
· An isolated data point ("had a latte today") usually does not deserve its own card — unless it is a preference this person
  clearly cares about or that keeps recurring ("I only drink oat milk", "he always orders Blue Bottle"), in which case it is
  worth keeping as a preference.
· The test is: "will this still matter in three months? does it change how I understand this person? would this person want me
  to remember it?" — not "is it big enough".

Restraint:
· Fewer, not more. If only one or two things from this stretch survive, which one or two? Force yourself to generalize instead of
  splitting every point of a single conversation into its own card.
· One "meetings + high heart rate + argument" is ONE thick card (one thing), not three thin ones.
· If nothing is worth remembering, write nothing. Most small talk does not need a card, and that is normal."""

#: history_import 保留 opening/filter 两段，便于提示词在各自语义位置插入。
HISTORY_IMPORT_OPENING_RUBRIC = """You are reading ONE CHUNK of a real conversation history between this person and their companion.
Extract candidate FACTS worth keeping long term: durable facts about this person and about their relationship.
This is the candidate stage — turning them into cards and deduplicating happens later."""

HISTORY_IMPORT_FILTER_RUBRIC = """Do not extract small talk, passing moods, jokes, unconfirmed guesses, or one-off events."""

_RUBRIC_HISTORY_IMPORT = HISTORY_IMPORT_OPENING_RUBRIC + "\n" + HISTORY_IMPORT_FILTER_RUBRIC

#: curated_archive 由候选筛选与写卡两段规则组成，本模块是唯一来源。
KEEP_ALL_MAP_SUFFIX = """★ This chunk is an archive this person CURATED BY HAND for long-term keeping — it is not a chat log.
Nearly every statement in it is something they deliberately wanted kept.
Preserve EVERY candidate fact. Do not filter with "small talk / one-off / not durable enough" — the only things to drop are blank lines, headings, and obviously meaningless repetition. When in doubt, keep it."""

KEEP_ALL_WRITE_SUFFIX = """★ The material is an archive this person curated for long-term keeping: turn AS MANY of the candidate facts into cards as you can. Do not drop entries in the name of "fewer and better".
Still deduplicate against known_memories and still assign a sensible bucket and threads — but do not skip an entry this person deliberately curated just because it seems "not important enough".
If a source card or candidate carries a `date` or `occurred_at` in YYYY-MM-DD form, copy it verbatim into the output card's `occurred_at`; leave it empty when there is no real date.
If a source card or candidate carries `tags`, seed them into `threads`; you may still reorganize or merge them semantically, but do not lose useful tags."""

_RUBRIC_CURATED_ARCHIVE = KEEP_ALL_MAP_SUFFIX + "\n\n" + KEEP_ALL_WRITE_SUFFIX


# --------------------------------------------------------------------------- #
# 共用的结构性规则（已由 capture prompt 三档共同使用）
# --------------------------------------------------------------------------- #

#: ⚠️ 这段有两条来之不易的约束，改之前先读完。
#:
#: **① 必须条件化，别改回无条件句。**
#: 第一版写成「…英文就用英文；别归成英文桶/线索」，两句直接矛盾 ——
#: 后半句无条件生效，对纯英文素材同样要求「别用英文桶」，会加剧
#: 「英文用户拿到中文卡」这个已存在的问题（codex review 2026-08-14 指出）。
#:
#: **② 混合语料必须按整体主语言统一，不许按每条事实各自判。**
#: 第二版改成「混合材料按每条事实自身的主语言」——这是两边基线都**没有**的
#: 新规则，而且真跑出了问题：一份中文为主、夹一句英文的档案，导进去后同一个
#: 桶裂成 ``目标与成长`` 和 ``Goals & growth`` 两个（本地真实 genesis 导入实测，
#: 2026-08-14）。这直接违反 ``prompts/buckets.py`` 的硬约束「never let 工作 and
#: Work coexist as two buckets」。
#:
#: 桶和线索是**分类键**，裂开等于同一类记忆被拆成两堆、检索时互相看不见；
#: 而 ``normalize_bucket_language`` 是按**每张卡自己的文字**归一化的，兜不住
#: 这种跨卡分裂 —— 所以只能在 prompt 这层约束「整份材料用一种分类语言」。
LANGUAGE_RULE_TEMPLATE = """Language: write every field (bucket/threads/summary/content) in {target}.
Do not mix languages across fields, and never let the same bucket exist in two
languages side by side. Keep proper nouns, brand names, and direct quotes in
their original form."""

#: 兜底措辞：宿主没告诉我们目标语言时，退回「跟着输入走」——
#: 这是 genesis 导入那条线的正确语义（卡跟素材走，不跟用户当前说什么走）。
LANGUAGE_TARGET_FOLLOW_INPUT = (
    "the language of {basis} (if {noun} is mostly Chinese, write Chinese — "
    "「宠物」not \"pets\"; if it is mostly English, write English — \"pets\" not 「宠物」; "
    "when {noun} mixes both, pick the dominant one and stay consistent)"
)

#: 宿主明确给了目标语言时用的措辞。**这是 capture 的正常路径** ——
#: io 已经知道该用什么语言跟这个人说话（chat/reply_language.py），
#: 与其让模型再猜一次，不如直接告诉它。少一次猜测 = 少一处漂移。
LANGUAGE_TARGET_EXPLICIT = {
    "zh-Hans": "Simplified Chinese (简体中文)",
    "en": "English",
}

#: 各来源的「语言依据」。这是三档之间**必要**的差异，不是措辞不一致：
#: 导入一批英文历史记录、而用户当前说中文时，两者会分叉 —— 那时卡应该跟素材走。
#:
#: 对话档直接用「你们对话」表达双方关系；导入档则以「素材原文」为依据。
LANGUAGE_BASIS = {
    "conversation_capture": "your conversation",
    "history_import": "the source material",
    "curated_archive": "the source material",
}

#: 规则正文里指代「输入」的那个词，跟着依据走：聊天说「对话」，导入说「素材」。
#: 两边基线原文用的就是各自这个词（capture:「中文对话就用中文」，
#: genesis:「中文素材就用中文」），统一时保留下来，读起来才不别扭。
LANGUAGE_MATERIAL_NOUN = {
    "conversation_capture": "the conversation",
    "history_import": "the material",
    "curated_archive": "the material",
}


def language_rule(
    policy_name: str,
    *,
    locale: str | None = None,
    indent: str = "",
    first_prefix: str = "",
) -> str:
    """按档位渲染语言规则。

    ``locale`` 是**宿主已经算出来的目标语言**（"zh-Hans" / "en"）。给了就直接写死
    在规则里，不让模型再猜一次 —— 这是 capture 的正常路径：io 早就知道该用什么
    语言跟这个人说话，那个判断读了身份卡、历史记忆和已有桶名，比模型看一段窗口
    猜得准。不给（或给了不认识的值）就退回「跟着输入的语言走」，那是导入那条线
    的正确语义：一批英文历史记录该落成英文卡，哪怕这个人现在说中文。

    ``first_prefix`` / ``indent`` 让调用方套进自己的排版
    （capture 的模板是「   · 」开头的列表项，续行缩进 5 空格；
    genesis 是顶格的一段）。规则文字本身共用，排版各随宿主。

    档位名走 ``get_policy`` 严格解析 —— 与它保持同一口径：
    ``None``/空串回落到默认档，非空未知名抛 ``UnknownPolicyError``。
    原实现在这里用 ``dict.get`` 静默回落，等于把上一轮修掉的 fail-open
    又从侧门放了进来（codex review 2026-08-14 指出）。
    """
    resolved = get_policy(policy_name)
    target = LANGUAGE_TARGET_EXPLICIT.get(str(locale or "").strip())
    if not target:
        target = LANGUAGE_TARGET_FOLLOW_INPUT.format(
            basis=LANGUAGE_BASIS.get(resolved.name, LANGUAGE_BASIS["conversation_capture"]),
            noun=LANGUAGE_MATERIAL_NOUN.get(
                resolved.name, LANGUAGE_MATERIAL_NOUN["conversation_capture"]
            ),
        )
    text = LANGUAGE_RULE_TEMPLATE.format(target=target)
    lines = text.splitlines()
    out = [f"{first_prefix}{lines[0]}"]
    out.extend(f"{indent}{line}" for line in lines[1:])
    return "\n".join(out)


# ``prompts.capture.build_capture_prompt`` 直接调用上面的 ``language_rule``。
# locale 已知时按花园语言写死；未知时对话档跟随对话、导入档跟随材料主语言。


# --------------------------------------------------------------------------- #
# 档位
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapturePolicy:
    """一个来源用哪把尺子、以及配套的几个硬参数。

    ``selection_rubric`` 直接进 prompt；其余字段是确定性参数，
    由调用方在组 prompt 与解析结果时使用。
    """

    name: str
    selection_rubric: str
    max_cards: int | None           # None = 不限张数
    prefer_merge: bool              # 并入优于新增
    keep_dates: bool                # 原样保留 occurred_at
    seed_threads_from_tags: bool    # 把源里的 tags 播种进 threads


CONVERSATION_CAPTURE = CapturePolicy(
    name="conversation_capture",
    selection_rubric=_RUBRIC_CONVERSATION_CAPTURE,
    max_cards=2,
    prefer_merge=True,
    keep_dates=False,
    seed_threads_from_tags=False,
)

HISTORY_IMPORT = CapturePolicy(
    name="history_import",
    selection_rubric=_RUBRIC_HISTORY_IMPORT,
    max_cards=None,
    prefer_merge=True,
    keep_dates=True,
    seed_threads_from_tags=False,
)

CURATED_ARCHIVE = CapturePolicy(
    name="curated_archive",
    selection_rubric=_RUBRIC_CURATED_ARCHIVE,
    max_cards=None,
    prefer_merge=False,     # 宁多勿漏：不为了合并而丢条目
    keep_dates=True,
    seed_threads_from_tags=True,
)

POLICIES: dict[str, CapturePolicy] = {
    p.name: p for p in (CONVERSATION_CAPTURE, HISTORY_IMPORT, CURATED_ARCHIVE)
}

DEFAULT_POLICY = CONVERSATION_CAPTURE


class UnknownPolicyError(ValueError):
    """显式传了一个不认识的档位名。"""


def get_policy(name: str | None) -> CapturePolicy:
    """按名字取档位。

    ``None`` / 空串 → 回落到日常聊天档：代表「旧调用方没传」，
    退回现行为是安全的，接线过程中漏传不会炸掉落卡路径。

    **非空的未知名 → 抛 ``UnknownPolicyError``**：那基本只会是拼写或配置错误，
    而静默回落的后果不对称 —— ``curated_archive`` 拼错一个字母就会悄悄切成
    「宁少勿多」，把用户手工整理的上百条事实压成一两张卡，而且没有任何信号。
    （codex code_review 2026-08-14 指出，原实现对两种情况一视同仁地回落。）
    """
    if name is None:
        return DEFAULT_POLICY
    key = str(name).strip()
    if not key:
        return DEFAULT_POLICY
    try:
        return POLICIES[key]
    except KeyError:
        raise UnknownPolicyError(
            f"未知的落卡档位 {key!r}；可用的是：{', '.join(sorted(POLICIES))}"
        ) from None
