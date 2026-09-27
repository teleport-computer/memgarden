# PR #10 synthetic real-model review — 2026-09-28

## Decision and scope

The concrete-value regression probe supports merging the prompt change, with the limitations below. This is **not** a clean factuality certificate or evidence of a measured quality improvement. Baseline also preserves the required values on this small corpus. No release, IO dependency bump, production data test, or real DSH run is included.

`results.json` preserves original reports, including failures and the first revised prompt's invented year. All material is synthetic. Seven cases come from `evals/corpus/specifics.jsonl`; no real conversations or credentials are included.

## Reproduction and provenance

- Baseline: v0.22.0, `05ad1ef369728eb4d7dc4520c2925c3c898444e2`.
- Initial revised prompt: clean `a8e59fb`; final revised prompt: that commit plus the grounding fix committed with this evidence. The final reports truthfully retain `source_dirty=true`.
- Final `src/memgarden/policies.py` SHA-256: `6c7f7bc96dcc2944e71636e2b042413d2ed20f8df017b4a9383c878b0d0dcd93`. Reports contain rendered prompt and corpus hashes.
- Model: `deepseek/deepseek-v4-pro-0813`, OpenRouter provider `coreweave` (response: CoreWeave). Same model, provider and temperature 0 on both sides; reasoning disabled, no fallback, 4096 output tokens, 180-second request timeout, no automatic retry.
- Run `evals/specifics.py --provider openrouter --model deepseek/deepseek-v4-pro-0813 --openrouter-provider coreweave --source-root <tree> --repeat 2`. Explicit supplemental runs use `--case <id>` and separate output files; they never replace failed reports.

## All attempts, not just successful responses

| Stage | Requests | Successful responses | Transport failures | Finding |
|---|---:|---:|---:|---|
| Initial direct-provider route, baseline + revised | 28 | 0 | 28 HTTPError | Account privacy/provider routing rejection; not model-quality evidence |
| CoreWeave baseline, including two supplemental runs | 17 | 14 | 3 RemoteDisconnected | Two successful responses per case |
| Initial revised prompt, including supplement | 16 | 14 | 2 RemoteDisconnected | Value checks passed, but manual review found an invented year |
| Final revised prompt, including supplement | 15 | 14 | 1 RemoteDisconnected | Two successful responses per case; value and year checks passed |

The first revised Chinese book Capture inserted **2025** in `retrieval_cues` even though the source only says September 12. It was NOT a factuality pass despite the original automatic checks being green. The Capture instruction now explicitly prohibits completing partial dates or inferring unstated motives. The evaluator additionally checks invented years across all card fields, including cues; this narrow guard is not a general factuality checker. Final successful responses did not repeat the year error.

Response-reported charges in the retained reports total **US$0.090240620**. A separate successful routing smoke request reported US$0.000015780. Failed/disconnected requests lack usage evidence; these sums are not reconciled account billing and do not establish that failed calls were free. No account privacy policy was weakened.

## Manual review of baseline and final successful responses

| Case | Review |
|---|---|
| `capture-gift-en` | Nora, yellow dress, $48 and May 2 retained. Body correctly dates purchase. Summary wording “for her birthday on May 2” is ambiguous on both sides; not proof that the birthday was on May 2. Both versions add qualified guesses about why the gift should be remembered. |
| `capture-book-zh` | Nora, 青禾书店, 长日将尽, 68元 and 9月12日 retained. Final cues leave the year unknown. Both versions add qualified guesses about the reason for remembering. |
| `capture-smalltalk` | Empty cards on both sides; no false memory from greetings. |
| `capture-unaccepted-advice` | Empty cards on both sides; assistant suggestion is not stored as a user's purchase. |
| `capture-correction` | $27 is the corrected price; $72 appears only as the superseded price. Baseline additionally infers a concern for accuracy. |
| `dream-merge-en` | Correct two targets; yellow dress, $48, Cedar Shop, May 2 purchase, May 5 delivery, blue box and Lake Cafe retained with chronology. |
| `dream-thicken-zh` | Correct target; 成都→上海, 9月12日, 青禾搬家, 1800元, 6小时 retained, and last box at 20:00 added. |

### Known residual issues and follow-up

The final Capture prompt still produces **qualified, unsupported motive speculation**, e.g. “likely to avoid repeating” or “可能是为了记录开销”. This already occurs with v0.22.0 and is not shown to be a new regression, but is not a desired fact-preservation result. The new prompt instruction did not eliminate it. A follow-up should separate source facts from interpretations and test summary date attachment; do not tell hosts the generated cards are exclusively verified facts. This review accepts the bounded concrete-value change, not these residual behaviors as correct.

Two successful outputs per case, temperature 0, and explicit “remember this” requests make this a small regression probe, not an independent estimate of ordinary Capture quality. It does not measure long-window card limits, conflicting identities, broad hallucination rates, cross-model behavior, or real host execution. The author's history-import 27→39/39 result was **not independently rerun**; its original exclusions and single-run limitations remain in the PR.

## Deterministic validation

- Full offline suite: **1098 passed, 2 existing xfailed**.
- Version consistency: 0.22.0.
- Deterministic eval: no regression against baseline; recall remains 88.9%, including its existing missed case. Real-model evidence above is separate from this command.
- `mount_in_ten_minutes.py`, `wire_capture.py`, `retrieval_runtime.py`: passed.
- Source distribution and wheel build: passed.
- `git diff --check`: passed.

Latest branch CI is recorded on PR #10, not inferred from an older SHA. Paid tests are intentionally not part of ordinary offline CI.
