# Memory Garden

**Memory an Agent can read, recall, and revise—not just accumulate.**

[中文](README.md) · [Integration guide](docs/GETTING-STARTED.md) · [Retrieval](docs/RETRIEVAL.md) · [DeepSeek Harness](adapters/dsh-memgarden/README.md)

Memory Garden (`memgarden`) is an embeddable memory-editing engine for Agent runtimes. It turns conversations into readable cards, selects useful memories for a later turn, and consolidates or supersedes them as new information arrives.

Bring your model, trusted user identity, and storage. Garden supplies Capture, recall, Dream/Maintenance, and persistence orchestration. Start with a SQLite file or implement StoragePort; no specific Agent framework, provider, or vector database is required.

Python ≥ 3.10 · Zero third-party runtime dependencies · Apache-2.0 · Python SDK / JSON Lines / bundled DSH Adapter

## What makes it different

A transcript records what was said. Garden maintains what may be useful to remember.

If someone says “spicy food hurts my stomach” and later adds “a little spice is fine now,” remembering well means retaining the reason, distinguishing updates from contradictions, and recalling the relevant detail at dinner. Garden provides that workflow; model judgment still needs evaluation on your data.

| Design | What it enables |
|---|---|
| Readable, substantive cards | A short summary for navigation; a full body for context, causes, and detail |
| Different writing intentions | Restrained conversational Capture, more inclusive historical import, and explicit user-directed writes |
| Automatic capture separate from tools | Invoke Capture after a turn without waiting for the Agent to choose `memory_write`; tools remain available |
| Evolving memories | Merge, thicken, or supersede cards; retain supersession history while keeping user deletion distinct from archival |
| Replaceable retrieval policy | Compose stages, opt into relevance-gated soft quotas, or supply vectors for hybrid retrieval |
| Explicit persistence semantics | Scoped ownership, idempotency, revision checks, atomic card/maintenance-ledger commits, and honest failure receipts |

This is not a vector database, transcript backup, full Agent runtime, or universal facade over other memory products. Replacing storage does not replace Garden's own memory-editing behavior.

## Quickstart

```bash
python -m pip install memgarden
memgarden manifest
```

This example uses a **fixed model reply, not a real model**. It creates plaintext `garden.db` in the current directory. Do not commit that file.

```python
import json
from memgarden import CaptureRequest, MountedGarden, Scope, SqliteStore
from memgarden.selection import Chain, RelevanceStage

class DemoModel:
    def complete(self, prompt: str, *, purpose: str = "") -> str:
        # Plumbing demo only. Replace this with your runtime's model call.
        return json.dumps({"cards": [{
            "action": "add", "summary": "Avoids spicy food",
            "content": "Spicy food causes stomach pain; choose mild dishes.",
            "bucket": "Food", "threads": ["diet"],
        }]})

garden = MountedGarden(
    model=DemoModel(),
    store=SqliteStore("garden.db"),
    selection_policy=Chain(stages=(RelevanceStage(limit=4),)),
)
# Build this from authenticated runtime state, never from model arguments.
scope = Scope(tenant_id="demo", memory_owner_id="user-42")
receipt = garden.capture_and_store(scope, CaptureRequest(
    window="User: Spicy food gives me stomach pain.",
    locale="en", idempotency_key="conversation-1:turn-1",
))
assert receipt.error is None, receipt.error
assert receipt.written

context = garden.context_for_turn(scope, "Can I eat spicy food?")
assert context.record_ids
for block in context.blocks:
    print(block["text"])
# Pass these blocks to your Agent as memory context before its next reply.
```

Expected output: `Avoids spicy food`. Default context blocks contain summaries, not the full card body. The body remains in storage; fetch it within the same authorized scope when more detail is needed. Reopening the database preserves cards, and replaying the same business request does not duplicate the write. Stores maintain `created_at` and `updated_at`; `occurred_at` is the separately sourced event time.

## Connect your runtime

```text
user input -> recall -> runtime adds memory context -> Agent replies
                                                          |
                         completed turn -> Capture -> persist cards
                                                          |
                         runtime schedules Dream -> cards + ledger
```

