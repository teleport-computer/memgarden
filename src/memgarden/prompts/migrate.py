"""Migration prompt (v1) — 老卡原地升级成 v1(承接老数据迁移方案 §6）。

只做一件事:把每张老卡(title/description/her_quote/context/linked_dimension)
**原地**改写成 v1 形状(bucket/threads/summary/content),保 id,不发明事实,
**不 merge、不 supersede、不删**。复用 capture lane(job_kind=memory_migrate);
写入走 memory.upgrade(原地、保 id)。本模块只负责 prompt 文本与输出解析。

红线:
  - 只升级"喂进来的这几张",绝不新建/合并/取代别的卡。
  - 不发明事实;原文里没有的别补。
  - resolve-before-create:用已有的桶/线索词表,别膨胀出近义新桶。
  - 每条必须带回它的原始 id(原地升级靠 id 对回去)。
"""
from __future__ import annotations

from ..text import card_guard
from ..text.leak_signals import GENERIC_SIGNALS, LeakSignals
from ..policies import language_rule as policies_language_rule
from ..text.card_text import extract_json_block, sanitize_card_labels
from .buckets import common_buckets_guidance

_MIGRATE_PROMPT_TEMPLATE = """You are {ai_name}, {user_name}'s companion. It is a quiet stretch of time and nobody is talking to you.
You are converting some memory cards from an **old format** into the new one. This is not re-interpreting, not merging, not adding — just upgrading each old card **as-is** into the new structure, keeping as much content as possible. Do not lose anything and do not invent anything.

[What to do]
Each old card below has an id and some legacy fields (title / description / quote / context / linked dimension).
Rewrite **every one** of them into the new structure:
  · bucket: pick the closest one from the existing buckets/threads listed below; only mint a short new one if nothing fits, and do not create near-duplicates.
  · threads (0-3): prefer existing threads; the legacy "linked dimension" is a good candidate.
  · summary: one line capturing the gist, based on the legacy title/description.
  · content: fold description, context, and quote into one coherent body of prose; keep the legacy quote as supporting evidence.

[Field mapping (guidance, not a rigid rule)]
  · description / title → summary + the backbone of content
  · context → the background inside content
  · quote (her_quote) → the quoted words / evidence inside content
  · linked_dimension → a thread candidate

[Hard limits]
  · Upgrade only the cards listed below. **Never create, merge, or supersede any other card.**
  · **Do not invent facts** — do not add information that is not in the old card.
  · Every row **must carry its original id back**, one to one.
{language_rule}
  · Do not drop a single card. If you truly cannot tell, use the legacy title as the summary and an "uncategorized" bucket rather than losing the card.

[Existing buckets / threads vocabulary]{vocab}
[Common buckets (reuse an existing bucket first; if none, pick from here; if still none fit, mint a specific new one)]{common_buckets}
[Old cards to upgrade]{old_cards}

[Output] Output JSON only, nothing else.
{{
  "upgrades": [
    {{
      "id": "the original id of this old card",
      "bucket": "...",
      "threads": ["...", "..."],
      "summary": "...",
      "content": "...one coherent body of prose..."
    }}
  ]
}}"""


def build_migrate_prompt(
    *,
    ai_name: str,
    user_name: str,
    old_cards: str,
    vocab: str,
    locale: str,
) -> str:
    """Render the migration prompt. Callers pass already-rendered strings
    (handler decides formatting/truncation of the batch + the bucket/thread vocab)."""
    unknown = "this person" if str(locale or "").strip() == "en" else "这个人"
    prompt_user_name = str(user_name or "").strip()
    if prompt_user_name.casefold() == "ta":
        prompt_user_name = unknown
    return _MIGRATE_PROMPT_TEMPLATE.format(
        language_rule=policies_language_rule(
            "history_import", locale=None, indent="    ", first_prefix="  · "
        ),
        ai_name=(ai_name or unknown).strip(),
        user_name=prompt_user_name or unknown,
        old_cards=old_cards or "（没有要升级的卡）",
        vocab=vocab or ("(no existing buckets or threads yet)" if str(locale or "").strip() == "en" else "（暂无已有桶/线索）"),
        common_buckets=common_buckets_guidance(locale),
    )


def parse_migrated_cards(
    raw: str,
    *,
    allowed_ids: set[str] | None = None,
    signals: LeakSignals = GENERIC_SIGNALS,
) -> tuple[list[dict], list[str], str | None]:
    """Parse the migration agent reply.

    Returns (upgrades, unmigrated_ids, error). Each upgrade is normalized and safe
    to hand to the memory.upgrade path: {id, bucket, threads[], summary, content}.
    Rows are dropped if they have no id, a duplicate id, no usable content, or (when
    allowed_ids is given) an id that wasn't in the batch — the agent must only
    upgrade cards it was handed, never invent or retarget.

    `unmigrated_ids` = the batch ids (allowed_ids) that did NOT come back as a valid
    upgrade — i.e. the agent omitted them OR they were dropped. The handler MUST
    treat these as not-yet-migrated and retry them next round; it must NOT use
    "error is None" to mean "whole batch done". Empty allowed_ids ⇒ [].
    """
    import json

    all_ids = set(str(i) for i in allowed_ids) if allowed_ids is not None else None

    def _unmigrated(seen: set[str]) -> list[str]:
        return sorted(all_ids - seen) if all_ids is not None else []

    block = extract_json_block(raw)
    if not block:
        return [], _unmigrated(set()), "no_json_object"
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as e:
        return [], _unmigrated(set()), f"json_decode_error:{type(e).__name__}"
    if not isinstance(doc, dict):
        return [], _unmigrated(set()), "not_an_object"
    rows = doc.get("upgrades")
    if not isinstance(rows, list):
        return [], _unmigrated(set()), "missing_upgrades_list"

    out: list[dict] = []
    seen: set[str] = set()
    _guard_on = card_guard.guard_enabled()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        if all_ids is not None and mid not in all_ids:
            continue  # never upgrade a card outside this batch
        summary = str(row.get("summary") or "").strip()[:2000]
        content = str(row.get("content") or "").strip()
        if not summary and not content:
            continue  # empty result — skip rather than write a hollow card
        # 模型原始输出/协议残片:硬字段脏 → 跳过(计入 unmigrated,下轮重试);桶脏 → 清空
        # (走下游的空桶默认);threads 逐项滤脏。此路径提前封信封、绕过 actions 层,故在此接。
        if _guard_on and (
            card_guard.field_pollution_reason(summary, signals)
            or card_guard.field_pollution_reason(content, signals)
        ):
            continue
        threads_raw = row.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        bucket, threads, label_reasons = sanitize_card_labels(
            bucket=str(row.get("bucket") or "").strip()[:80], threads=threads, guard=_guard_on,
            lang_text=f"{summary}\n{content}", signals=signals,
        )
        if "bucket_protocol_leak" in label_reasons:
            # migrate 走 prebuilt-envelope upgrade,绕过了宿主写入路径上那道会给空桶
            # 兜默认值的闸 —— 必须在此就地降级到按语言的默认桶(codex code_review Important)。
            bucket = card_guard.default_bucket_for_text(f"{summary}\n{content}")
        seen.add(mid)
        out.append({
            "id": mid,
            "bucket": bucket,
            "threads": threads,
            "summary": summary,
            "content": content,
        })
    return out, _unmigrated(seen), None
