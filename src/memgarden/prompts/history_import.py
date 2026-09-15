"""历史导入的两段式（two_pass）提示词：先抽候选事实，再统一写卡。

## 为什么有第二种形状

单段式（``single_pass``，默认）是「每批直接写卡」：第 N 批写卡时，前 N-1 批
写进去的卡在它的「已有记忆索引」里，模型据此选 merge 而不是 add。

两段式把一次导入拆成两类调用：

    抽候选   每批只抽「值得长期记住的事实」+ 一句原话证据，输出很短
    写卡     所有批都抽完之后，把候选分组交给写卡提示词：去重、归桶、写厚正文

这是宿主 io 的 genesis 导入一直在跑的形状（fact_map → fact_write）。它的好处是
写卡那一步**一次看到跨很多批的候选**：同一件事在第 3 批和第 40 批各被提了一次，
写卡时就在同一张清单里，桶名也在同一次调用里收敛；抽候选的输出又短，
截断风险低。代价是多一类调用，而且候选要存进进度（含用户内容，宿主要按记忆
正文的等级保存）。

哪个默认更好是产品问题，要真模型对比才能定 —— 两种形状都走同一套解析、
内容闸、语义重问和写入指令，差别只在「谁在什么时候看见什么」。

## 写卡那一步复用 Capture 提示词

写卡阶段不另起一套模板：它就是 ``build_capture_prompt``，只是窗口里放的是
候选清单而不是原文。另起一套的话，卡长什么样、怎么归桶、怎么拒占位符会在
两份模板之间慢慢漂开，而这种漂移只有真模型上才看得见。
"""
from __future__ import annotations

import json
import re

from ..naming import naming_rule as _default_naming_rule
from ..policies import (
    HISTORY_IMPORT_FILTER_RUBRIC,
    HISTORY_IMPORT_OPENING_RUBRIC,
    KEEP_ALL_MAP_SUFFIX,
    CapturePolicy,
    get_policy,
)
from ..policies import language_rule as _language_rule
from ..text import card_guard
from ..text.card_text import (
    MIN_SUMMARY_CHARS,
    build_format_retry_prompt,
    extract_json_block,
    format_error,
    placeholder_reason,
    quote_repair_candidates,
    repair_unescaped_quotes,
    substantive_len,
)
from ..text.leak_signals import GENERIC_SIGNALS, LeakSignals
from ..timestamps import normalize as _normalize_ts

_EMPTY_CANDIDATES_REPLY = '{"candidates": []}'

#: 单条候选的长度上限。候选只是「指针 + 证据」，正文在写卡那步才写厚；
#: 放宽了会让进度对象随导入规模线性膨胀。
CANDIDATE_SUMMARY_CHARS = 300
CANDIDATE_EVIDENCE_CHARS = 200

_ABOUT_VALUES = ("person", "relationship")

_CANDIDATES_PROMPT_TEMPLATE = """{framing}

{opening}
{filter}

[Rules]
· One candidate = one durable fact. Do not bundle unrelated facts into one line.
· summary: the fact in one plain line.
· evidence: a SHORT verbatim quote from the material that supports it (copy, do not paraphrase).
· about: "person" for a fact about this person; "relationship" for a fact about the two of you.
· occurred_at: when it happened, copied from the material (ISO date), or null. Do not guess a date.
· A profile, or what they say about themselves, is a fact ABOUT THE PERSON — never turn it into your own personality.
· How to refer to them: {naming_rule}
· {language_rule}
· Use only what the material actually says. If this stretch is thin, return fewer or none.
{time_hint}
[{window_label}]
{window}

[Output] Output JSON only, nothing else. If there is nothing, output {{"candidates": []}}.
{{
  "candidates": [
    {{"about": "person | relationship", "summary": "...", "evidence": "...", "occurred_at": "YYYY-MM-DD or null"}}
  ]
}}"""


def _opening(policy: CapturePolicy) -> tuple[str, str]:
    if policy.name == "curated_archive":
        return HISTORY_IMPORT_OPENING_RUBRIC, KEEP_ALL_MAP_SUFFIX
    return HISTORY_IMPORT_OPENING_RUBRIC, HISTORY_IMPORT_FILTER_RUBRIC


def build_import_candidates_prompt(
    *,
    window: str,
    locale: str,
    policy: CapturePolicy | str | None = "history_import",
    ai_name: str = "",
    user_name: str = "",
    naming_rule: str | None = None,
    material_kind: str = "",
    time_hint: str = "",
) -> str:
    """两段式第一段：这一批材料里有哪些候选事实。"""
    resolved = policy if isinstance(policy, CapturePolicy) else get_policy(
        policy or "history_import")
    unknown = "this person" if str(locale or "").strip() == "en" else "这个人"
    ai = (ai_name or unknown).strip()
    person = str(user_name or "").strip()
    if not person or person == "TA":
        person = unknown
    if naming_rule is None:
        naming_rule = _default_naming_rule(user_name, locale=locale)
    kind = (f" The source describes itself as: {material_kind}."
            if str(material_kind or "").strip() else "")
    framing = (f"You are {ai}, {person}'s companion. {person} has handed you "
               f"material from their past.{kind}")
    opening, filt = _opening(resolved)
    hint = f"\n[When this stretch is from] {time_hint.strip()}\n" if str(
        time_hint or "").strip() else ""
    return _CANDIDATES_PROMPT_TEMPLATE.format(
        framing=framing,
        opening=opening,
        filter=filt,
        naming_rule=naming_rule,
        language_rule=_language_rule(resolved.name, locale=locale),
        time_hint=hint,
        window_label="The material",
        window=window or "（空）",
    )


