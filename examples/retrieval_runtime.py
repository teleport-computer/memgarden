"""Relevant SelectionPolicy on the unified ranker: project roles without changing storage.

Run: python examples/retrieval_runtime.py
The model and vectors are synthetic; this is integration evidence, not a
benchmark of real embeddings. No key, network or production data is used.
"""
from __future__ import annotations

import json

from memgarden import CaptureRequest, MountedGarden, Scope
from memgarden.retrieval import select_context
from memgarden.selection import Pick, SelectionResult
from memgarden.stores.memory import InMemoryStore


def project_for_search(card):
    """Example HOST policy: map the stored single ``role`` to selector ``roles``.

    Search text needs no projection: ``retrieval.default_search_text`` already reads
    summary, content, bucket, threads and normalized retrieval_cues. A host with its
    own searchable fields passes ``search_text`` (or ``text_of=``) instead.
    """
    projected = dict(card)
    if "roles" not in card:
        role = str(card.get("role") or "").strip()
        projected["roles"] = [role] if role else []
    return projected


class RelevantPolicy:
    def select(self, cards, query, *, limit):
        # MountedGarden already restricted these candidates to this Scope.
        projected = [project_for_search(card) for card in cards]
        chosen, _ = select_context(query, projected, cap=limit)
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
    chosen, trace = select_context(
        "outdoor exercise", cards, query_vector=[1.0, 0.0],
        card_vectors={"c1": [1.0, 0.0]}, min_cosine=0.9,
        vector_model="synthetic-demo", card_vector_models={"c1": "synthetic-demo"},
    )
    assert [card["id"] for card in chosen] == ["c1"]
    assert trace["vector_lane"] == "active"
    assert chosen[0]["selection"]["reason"] == "hybrid_rrf"
    assert chosen[0]["selection"]["lanes"] == {"lexical": None, "vector": 1}
    print("Relevant policy: cues + role projection + owner isolation: PASS")
    print("Hybrid: synthetic vector contract + fusion: PASS (not a quality benchmark)")


if __name__ == "__main__":
    main()
