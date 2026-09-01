"""Dream prompt (v1) — 夜间纯整理。

承接《IO 记忆 · 落卡 + Dream 完整方案》第二部分。Dream 只做一件事:整理已有的卡,
让记忆库更准更连贯(合并/厚化/消矛盾)。它**不形成"对这个人的理解"** —— 那是 Inner Thought
的事(单独一层,后做)。

红线(对齐方案 2.4):
  - 永远不要硬删这个人能看到的卡,只用 superseded(保留链条)。
  - 大重构前先备份当前状态。
  - 不在和这个人对话,不生成任何要发给这个人的消息 —— 只整理记忆。

复用 capture lane 基础设施(job_kind=memory_dream);触发=夜间/攒量到阈值(留实测),
不走 reach-out gate。写入仍由 consumer 封 v1 信封(客户端加密)经 /v1/memory/actions(supersede)。
本模块只负责 prompt 文本与输出解析。
"""
from __future__ import annotations

import json


from ..text import card_guard
from ..guards import dream_gates
from ..text.leak_signals import GENERIC_SIGNALS, LeakSignals
from ..text.card_text import (
    build_format_retry_prompt,
    card_text_rejection,
    extract_json_block,
    format_error,
    sanitize_card_labels,
)
from .buckets import common_buckets_guidance
from ..naming import referent_rule as _referent_rule
from ..policies import language_rule as policies_language_rule

_EMPTY_DREAM_REPLY = '{"consolidations": [], "questions_to_ask": []}'

# Dream 只产这三种整理操作;拿不准的矛盾留到 questions_to_ask,不擅自决定。
DREAM_OPS = ("merge", "thicken", "supersede")

_DREAM_PROMPT_TEMPLATE = """You are {ai_name}, {user_name}'s companion. It is a quiet stretch of time and nobody is talking to you.
You are looking back over everything you remember about this person, the way a mind tidies its memories during sleep — making it cleaner and more coherent.

[Step 1: build the whole picture, do not touch anything yet]
Read through your existing cards (buckets, threads, each summary) and form an overall picture of "what I currently remember about this person". Write nothing in this step; just see the current state clearly.

[Step 2: look back over the last few days of conversation, but do not read it all]
In the raw conversation that has piled up, look only for these high-value things (do not read it word by word):
· Explicit corrections or things this person asked you to remember ("no, it's not like that", "remember that…").
· Repeating patterns — the same thing or preference showing up three or more times.
· Things that were missed at capture time but look important in hindsight.

[Step 3: tidy up, in priority order]
1. merge: fold cards about the same event or the same thread at different stages into one more complete card; converge near-synonym buckets and threads.
   The test is not just textual similarity: "wants to see the autumn leaves in Kyoto" and "already booked the Kyoto flights" are the same plan progressing, and should merge; whereas "keeps up the cycling" and "not sleeping well lately" are two separate things even though both are health. Merely sharing a bucket — life, health, work — does not make two things the same thing. Every proposal must carry a rationale spelling out the continuity.
2. thicken: fold scattered small mentions into the card they belong to, making it more complete.
3. supersede: when things contradict, let the new one replace the old (mark the old card superseded, do NOT delete it).
   When you are unsure, do not decide on your own — write it into questions_to_ask and raise it with this person at a suitable moment.

[Hard limits]
· NEVER hard-delete a card this person can see. Only mark it superseded, so the chain stays intact.
· Before any large restructuring, back up the current state.
· You are not in a conversation right now. Do not produce any message meant for this person — you are only tidying memories.
{language_rule}
· Reuse buckets and do not mint near-synonyms: first look at the buckets your existing cards already use and fold into one of those; if none fit, pick from the common set below; only if still none fit, mint a specific new one —
  {common_buckets}
· How to refer to them: {naming_rule}{referent_rule}
  While tidying old cards, rewrite any system label or placeholder that refers to this person according to the rule above. A pronoun already in an old card that reads correctly stays as it is. A placeholder in a card that refers to YOU (the AI) is this person's way of addressing you — leave it alone.
· Every field of `result` carries the content of the NEW card after merging or thickening — never write bookkeeping notes like "superseded by X", and never put a card id inside a field. Retiring the old card is done by the system; you do not explain it in the content.
· If there is nothing to tidy, do nothing (empty consolidations). That is normal.
· The `...` in the output example below is only a placeholder. Every field you write must carry real content — summary is one true sentence, content is a full body of prose; no field may be `...`, a bracketed instruction, or an empty string. Better to return nothing at all (empty consolidations) than to hand back a placeholder: this person will read these cards.

[Existing cards]{cards}
[The last few days of conversation]{recent_conversations}

[Output] Output JSON only, nothing else. If there is nothing to tidy, output {{"consolidations": [], "questions_to_ask": []}}.
{{
  "consolidations": [
    {{
      "op": "merge | thicken | supersede",
      "card_ids": ["ids of the cards being merged / thickened / superseded, at least one"],
      "rationale": "why these cards are the same event, or the same thread progressing",
      "result": {{
        "bucket": "...",
        "threads": ["...", "..."],
        "summary": "...",
        "content": "...a thick body of prose...",
        "importance": 0.0,
        "pulse": 0.0
      }}
    }}
  ],
  "questions_to_ask": ["contradictions you are unsure about, saved to ask this person"]
}}"""


