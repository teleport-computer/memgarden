"""Original-request identity, independent of generated model output or live index."""
from dataclasses import asdict
import hashlib
import json


def input_digest(scope, request) -> str:
    values = asdict(request)
    values.pop("_input_digest", None)
    values.pop("idempotency_key", None)
    # actor is supplied by the trusted scope, not by the request payload.
    values["actor"] = scope.actor.as_dict()
    return hashlib.sha256(json.dumps({"scope": asdict(scope), "request": values},
                                    sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def same_request(previous: str | None, current: str | None,
                 previous_mutations: str | None, current_mutations: str) -> bool:
    # A request receipt cannot be replayed as a plain mutation batch, or vice versa.
    if previous or current:
        return bool(previous and current and previous == current)
    return not previous_mutations or previous_mutations == current_mutations
