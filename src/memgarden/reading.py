"""Lossless, bounded model-facing reads of one authorized memory record.

The budget limits one response, never the stored card. Cursors pin the serialized
record version so an edit cannot silently splice two different versions together.
"""
from __future__ import annotations

import base64
import hashlib
import json

DEFAULT_READ_CHARS = 5000
MAX_READ_CHARS = 20000


def record_chunk(record: dict, *, cursor: str = "", limit: int = DEFAULT_READ_CHARS) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_READ_CHARS:
        raise ValueError(f"limit must be 1..{MAX_READ_CHARS} Unicode characters")
    text = json.dumps(record, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    offset = 0
    if cursor:
        try:
            version, offset = json.loads(base64.urlsafe_b64decode(cursor))
            if type(offset) is not int or not 0 <= offset < len(text):
                raise ValueError("invalid offset")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("invalid read cursor") from exc
        if version != digest:
            raise ValueError("record_changed: restart reading without cursor")
    end = min(len(text), offset + limit)
    next_cursor = (base64.urlsafe_b64encode(json.dumps([digest, end]).encode()).decode()
                   if end < len(text) else "")
    return {"record_id": str(record["id"]), "text": text[offset:end],
            "format": "json_fragment", "offset": offset, "total_chars": len(text),
            "next_cursor": next_cursor}
