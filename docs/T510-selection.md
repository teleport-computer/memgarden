# T510/T512: opt-in relevant context selection

`scoring.relevance.select_relevant_context_memories_with_trace(cards, query)`
provides ambient recall with a default relevance floor of 0.35 and medium/strong
lexical evidence required in every bucket. Turning points reserve up to three
seats, recent eligible cards up to two, and ranked relevant cards use all remaining
seats up to the overall cap (eight by default). Unrelated cards never fill spare
seats. Ties use normalized timestamps and IDs, not input order.

Roles must be explicit `roles=["turning_point"]`; titles do not confer roles.
An empty query or no eligible cards returns an empty selection. The threshold is
lexical and does not establish factual accuracy or answerability.

Existing default/strict context-selection modes and arbitrary `selection.Chain`
compositions do not opt into this new policy. Hosts can opt in with the new
function or `mode="relevant"`; upgrading the package alone does not select the
new context policy for those callers. See
`test_strict_threshold_cap_and_compatibility_are_explicit` and the existing
selection-path agreement suite.

```python
from memgarden.scoring import relevance

cards, trace = relevance.select_context_memories_with_trace(
    moments, latest_user_text, mode="relevant", cap=8,
)
# Or use the explicit entry point to configure the threshold:
cards, trace = relevance.select_relevant_context_memories_with_trace(
    moments, latest_user_text, cap=8, min_relevance=0.35,
)
```

IO's T512 adapter detects the new function after a reviewed release/pin upgrade,
but continues working on published 0.19.0. IO does not require an unpublished
editable package. Its integration point is `backend/enclave/routes/chat.py`,
which feature-detects `select_relevant_context_memories_with_trace`; upgrading
IO's pin is a separate change after the package is released.

## Index-ranking compatibility change: remove `is_open_thread`

Separately from the opt-in context policy, the index selector no longer gives
`is_open_thread=True` a metadata-score bonus or a tie-breaking preference.
This is an observable change for callers that supply the field, including those
that do not opt into relevant context selection. There is no package producer
for this field; a field without a producer is not an ordering signal. Explicit
semantic roles remain the input for the new context policy.
`test_unwritten_open_thread_flag_no_longer_influences_index_score` pins the
removed bonus. No stored card is rewritten or deleted.

Validation: `tests/test_relevant_context.py` covers soft quota redistribution,
irrelevant turning/recent exclusion, explicit roles, stable ties, no-match/empty
queries, caps and compatibility. Existing selection-path agreement tests remain
unchanged and must pass. No storage, model calls, or dependency additions.

## T513: optional retrieval cues and discrete importance prompts

Capture asks for three to five short grounded `retrieval_cues`: existing aliases,
keywords, answerable questions, and event time only when supported by the source.
These are search hints, not new facts. Dream may repair missing cues without
inventing details. Parsers accept only strings, collapse whitespace, deduplicate,
and retain at most five cues of 120 characters; absent or invalid cues add no key.

Both prompts ask for integer `importance_level` from one to five. Parsing maps
that level to the existing public `importance` float (level / 5), retaining the
legacy clamped float when the new level is absent or invalid. The storage and
selection importance contract remains 0–1; old model output remains accepted.

`tests/test_retrieval_cues.py` covers capture/dream output, legacy compatibility,
all five levels, invalid values, bounded hints and grounded prompt instructions.
This is the combined external PR1+PR2 change, not a published release.
IO T513 continues to use published memgarden 0.19.0; its optional
cue read/write plumbing does not imply that this future producer is deployed.
