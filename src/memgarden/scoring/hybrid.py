"""Hybrid context selection: dense (vector) + the existing lexical scorer, fused by
weighted Reciprocal Rank Fusion.  Opt-in, pure computation, no dependencies.

Division of labour (T523 plan A):
  * The **host** owns embeddings end to end — model, projection text, storage,
    privacy, lifecycle.  It hands this function one query vector and a mapping
    ``card_id -> vector`` for whatever candidates it has vectors for.
  * The **package** validates the vectors, computes cosine, reuses the lexical
    scorer, fuses the two rankings and applies the soft quotas.  It never sees a
    model, never persists anything, and returns only per-card scores/ranks in
    the trace (no vectors are echoed back).

Fusion: ``F(d) = w_v / (k + r_v(d)) + w_l / (k + r_l(d))`` with ranks starting
at 1 and a missing rank contributing 0.  Ranks are computed over the *eligible*
cards of each lane only (a card must pass that lane's gate to be ranked in it),
so an unrelated card can never inherit a tail rank.

Gates are OR-ed: a card is eligible if it passes the lexical gate (existing
``min_relevance`` + medium/strong confidence) **or** the vector gate
(``min_cosine``).  The vector gate has **no default**: cosine scales differ by
model (E5 runs high), so the host must pass the threshold it calibrated for the
model/projection version it uses.  Passing ``vector_model`` on both sides makes
mixing vectors from different models a hard error instead of a silent wrong
ranking.

Soft quotas (turning ≤ 3, recent ≤ 2, the rest by fusion) are applied **inside**
the fusion-ordered shortlist; each bucket keeps fusion order, unused seats go to
the next bucket, and nothing outside the shortlist is pulled in to fill a seat.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

from .. import timestamps as memory_timestamps
from .relevance import (
    ROLE_TURNING_POINT,
    _memory_relevance,
    _query_trace,
)

MODE_HYBRID = "hybrid"

DEFAULT_RRF_K = 20
DEFAULT_VECTOR_WEIGHT = 2.0
DEFAULT_LEXICAL_WEIGHT = 1.0
DEFAULT_SHORTLIST = 20
DEFAULT_MIN_RELEVANCE = 0.35
TURNING_QUOTA = 3
RECENT_QUOTA = 2


class VectorContractError(ValueError):
    """The host handed us vectors that cannot be compared (dim / NaN / model)."""


def _as_float_vector(value: object, *, what: str) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise VectorContractError(f"{what}: vector must be a sequence of numbers")
    out: list[float] = []
    for x in value:
        try:
            f = float(x)
        except (TypeError, ValueError) as exc:
            raise VectorContractError(f"{what}: non-numeric component") from exc
        if not math.isfinite(f):
            raise VectorContractError(f"{what}: non-finite component")
        out.append(f)
    if not out:
        raise VectorContractError(f"{what}: empty vector")
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain cosine similarity; vectors need not be pre-normalized."""
    if len(a) != len(b):
        raise VectorContractError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        raise VectorContractError("zero-norm vector")
    return dot / math.sqrt(na * nb)


def rrf_fuse(
    ranks_by_lane: Mapping[str, Mapping[str, int]],
    weights: Mapping[str, float],
    *,
    k: int = DEFAULT_RRF_K,
) -> dict[str, float]:
    """Weighted Reciprocal Rank Fusion. ``ranks_by_lane[lane][id]`` is a 1-based
    rank; ids absent from a lane contribute 0 for that lane."""
    if k < 0:
        raise ValueError("k must be >= 0")
    fused: dict[str, float] = {}
    for lane, ranks in ranks_by_lane.items():
        w = float(weights.get(lane, 0.0))
        for cid, r in ranks.items():
            if r < 1:
                raise ValueError("ranks are 1-based")
            fused[cid] = fused.get(cid, 0.0) + w / (k + r)
    return fused


def _rank(sorted_ids: Sequence[str]) -> dict[str, int]:
    return {cid: i + 1 for i, cid in enumerate(sorted_ids)}