def build_import_candidates_retry_prompt(prompt: str, err: str) -> str:
    return build_format_retry_prompt(prompt, err, empty_example=_EMPTY_CANDIDATES_REPLY)


_UNPARSED = object()


def parse_import_candidates(
    raw: str,
    *,
    strict: bool = True,
    signals: LeakSignals = GENERIC_SIGNALS,
) -> tuple[list[dict], str | None]:
    """解析候选。返回 ``(candidates, error)``；合法的空清单是 ``([], None)``。

    和落卡同口径：占位符/协议残片的候选 ``strict`` 时整份打回重问，重问后
    有干净的就只丢脏的；**一条干净的都没有且确实有脏的 → 报错**，不能伪装成
    「这一批没什么可记」让游标推过去。
    """
    block = extract_json_block(raw)
    if not block:
        return [], "no_json_object"
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as first:
        doc = _UNPARSED
        for candidate in quote_repair_candidates(raw):
            try:
                doc = json.loads(repair_unescaped_quotes(candidate))
                break
            except (ValueError, TypeError):
                continue
        if doc is _UNPARSED:
            return [], f"json_decode_error:{type(first).__name__}"
    if not isinstance(doc, dict):
        return [], "not_an_object"
    rows = doc.get("candidates")
    if not isinstance(rows, list):
        return [], "missing_candidates_list"

    out: list[dict] = []
    rejections: list[str] = []
    guard = card_guard.guard_enabled()
    for row in rows:
        if not isinstance(row, dict):
            continue
        summary = " ".join(str(row.get("summary") or "").split())[:CANDIDATE_SUMMARY_CHARS]
        evidence = " ".join(str(row.get("evidence") or "").split())[:CANDIDATE_EVIDENCE_CHARS]
        # 候选没有正文，只体检 summary（证据可以为空）；判据与卡的 summary 同一份。
        rejection = _summary_rejection(summary, guard=guard, signals=signals)
        if rejection:
            rejections.append(rejection)
            continue
        about = str(row.get("about") or "").strip().lower()
        item = {"about": about if about in _ABOUT_VALUES else "person",
                "summary": summary}
        if evidence:
            item["evidence"] = evidence
        raw_date = row.get("occurred_at")
        if isinstance(raw_date, str) and raw_date.strip():
            date = _normalize_ts(raw_date)
            # 候选阶段宽松：日期写坏就丢日期、不丢事实。写卡那步照常严格。
            if date:
                item["occurred_at"] = date
        out.append(item)
    if rejections:
        if strict:
            return [], format_error(rejections)
        if not out:
            return [], format_error(rejections, after_retry=True)
    return out, None


def _summary_rejection(summary: str, *, guard: bool, signals: LeakSignals) -> str | None:
    reason = placeholder_reason(summary)
    if reason:
        return f"summary_{reason}"
    if guard and card_guard.hard_field_pollution_reason(summary, signals):
        return "summary_protocol_leak"
    if substantive_len(summary) < MIN_SUMMARY_CHARS:
        return "summary_too_short"
    return None


_NORM_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


def candidate_key(candidate: dict) -> str:
    """跨批去重用的键：只去**字面上相同**的候选（大小写、空白、标点不算差别）。

    刻意保守。「喜欢美式」和「喜欢拿铁」字面很像但必须是两条；语义上的同义
    改写交给写卡那一步的模型（它同时看得见两条），不在这里用阈值猜。
    """
    return _NORM_STRIP.sub("", str(candidate.get("summary") or "").casefold())


def render_candidate_digest(candidates: list[dict]) -> str:
    """写卡阶段的「窗口」：候选清单。"""
    lines = [
        "Candidate facts distilled from the material they handed you, one per line "
        "(about · date · fact — evidence). They may repeat each other or repeat "
        "the existing memory index: deduplicate, then write cards."
    ]
    for c in candidates:
        date = str(c.get("occurred_at") or "").strip() or "-"
        evidence = str(c.get("evidence") or "").strip()
        tail = f" — “{evidence}”" if evidence else ""
        lines.append(f"- [{c.get('about') or 'person'}] {date} · {c.get('summary')}{tail}")
    return "\n".join(lines)


__all__ = [
    "CANDIDATE_EVIDENCE_CHARS",
    "CANDIDATE_SUMMARY_CHARS",
    "build_import_candidates_prompt",
    "build_import_candidates_retry_prompt",
    "candidate_key",
    "parse_import_candidates",
    "render_candidate_digest",
]
