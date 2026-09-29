"""T744: keep the concrete specifics of this person's life.

History import used to tell the model not to extract one-off events, and
conversation capture to keep only one or two things. Both dropped exactly the
details a person later asks about (what they bought, which play, how long the
move took). Import now keeps those specifics; capture still writes one card per
thing but keeps the details inside it; dream may not merge them away.
"""
import pathlib

import pytest

from memgarden import policies
from memgarden.prompts.capture import build_capture_prompt
from memgarden.prompts import dream as dream_prompts
from memgarden.prompts.history_import import build_import_candidates_prompt

GOLDEN = pathlib.Path(__file__).parent / "golden"

CAPTURE_KW = dict(ai_name="Aster", user_name="Mina", buckets="Pets", threads="Mochi", identity="(none)",
                  window="2024-03-02 Mina: Mochi pushed the cup off the table again",
                  cards="- m_1: [Pets] a cat named Mochi")

#: (v0.22.0 text, new text) for the explicitly reviewed capture passages.
CAPTURE_EDITS = [
    ('content: a "thick" body, the way you would hold the whole thing in your own mind — what happened, what led to it and what followed, what it means for this person, the feeling in the moment. Not a one-line title.',
     'content: preserve the full factual account, including concrete details. Include causes, consequences, feelings and personal meaning ONLY when explicitly stated by this person or directly evidenced in the source. A short factual card is better than an embellished "thick" one. Never add inferred motives, personality traits, causal links or missing dates, even qualified with "perhaps". An assistant\'s speculation is not a user fact; preserve attribution and uncertainty of actual source statements.'),
    ('never insight/reflection (those belong to dreaming).',
     'never insight/reflection. Dream also reorganizes facts, not hypotheses; speculative insights require a separate future feature.'),
    ('''· An isolated data point ("had a latte today") usually does not deserve its own card — unless it is a preference this person
  clearly cares about or that keeps recurring ("I only drink oat milk", "he always orders Blue Bottle"), in which case it is
  worth keeping as a preference.''',
     '''· An isolated data point ("had a latte today") usually does not deserve its own card — fold it into the card it belongs to
  instead of dropping it. If it is a preference this person clearly cares about or that keeps recurring ("I only drink oat milk",
  "he always orders Blue Bottle"), it is worth keeping as a preference.'''),
    ('''· Fewer, not more. If only one or two things from this stretch survive, which one or two? Force yourself to generalize instead of
  splitting every point of a single conversation into its own card.''',
     '''· Fewer, not more — fewer cards, not fewer facts. If only one or two things from this stretch survive, which one or two? Force
  yourself to generalize instead of splitting every point of a single conversation into its own card.
· Generalizing must not erase the specifics: keep the concrete details this person mentioned (what, who, where, when, how many,
  how long; names, titles, numbers, dates) inside the card they belong to.
  Keep partial dates partial: never supply a missing year, including in retrieval_cues. Do not infer unstated motives or reasons
  to make the card thicker; preserve what was actually said and leave unknown details unknown.'''),
]


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_capture_prompt_differs_from_v0_22_0_only_in_reviewed_passages(locale):
    """The golden was rendered from the untouched v0.22.0 source."""
    now = build_capture_prompt(**CAPTURE_KW, policy="conversation_capture", locale=locale)
    before = (GOLDEN / f"capture_conversation_v0_22_0_{locale}.txt").read_text()
    for old, new in CAPTURE_EDITS:
        assert now.count(new) == 1
        now = now.replace(new, old)
    assert now == before


def test_capture_still_carries_the_quotable_restraint_phrase():
    # Hosts quote this phrase when they override it (call transcripts).
    prompt = build_capture_prompt(**CAPTURE_KW, policy="conversation_capture", locale="en")
    assert policies.RESTRAINT_RULE_QUOTE in prompt
    assert "keep the concrete details" in prompt


def test_history_import_rubrics_keep_one_off_specifics():
    for text in (policies.HISTORY_IMPORT_OPENING_RUBRIC, policies.HISTORY_IMPORT_FILTER_RUBRIC,
                 policies.HISTORY_IMPORT_CARD_OPENING_RUBRIC):
        assert "one-off events" not in text
        assert "durable facts worth keeping long term" not in text
    assert "DO keep the concrete specifics" in policies.HISTORY_IMPORT_FILTER_RUBRIC
    assert "what the companion itself said or suggested" in policies.HISTORY_IMPORT_FILTER_RUBRIC
    for name in ("history_import_single_pass.txt", "history_import_two_pass_candidates.txt",
                 "history_import_two_pass_write.txt"):
        golden = (GOLDEN / name).read_text()
        assert "DO keep the concrete specifics" in golden and "one-off events" not in golden


@pytest.mark.parametrize("policy", ["history_import", "curated_archive"])
def test_rendered_candidates_accept_supported_one_off_events(policy):
    prompt = build_import_candidates_prompt(
        window="Mina bought a yellow dress on May 2.", locale="en", policy=policy)
    assert "One candidate = one supported fact or concrete event" in prompt
    assert "one durable fact" not in prompt
    if policy == "curated_archive":
        # Curated's keep-all filter is unchanged, but its shared opening/rules
        # also change. Test the rendered prompt rather than one constant.
        assert policies.HISTORY_IMPORT_OPENING_RUBRIC in prompt
        assert policies.KEEP_ALL_MAP_SUFFIX in prompt
        assert policies.HISTORY_IMPORT_FILTER_RUBRIC not in prompt


def test_curated_archive_keep_all_filter_is_untouched():
    assert "Preserve EVERY candidate fact" in policies.KEEP_ALL_MAP_SUFFIX
    assert "DO keep the concrete specifics" not in policies.KEEP_ALL_MAP_SUFFIX


def test_dream_merge_keeps_concrete_values():
    assert "A merged card must never be vaguer than the cards it replaces." in dream_prompts._DREAM_PROMPT_TEMPLATE