def _is_recent(card: dict, candidates: Iterable[dict], within_days: int) -> bool:
    """``created_at`` within ``within_days`` of the newest candidate ``created_at``."""
    if within_days < 0:
        return False
    own = memory_timestamps.parse_ts(card.get("created_at"))
    if own is None:
        return False
    newest = None
    for c in candidates:
        t = memory_timestamps.parse_ts(c.get("created_at"))
        if t is not None and (newest is None or t > newest):
            newest = t
    if newest is None:
        return False
    return (newest - own).total_seconds() <= within_days * 86400


def select_hybrid_context_memories_with_trace(
    moments: list[dict],
    query: str,
    *,
    query_vector: Sequence[float] | None,
    card_vectors: Mapping[str, Sequence[float]] | None,
    min_cosine: float,
    cap: int = 8,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    rrf_k: int = DEFAULT_RRF_K,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
    shortlist: int = DEFAULT_SHORTLIST,
    vector_model: str | None = None,
    card_vector_models: Mapping[str, str] | None = None,
    recent_within_days: int = 7,
) -> tuple[list[dict], dict]:
    """Pick up to ``cap`` cards by fused dense + lexical relevance.

    ``query_vector`` / ``card_vectors`` may be ``None`` or partial: cards without
    a vector are ranked in the lexical lane only, and with no query vector at all
    the function degrades to the lexical lane and says so in the trace
    (``vector_lane="absent"``).  It never fabricates a vector rank.

    "Recent" for the recent bucket means ``created_at`` within
    ``recent_within_days`` of the newest ``created_at`` among the candidates —
    deterministic, no wall clock.  Every bucket is walked in fusion order and the
    returned list is in fusion order too; the bucket only decides *which* seats a
    card may take, never its position.
    """
    if not math.isfinite(min_relevance) or not 0 <= min_relevance <= 1:
        raise ValueError("min_relevance must be finite and between zero and one")
    if not math.isfinite(min_cosine) or not -1 <= min_cosine <= 1:
        raise ValueError("min_cosine must be finite and between -1 and 1")
    if vector_weight < 0 or lexical_weight < 0:
        raise ValueError("weights must be >= 0")
    cap = max(0, int(cap))
    shortlist = max(cap, int(shortlist))

    query = query or ""
    cards = [m for m in moments if m.get("id")]
    by_id = {str(m["id"]): m for m in cards}

    # --- lexical lane (unchanged scorer) ---------------------------------
    lexical: dict[str, dict] = {cid: _memory_relevance(query, m) for cid, m in by_id.items()}
    lex_eligible = [
        cid for cid, r in lexical.items()
        if r["score"] >= min_relevance and r["confidence"] in {"medium", "strong"}
    ]
    lex_sorted = sorted(
        lex_eligible,
        key=lambda cid: (
            lexical[cid]["score"],
            memory_timestamps.sort_key(by_id[cid].get("occurred_at")),
            cid,
        ),
        reverse=True,
    )
    lex_rank = _rank(lex_sorted)

    # --- vector lane -------------------------------------------------------
    vector_lane = "absent"
    vec_score: dict[str, float] = {}
    vec_rank: dict[str, int] = {}
    if query_vector is not None and card_vectors:
        qv = _as_float_vector(query_vector, what="query_vector")
        vector_lane = "active"
        for cid in by_id:
            raw = card_vectors.get(cid)
            if raw is None:
                continue
            if vector_model is not None and card_vector_models is not None:
                cm = card_vector_models.get(cid)
                if cm is not None and cm != vector_model:
                    raise VectorContractError(
                        f"card {cid}: vector model {cm!r} != query model {vector_model!r}"
                    )
            cv = _as_float_vector(raw, what=f"card_vectors[{cid}]")
            vec_score[cid] = cosine(qv, cv)
        vec_eligible = [cid for cid, s in vec_score.items() if s >= min_cosine]
        vec_sorted = sorted(
            vec_eligible,
            key=lambda cid: (
                vec_score[cid],
                memory_timestamps.sort_key(by_id[cid].get("occurred_at")),
                cid,
            ),
            reverse=True,
        )
        vec_rank = _rank(vec_sorted)

    # --- fusion over the union of eligible ids ------------------------------
    fused = rrf_fuse(
        {"vector": vec_rank, "lexical": lex_rank},
        {"vector": vector_weight, "lexical": lexical_weight},
        k=rrf_k,
    )
    fused_sorted = sorted(
        fused,
        key=lambda cid: (
            fused[cid],
            memory_timestamps.sort_key(by_id[cid].get("occurred_at")),
            cid,
        ),
        reverse=True,
    )
    short = fused_sorted[:shortlist]
    short_set = set(short)

    # --- soft quotas inside the shortlist, each bucket in fusion order --------
    chosen: list[dict] = []
    traces: list[dict] = []
    seen: set[str] = set()

    def lane_of(cid: str) -> str:
        if cid in vec_rank and cid in lex_rank:
            return "both"
        return "vector" if cid in vec_rank else "lexical"

    def row(cid: str, *, bucket: str, selected: bool) -> dict:
        m = by_id[cid]
        r = lexical[cid]
        return {
            "id": cid,
            "title": str(m.get("title") or "")[:160],
            "type": str(m.get("type") or "")[:40],
            "score": round(float(fused.get(cid, 0.0)), 6),
            "fusion_score": round(float(fused.get(cid, 0.0)), 6),
            "fusion_rank": (fused_sorted.index(cid) + 1) if cid in fused else None,
            "vector_score": round(float(vec_score[cid]), 4) if cid in vec_score else None,
            "vector_rank": vec_rank.get(cid),
            "lexical_score": float(r["score"]),
            "lexical_rank": lex_rank.get(cid),
            "confidence": str(r["confidence"]),
            "matched_units": list(r.get("matched_units") or [])[:8],
            "matched_phrases": list(r.get("matched_phrases") or [])[:6],
            "reason": str(r.get("reason") or "")[:120],
            "lane": lane_of(cid) if cid in fused else "none",
            "bucket": bucket,
            "selected": bool(selected),
        }

    def annotate(cid: str, *, bucket: str) -> dict:
        out = dict(by_id[cid])
        sel = row(cid, bucket=bucket, selected=True)
        out["selection"] = {
            k: sel[k] for k in (
                "score", "fusion_score", "fusion_rank", "vector_score", "vector_rank",
                "lexical_score", "lexical_rank", "confidence", "matched_units",
                "matched_phrases", "reason", "lane", "bucket",
            )
        }
        return out

    def choose(pool: Sequence[str], quota: int, bucket: str) -> None:
        added = 0
        for cid in pool:
            if cid in seen:
                continue
            if len(chosen) >= cap or added >= quota:
                break
            seen.add(cid)
            chosen.append(annotate(cid, bucket=bucket))
            traces.append(row(cid, bucket=bucket, selected=True))
            added += 1

    turning = [cid for cid in short if ROLE_TURNING_POINT in (by_id[cid].get("roles") or [])]
    recent = [cid for cid in short if _is_recent(by_id[cid], by_id.values(), recent_within_days)]
    choose(turning, TURNING_QUOTA, "turning")
    choose(recent, RECENT_QUOTA, "recent")
    choose(short, cap - len(chosen), "query")
    # Buckets decide seats, fusion decides order: hand back the picks by fusion rank.
    chosen.sort(key=lambda c: fused_sorted.index(str(c["id"])))
    traces.sort(key=lambda t: fused_sorted.index(t["id"]))

    rejected = [cid for cid in fused_sorted if cid not in seen]
    trace = {
        **_query_trace(query),
        "mode": MODE_HYBRID,
        "vector_lane": vector_lane,
        "vector_model": vector_model,
        "min_cosine": min_cosine,
        "min_relevance": min_relevance,
        "rrf_k": rrf_k,
        "weights": {"vector": vector_weight, "lexical": lexical_weight},
        "shortlist": shortlist,
        "recent_within_days": recent_within_days,
        "counts": {
            "candidates": len(by_id),
            "with_vector": len(vec_score),
            "vector_eligible": len(vec_rank),
            "lexical_eligible": len(lex_rank),
            "fused": len(fused),
            "shortlisted": len(short),
            "selected": len(chosen),
        },
        "selected": traces,
        "rejected_sample": [row(cid, bucket="rejected", selected=False) for cid in rejected[:8]],
    }
    return chosen, trace