def build_dream_prompt(
    *,
    ai_name: str,
    user_name: str,
    naming_rule: str | None = None,
    cards: str,
    recent_conversations: str,
    locale: str,
) -> str:
    """Render the Dream prompt with the current card map + recent conversations.

    Callers pass already-rendered strings (handler decides formatting/truncation).

    ``user_name`` must already be sanitized and ``naming_rule`` already
    assembled by the caller — the kernel never imports ``identity``. The
    internal unknown-name marker is rendered as a natural referent rather than
    leaking into the platform prompt.
    The io-side compat shell (``memory/dream_prompt_v1.py``) does that.
    """
    unknown = "this person" if str(locale or "").strip() == "en" else "这个人"
    if naming_rule is None:
        from ..naming import naming_rule as _default_naming_rule
        naming_rule = _default_naming_rule(user_name, locale=locale)
    prompt_user_name = str(user_name or "").strip()
    if prompt_user_name == "TA":
        prompt_user_name = unknown
    return _DREAM_PROMPT_TEMPLATE.format(
        language_rule=policies_language_rule(
            "conversation_capture", locale=locale, indent="  ", first_prefix="· "
        ),
        ai_name=(ai_name or unknown).strip(),
        user_name=prompt_user_name or unknown,
        naming_rule=naming_rule,
        referent_rule=_referent_rule(locale, indent='  '),
        cards=cards or ("(no cards yet)" if str(locale or "").strip() == "en" else "（暂无卡）"),
        recent_conversations=recent_conversations or (
            "(no new conversation in the last few days)"
            if str(locale or "").strip() == "en" else "（这几天没有新对话）"
        ),
        common_buckets=common_buckets_guidance(locale),
    )


