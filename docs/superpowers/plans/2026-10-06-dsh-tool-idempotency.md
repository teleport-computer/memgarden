# DSH Tool Idempotency Implementation Plan

> **For Z:** Execute this plan in the current isolated worktree; do not modify or merge the source PR branch directly.

**Goal:** Make one DSH `memory_write` tool operation produce at most one committed Memgarden mutation when the same operation is replayed, while allowing a later distinct operation with identical content.

**Architecture:** DSH owns the durable operation identity as `(agent.session.id, exec.callId)`. The adapter serializes that tuple into Memgarden's existing `tool.invoke.idempotency_key`; Memgarden's store remains the atomic authority for replay detection and receipt return. No new persistence, lifecycle, retry, or semantic state is introduced.

**Tech Stack:** Node.js DSH adapter, Python Memgarden service, SQLite integration tests, pytest.

---

### Task 1: Reproduce the missing adapter contract

- [x] Add an offline adapter regression that executes the same registered `memory_write` twice with the same DSH session/call identity, then once with a different call identity.
- [x] Assert the first two executions commit one record and the distinct call commits a second record.
- [x] Run the focused test and confirm it fails before changing production code.

### Task 2: Forward the authoritative operation identity

- [x] Add a small helper that creates an unambiguous, versioned key from `agent.session.id` and `exec.callId`.
- [x] Require that identity for `memory_write`; do not infer it from content, arguments, timing, or UI state.
- [x] Pass the key through the existing `tool.invoke.idempotency_key` field.
- [x] Run the focused test and confirm it passes.

### Task 3: Close and deliver

- [x] Run adapter, core idempotency, and full repository verification from the final tree.
- [x] Record the user-visible before/after, failure/retry/concurrency behavior, and remaining real-DSH boundary in a closure report.
- [x] Commit and push the isolated branch, then open stacked PR #13 against `fix/0.23-memory-closure` for owner review.
