"""Optional retrieval hints and five-level importance, backwards compatible."""
from __future__ import annotations


def retrieval_cues(value: object) -> list[str]:
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        cue = " ".join(item.split())[:120]
        if cue and cue not in out:
            out.append(cue)
        if len(out) == 5:
            break
    return out


def importance(value: dict, legacy) -> float:
    level = value.get("importance_level")
    if type(level) is int and 1 <= level <= 5:
        return level / 5.0
    return legacy(value.get("importance"))
