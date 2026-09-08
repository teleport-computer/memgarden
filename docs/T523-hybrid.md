# T523 batch 2a: opt-in hybrid (dense + lexical) context selection

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
`roles`, ≤3) and recent cards (`created_at` within `recent_within_days` of the
newest candidate, ≤2, deterministic — no wall clock) reserve seats, everything
else fills by `F`. Buckets decide **which** seats a card may take; the returned
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
lexical lane and says so (`trace["vector_lane"] == "absent"`).

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

This is a stacked change on top of the T514 PR (#2): it adds one module, one
test file and this note, and does not modify anything that PR touches.
