"""
Stage 5: Status resolution.

A task is only "done" when there is explicit confirming evidence — a
later message classified as `resolution` that matches the same object
keyword taxonomy. A passed deadline with no such evidence is `overdue`,
never silently `done`. `as_of` simulates "today" for the exercise's
fixed week, since the real system clock is meaningless against a
scenario frozen in Sept 2026.
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from models.schemas import Message
from engine.extract import OBJECT_TAXONOMY, combined_text


def find_resolution_evidence(object_key: str, messages: List[Message]) -> Optional[str]:
    keywords = OBJECT_TAXONOMY.get(object_key, {}).get("keywords", [])
    for m in messages:
        if m.msg_type != "resolution":
            continue
        if any(kw in combined_text(m) for kw in keywords):
            return m.id
    return None


def compute_status(object_key: str, deadline_date: Optional[str], as_of: str, messages: List[Message]):
    """Returns (status, auto_evidence_message_id_or_None)."""
    resolved_id = find_resolution_evidence(object_key, messages)
    if resolved_id:
        return "done", resolved_id

    if deadline_date is None:
        return "pending", None

    try:
        if datetime.fromisoformat(as_of).date() > datetime.fromisoformat(deadline_date).date():
            return "overdue", None
    except ValueError:
        pass

    return "pending", None
