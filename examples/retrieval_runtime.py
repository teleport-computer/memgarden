"""Opt-in relevant SelectionPolicy: project cues/role without changing storage.

Run: python examples/retrieval_runtime.py
The model and vectors are synthetic; this is integration evidence, not a
benchmark of real embeddings. No key, network or production data is used.
"""
from __future__ import annotations

import json

from memgarden import CaptureRequest, MountedGarden, Scope
from memgarden.scoring.hybrid import select_hybrid_context_memories_with_trace
from memgarden.scoring.relevance import select_relevant_context_memories_with_trace
from memgarden.selection import Pick, SelectionResult
from memgarden.stores.memory import InMemoryStore


def project_for_search(card):
    """Example HOST policy: preserve explicit search_text and roles if supplied."""
    projected = dict(card)
    if not str(card.get("search_text") or "").strip():
        cues = [x for x in card.get("retrieval_cues", []) if isinstance(x, str)]
        projected["search_text"] = " ".join([
            str(card.get("summary") or ""), str(card.get("content") or ""),
            str(card.get("bucket") or ""), *cues,
        ])
    if "roles" not in card:
        role = str(card.get("role") or "").strip()
        projected["roles"] = [role] if role else []
    return projected


class RelevantPolicy:
    def select(self, cards, query, *, limit):
        # MountedGarden already restricted these candidates to this Scope.
        projected = [project_for_search(card) for card in cards]
        chosen, _ = select_relevant_context_memories_with_trace(projected, query, cap=limit)
        # Return only IDs/evidence. Garden renders original authorized cards.
        return SelectionResult(picks=tuple(Pick(
            card_id=str(card["id"]), stage=card["selection"]["bucket"],
            score=card["selection"]["score"],
        ) for card in chosen))


class DemoModel:
    def complete(self, prompt, *, purpose=""):
        return json.dumps({"cards": [{
            "action": "add", "summary": "Avoids spicy food",
            "content": "Spicy food causes stomach pain; choose mild dishes.",
            "retrieval_cues": ["meal planning", "mild dishes"],
            "role": "turning_point",
        }]})


def main():
    garden = MountedGarden(model=DemoModel(), store=InMemoryStore(),
                           selection_policy=RelevantPolicy())
    scope = Scope(tenant_id="demo", memory_owner_id="user-42")
    receipt = garden.capture_and_store(scope, CaptureRequest(
        window="Spicy food gives me stomach pain.", locale="en", idempotency_key="turn-1"))
    assert receipt.written and not receipt.error
    context = garden.context_for_turn(scope, "meal planning")
    assert context.record_ids == list(receipt.record_ids)
    assert context.blocks[0]["stage"] == "turning"
    other = Scope(tenant_id="demo", memory_owner_id="another-user")
    assert not garden.context_for_turn(other, "meal planning").record_ids

    # Pure synthetic vector illustration, not a calibrated production threshold.
    cards = [{"id": "c1", "summary": "A long walk in the park", "content": "Fresh air helps."}]
    chosen, trace = select_hybrid_context_memories_with_trace(
        cards, "outdoor exercise", query_vector=[1.0, 0.0],
        card_vectors={"c1": [1.0, 0.0]}, min_cosine=0.9,
        vector_model="synthetic-demo", card_vector_models={"c1": "synthetic-demo"},
        reference_time="2026-09-10T00:00:00Z",
    )
    assert [card["id"] for card in chosen] == ["c1"]
    assert trace["vector_lane"] == "active"
    print("Relevant policy: cues + role projection + owner isolation: PASS")
    print("Hybrid: synthetic vector contract + fusion: PASS (not a quality benchmark)")


if __name__ == "__main__":
    main()
