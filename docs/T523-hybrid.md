# T523 batch 2a: opt-in hybrid (dense + lexical) context selection

Implementation/design note retained for provenance; hybrid shipped in v0.20.0. See [Retrieval](RETRIEVAL.md) for the current integration contract and [Status](STATUS.md) for evidence. Host rollout is separate from package publication.

`scoring.hybrid.select_hybrid_context_memories_with_trace(cards, query, *,
query_vector, card_vectors, min_cosine, ...)` fuses two rankings of the same
authorized candidate snapshot:

- **lexical lane** — the existing `_memory_relevance` scorer, gated exactly as
  the `relevant` mode (`min_relevance` 0.35 + medium/strong confidence);
- **vector lane** — cosine between a host-supplied query vector and host-supplied
  card vectors, gated by `min_cosine` (**no default**: cosine scales differ per
  model, the host passes the threshold it calibrated for its model/projection
  version).

Gates are OR-ed, each lane ranks only its own eligible cards (1-based), and the
lanes are fused with weighted Reciprocal Rank Fusion
`F = w_v/(k+r_v) + w_l/(k+r_l)` (defaults `k=20`, weights 2:1; a missing rank
contributes 0, never a fabricated tail rank). The top `shortlist` (default 20)
by `F` is the only pool the soft quotas may draw from: turning points (explicit
`roles`, ≤3) and recent cards (≤2) reserve seats, everything else fills by `F`.
"Recent" means `created_at` within `recent_within_days` before the host-supplied
`reference_time` and not after it — pass your notion of now (IO's
`recent_cards` is relative to now and rejects future timestamps, so this
matches). Without `reference_time` the newest candidate is the reference, which
is deterministic but counts an all-old garden as recent; that fallback is for
clockless hosts, not the recommended path. Buckets decide **which** seats a card may take; the returned
list and the trace are in fusion order. Unrelated cards never fill a seat, an
empty query or no eligible card returns an empty selection, and `cap` /
`shortlist` bound the output.

## Division of labour (plan A)

The host owns embeddings end to end — model, projection text, storage, privacy
and lifecycle — and hands the package only vectors for the candidates it has
them for. The package validates (dimension match, finite, non-zero norm, and an
optional `vector_model` / `card_vector_models` pair that turns mixed-model
vectors into a hard `VectorContractError`), computes cosine in pure Python,
fuses, selects, and returns per-card `vector_score/rank`, `lexical_score/rank`,
`fusion_score/rank`, `lane` and `bucket` in the trace. Vectors are never echoed
back and nothing is persisted. No dependency is added.

Partial input is honest by construction: cards without a vector are ranked in
the lexical lane only; with no query vector at all the function degrades to the
lexical lane and says so (`trace["vector_lane"] == "absent"`). An empty or
whitespace query returns nothing even when vectors are supplied — ambient recall
is keyed on what was said this turn, a vector alone must not smuggle cards in.
A lane weight of 0 is allowed (switches that lane off without changing the call
shape); NaN/inf/negative weights, `k`, or thresholds are rejected, so a trace can
always be JSON-encoded with `allow_nan=False`. Cosine is computed scale-stably
(each side divided by its max-abs component first), so unnormalized vectors of
any magnitude compare correctly instead of overflowing to NaN.

The trace carries `selected`, `rejected_sample` (fused but not chosen) and
`gate_rejected_sample` (passed neither gate, with both raw scores) — bounded
samples so a miss can be diagnosed ("target was there, cosine 0.41 < 0.5").
Trace rows contain titles and matched units: hosts that log outside the trust
boundary must project them through an allow-list (batch 2b's job).

## Compatibility

`default`, `strict` and `relevant` modes and every `selection.Chain` composition
are untouched; `select_context_memories_with_trace` does not dispatch to hybrid
because hybrid needs vectors the legacy signature cannot carry. Hosts opt in by
calling the new function. IO's integration (T523 batch 2b) feature-detects it
after a reviewed release/pin upgrade and folds its own `context_recent`
front-insertion into the recent bucket instead of prepending outside the ranking.

Validation: `tests/test_hybrid_relevance.py` covers a semantic-only hit,
lexical-only exact-code retention, quota starvation of unrelated cards, honest
degradation without vectors, fusion-over-time ordering and input-order
independence, RRF arithmetic and weights, the vector contract (dimension, NaN,
zero norm, model mismatch, bad thresholds), cap/threshold edges, no vector echo
in the trace, and legacy-mode invariance.

This originated as a stacked change on T514 PR (#2); both are now merged. The current contract requires paired vector model metadata, when used, to label every participating card vector; missing labels are not evidence of compatibility.
