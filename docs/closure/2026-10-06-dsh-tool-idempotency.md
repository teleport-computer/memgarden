# DSH tool idempotency closure report

## User-visible result

Before:

```text
one DSH memory_write operation
  -> adapter drops execution identity
  -> replay is treated as a new operation
  -> duplicate memory card can be committed
```

After:

```text
one DSH memory_write operation
  -> adapter forwards (session_id, call_id)
  -> Memgarden atomically returns the stored receipt on replay
  -> one committed card
```

A later tool call with the same text still has a different operation identity and can intentionally create another card.

## Contract check

| Requirement | Result |
|---|---|
| Same logical operation has at most one effect | Passed: replay of the same session/call pair leaves one record |
| Distinct identical-content operations remain distinct | Passed: a different call ID or session creates a separate record |
| Authority decides at the earliest natural point | DSH supplies `agent.session.id` and `exec.callId`; the adapter does not infer identity |
| Durable execution remains atomic | Existing Memgarden store receipt transaction is reused; no adapter ledger was added |
| Missing authority fails safely | `memory_write` is rejected before mutation when session/call identity is absent |
| No extra latency or model work | No lookup, network round trip, retry, or model call was added |

## Failure, retry, cancellation and concurrency

- Response loss/replay: the same key returns the first receipt without a second mutation.
- Conflicting replay input: the existing request digest contract rejects key reuse with different arguments.
- Concurrent replay: the existing store transaction remains the single arbiter.
- Cancellation: unchanged; this patch adds no retry loop and no background work.
- Process restart: the idempotency receipt is stored in Memgarden's durable store, not adapter memory.

## Verification

- Regression-first proof: before the production change, the adapter scenario produced `3 !== 2` records.
- Focused adapter and core tests: `29 passed`.
- Full tests on Python 3.10, 3.11, 3.12 and 3.13: each `1141 passed, 2 xfailed`.
- Adapter/service failure paths: `25/25` passed.
- DSH acceptance-evidence, wheel-content and public-surface tests: `26 passed`.
- Deterministic evals and all documented examples passed.
- Source distribution and wheel built successfully; a clean venv installed the exact wheel and verified the CLI, public SDK imports and packaged DSH adapter.

## Remaining boundary

The pinned DSH `0.1.2-alpha.4` source contract at commit
`4e84901e6471b79ec0338099867ebb4606d12bb5` was inspected: the runtime passes
`ToolRunContext` as the second `execute` argument and binds the current Agent/session.
This machine has no `DEEPSEEK_API_KEY` and no configured exact-build `DSH_BIN`, so the
paid real-provider acceptance suite was not run. The deterministic test loads the real
Memgarden adapter and service, but it is not evidence of model behavior or a deployed DSH host.