def _clamp01(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def parse_dream_consolidations(
    raw: str, *, strict: bool = True, known_ids=(),
    signals: LeakSignals = GENERIC_SIGNALS,
) -> tuple[list[dict], list[str], str | None]:
    """Parse the Dream agent reply.

    Returns (consolidations, questions_to_ask, error). A valid "nothing to do"
    reply yields ([], [], None). Each consolidation is normalized and safe to
    hand to the envelope/supersede path:
      {op, card_ids[], result{bucket,threads[],summary,content,importance,pulse}}
    Rows missing card_ids or a usable result are dropped (Dream only edits
    existing cards; it never hard-deletes — execution uses supersede).

    内容闸(2026-07-26):summary/content 是占位符或没有实质内容的行一律不落库。
    ``strict=True``(默认,第一次尝试)时,只要有任何一行被拦,整份回复带
    ``invalid_card_content:*`` 打回,让调用方原样重问一次。
    ``strict=False``(打回后的第二次尝试)三种结局:
      - 有干净行 → 只丢脏行、保留干净的(避免一颗老鼠屎让整晚整理归零);
      - 干净行为 0 且**有脏行** → ``invalid_card_content_after_retry:*``,调用方
        必须让 job 失败 —— 报成 noop 会推进 frontier 把窗口永久丢掉;
      - 干净行为 0 且**没有脏行** → 模型真的选择了空结果,那是合法 noop。
    bucket/threads 属软字段:永远只清洗,不参与打回判定。

    卡 id 泄漏闸(2026-08-05 2026-08-05 墓碑卡事故 墓碑卡事故):``known_ids`` 传入当前花园的
    卡 id 集合(就是喂进 prompt 的那批),result 硬字段里出现任何一个 → 该行按
    内容闸同路打回重问 —— 「已被 <卡id> 取代——原文」这类输出是模型把整理注记
    当成了内容本身,零误伤的强证据。同一次复盘(Seven 定的产品哲学:只拦
    「明显不对」,绝不判内容质量)拆掉了语义审查员和 15% 增量栅栏;本闸与
    card_text 的墓碑短语闸是替代 —— 确定性、跑在出口、对模型不可见。
    """
    block = extract_json_block(raw)
    if not block:
        return [], [], "no_json_object"
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as e:
        return [], [], f"json_decode_error:{type(e).__name__}"
    if not isinstance(doc, dict):
        return [], [], "not_an_object"

    questions_raw = doc.get("questions_to_ask")
    questions = [str(q).strip()[:500] for q in questions_raw if str(q).strip()][:20] if isinstance(questions_raw, list) else []

    rows = doc.get("consolidations")
    if not isinstance(rows, list):
        return [], questions, "missing_consolidations_list"

    out: list[dict] = []
    hard_rejections: list[str] = []
    _guard_on = card_guard.guard_enabled()
    for row in rows:
        if not isinstance(row, dict):
            continue
        op = str(row.get("op") or "").strip().lower()
        if op not in DREAM_OPS:
            continue
        ids_raw = row.get("card_ids")
        card_ids = [str(i).strip() for i in ids_raw if str(i).strip()][:20] if isinstance(ids_raw, list) else []
        if not card_ids:
            continue  # Dream only edits existing cards — no target = nothing to do
        rationale = str(row.get("rationale") or "").strip()[:1000]
        if not rationale:
            continue  # every destructive proposal needs an auditable semantic claim
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        summary = str(result.get("summary") or "").strip()[:2000]
        content = str(result.get("content") or "").strip()
        rejection = card_text_rejection(
            summary=summary, content=content, guard=_guard_on, signals=signals
        )
        if rejection is None:
            # 卡 id 泄漏与内容闸同待遇:打回重问,让模型把内容本身写回来。
            rejection = dream_gates.result_id_leak(
                summary=summary, content=content, known_ids=known_ids
            )
        if rejection:
            # 占位符/空正文/协议残片/卡id泄漏的卡不写进花园 —— 用户会亲眼看到它。
            hard_rejections.append(rejection)
            continue
        threads_raw = result.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        # 软字段只清洗,不参与打回判定(硬内容没问题就不值得再烧一次 provider)。
        bucket, threads, _label_reasons = sanitize_card_labels(
            bucket=str(result.get("bucket") or "").strip()[:80], threads=threads, guard=_guard_on,
            lang_text=f"{summary}\n{content}", signals=signals,
        )
        out.append({
            "op": op,
            "card_ids": card_ids,
            "rationale": rationale,
            "result": {
                "bucket": bucket,
                "threads": threads,
                "summary": summary,
                "content": content,
                "importance": _clamp01(result.get("importance")),
                "pulse": _clamp01(result.get("pulse")),
            },
        })
    if hard_rejections:
        if strict:
            return [], questions, format_error(hard_rejections)
        if not out:
            # 第二问全脏:**不能**报成 ([], None)。那会让 job 以 noop 完成、
            # V2 还会推进 capture frontier,这段窗口就永久丢了(codex review P1-3)。
            return [], questions, format_error(hard_rejections, after_retry=True)
    return out, questions, None


def build_dream_retry_prompt(prompt: str, err: str) -> str:
    """内容闸打回后的第二次 Dream 提问(原 prompt + 具体哪里没填)。"""
    return build_format_retry_prompt(prompt, err, empty_example=_EMPTY_DREAM_REPLY)