1. **Python:** replace `DemoModel.complete` with your provider call returning text or the supported minimal `text`/`truncated` envelope. Build `Scope` from authentication, not model arguments. Give `MountedGarden` a Store and selection policy.
2. **Before each reply:** call `context_for_turn`; insert its blocks as delimited, untrusted memory data. Keep `record_ids` for provenance. Memory does not authorize tool execution.
3. **After each turn:** persist input in your host, then call `capture_and_store` with a stable turn identity. Check `receipt.error`; failure is not “nothing worth remembering.”
4. **Maintenance:** schedule `check_maintenance` and, when due, `run_and_store_maintenance`. Garden commits cards and its ledger together; it does not run an autonomous scheduler.
5. **Management:** use explicit writes, tools, paginated browse/export, historical import, or deletion. Full signatures and recovery semantics are in the [integration guide](docs/GETTING-STARTED.md).

For a non-Python runtime, start `memgarden serve --storage sqlite:///garden.db`. Send JSON Lines over stdin/stdout: `manifest.get`, then `capture.begin` → your model → `capture.feed` until `completed`. Inspect the nested result, not just RPC success. Maintenance has the same begin/feed/cancel shape. The host owns deadlines, cancellation, process restarts, and durable retry input.

These checkout examples make real Store/service calls with synthetic inputs and no network access:

```bash
uv run python examples/mount_in_ten_minutes.py
uv run python examples/wire_capture.py
uv run python examples/retrieval_runtime.py
```

**DeepSeek Harness:** the [bundled Adapter](adapters/dsh-memgarden/README.md) wires recall, turn-end Capture, Maintenance, tools, and outbox recovery. Configure a compatible DSH/provider, stable owner, database, and persistent state directory. Re-run `memgarden install-dsh` after package upgrades to refresh the copied plugin.

## Retrieval is opt-in

- `SelectionPolicy` plugs into `MountedGarden`; low-level relevant/hybrid functions do not automatically replace it.
- `retrieval_cues` are stored hints, not additional evidence. Include them in your `search_text` or embedding projection to use them for retrieval.
- Stored `Card.role` is singular; selection uses `roles: list[str]`. The [policy example](examples/retrieval_runtime.py) explicitly projects between them without rewriting storage.
- Hybrid combines host-supplied vectors and lexical scores with weighted RRF. The host owns embeddings, model/projection versions, authorization, lifecycle, and a calibrated cosine threshold. Garden neither generates nor stores vectors.
- Five-level model importance maps back to the existing 0–1 `importance` field; it is not a second persistent scale.

## Boundaries worth knowing

- Plaintext throughout; no encryption/decryption or key management. Databases, outboxes, and some retrieval traces contain private data.
- SQLite currently reads owner-level collections; response pagination is not database pagination or a cross-request export snapshot.
- Card deletion does not cascade to host transcripts, backups, vector caches, or other derived cards. The host coordinates those layers.
- The default model-less DSH service supports host-driven Capture/Maintenance, but no History Import/Migrate management lane. Runtime `manifest.get` reports actual capabilities.
- Do not share a DSH outbox directory between concurrent processes; it is not a complete transcript backup.
- Tests and live-model results cover particular versions and scenarios, not every provider or production workload. See [current evidence](docs/STATUS.md).

## Documentation and contribution

Detailed guides are currently primarily in Chinese; API names and runnable examples are shared.

[Getting started](docs/GETTING-STARTED.md) · [Data and storage](docs/INTEGRATION-AND-DATA.md) · [Retrieval](docs/RETRIEVAL.md) · [Status](docs/STATUS.md) · [Evals](evals/README.md) · [Releasing](docs/RELEASING.md)

```bash
uv run --extra dev pytest -q
uv run python evals/run.py --baseline evals/baseline.json
uv build
```

Development dependencies are separate from the zero-dependency runtime. Offline Adapter tests require Node.js (CI uses Node 20). Checkout documentation can describe unreleased fixes; deploy an audited tag or wheel rather than overwriting an existing release.

See [CONTRIBUTING](CONTRIBUTING.md) and [SECURITY](SECURITY.md). Report bugs with synthetic reproductions, never user memories or credentials. Licensed under [Apache-2.0](LICENSE).
