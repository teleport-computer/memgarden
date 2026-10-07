"""Incremental Dream: versioned coverage, fair backlog and bounded neighborhoods.

Progress records hashes, not copies of facts. Reads/reference counters do not dirty
a card. LLM context is bounded separately from the size of the local index.
"""
from __future__ import annotations

import hashlib
import json

from .retrieval import rank, default_search_text

_FIELDS = ("summary", "content", "bucket", "threads", "occurred_at", "retrieval_cues",
           "importance", "pulse", "source", "supersedes")


def card_version(card: dict) -> str:
    material = {key: card.get(key) for key in _FIELDS}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()


def pending_cards(cards: list[dict], reviewed: dict[str, str]) -> list[dict]:
    return sorted((c for c in cards if reviewed.get(str(c["id"])) != card_version(c)),
                  key=lambda c: (str(c.get("created_at") or ""), str(c["id"])))


def select_batch(cards: list[dict], pending: list[dict], *, limit: int, tokenizer=None) -> list[dict]:
    """At least half the slots advance the oldest backlog; the rest connect it.

    Same-thread and lexical matches pull previously reviewed facts back in. This
    is a heuristic neighborhood, not a guarantee to detect every semantic link.
    """
    if limit < 1:
        raise ValueError("cards_limit must be positive")
    primary = pending[:max(1, (limit + 1) // 2)]
    used = {str(c["id"]) for c in primary}
    others = [c for c in cards if str(c["id"]) not in used]
    threads = {str(t) for c in primary for t in c.get("threads", [])}
    same_thread = sorted((c for c in others if threads.intersection(c.get("threads", []))),
                         key=lambda c: str(c["id"]))
    query = "\n".join(default_search_text(c) for c in primary)
    by_id = {str(c["id"]): c for c in others}
    # Batches span topics: do not reject an old card for matching only one topic.
    related = [by_id[rid] for rid in rank(query, others, limit=limit, tokenizer=tokenizer,
                                         min_coverage=0.0, strong_evidence=0.0).ids]
    result = list(primary)
    for card in same_thread + related + pending:
        rid = str(card["id"])
        if rid not in used:
            result.append(card)
            used.add(rid)
        if len(result) >= limit:
            break
    return result[:limit]


def committed_progress(state: dict, mutations: list[dict], results: list[dict],
                       staged: dict[str, dict]) -> dict:
    """Mark this transaction's Dream outputs reviewed without triggering itself."""
    if "reviewed_versions" not in state:
        return state
    reviewed = dict(state["reviewed_versions"])
    for mutation, result in zip(mutations, results):
        rid = str(result.get("id") or "")
        if (mutation.get("op") in {"add", "supersede"} and rid in staged
                and staged[rid].get("source") == "memory_dream"):
            reviewed[rid] = card_version(staged[rid])
    # Deleted/retired IDs must not grow this ledger forever.
    reviewed = {rid: version for rid, version in reviewed.items()
                if rid in staged and not staged[rid].get("archived")
                and not staged[rid].get("superseded_by")}
    return {**state, "reviewed_versions": reviewed}
