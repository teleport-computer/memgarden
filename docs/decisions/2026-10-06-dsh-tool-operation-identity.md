# DSH tool write operation identity

## Problem

The Memgarden service and store already support replay-safe `memory_write` through `tool.invoke.idempotency_key`. The DSH adapter discarded the execution context and called `tool.invoke` without that key. Re-executing the same DSH tool operation therefore looked like a new write and could commit a duplicate card.

## Product invariant

- Replaying the same logical DSH tool operation must return the original outcome and must not commit another mutation.
- A later, distinct DSH tool operation may intentionally save identical content.
- Identity must not be inferred from card text, argument equality, time, ordering, or UI state.

## Authority and contract

DSH is the first component that knows the operation identity. Its tool contract supplies:

- `exec.agent.session.id`: durable conversation identity;
- `exec.callId`: tool-call identity within that conversation.

The adapter encodes the tuple `['dsh-tool-v1', session_id, call_id]` as the Memgarden idempotency key. The versioned JSON tuple is unambiguous even when either identifier contains punctuation. Memgarden remains responsible for atomically storing the receipt and rejecting key reuse with different input.

## Failure paths

- Response lost after commit: replay with the same tuple returns the stored receipt and creates no second card.
- Same content in a later call: a different `call_id` is a different operation and may create a new card.
- Missing session or call identity: `memory_write` fails before mutation instead of silently executing without replay safety.
- Concurrent replay: the existing store transaction arbitrates the shared idempotency key; the adapter adds no lock or retry loop.
- Cancellation: no new behavior; the adapter does not initiate another request or model call.

## Rejected alternatives

- Content deduplication: conflates distinct user intentions and breaks save-after-delete.
- Adapter-side ledger: duplicates the store's authority and creates a second recovery problem.
- Extra lookup before write: adds latency and still races with concurrent writers.
- New runtime state or retry protocol: unnecessary because both sides already expose the required identity and idempotency contracts.
