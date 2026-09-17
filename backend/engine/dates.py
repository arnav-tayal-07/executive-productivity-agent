"""
Resolves relative date phrases ("today", "tomorrow morning", "Wednesday",
"Thursday morning", "Friday, 25 September") into absolute ISO dates,
anchored to the timestamp of the message that used the phrase and the
fixed exercise week (Mon 21 - Fri 25 Sept 2026).
"""
from __future__ import annotations
from datetime import datetime, timedelta
import re

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

WEEK_START = datetime(2026, 9, 21)  # Monday
WEEK_END = datetime(2026, 9, 25)    # Friday


def _weekday_index(name: str) -> int | None:
    name = name.lower()
    for i, w in enumerate(WEEKDAYS):
        if name.startswith(w[:3]):
            return i
    return None


def resolve_date_phrase(phrase: str, anchor_ts: str) -> str | None:
    """
    phrase: free text possibly containing a relative date reference.
    anchor_ts: ISO timestamp of the message the phrase was said in.
    Returns an ISO date string (YYYY-MM-DD) or None if nothing resolvable.
    """
    if not phrase:
        return None
    p = phrase.lower()
    anchor = datetime.fromisoformat(anchor_ts)

    # explicit "Friday, 25 September" / "25 September" style
    m = re.search(r"(\d{1,2})\s+september", p)
    if m:
        day = int(m.group(1))
        return datetime(2026, 9, day).date().isoformat()

    if "today" in p:
        return anchor.date().isoformat()

    if "tomorrow" in p:
        return (anchor + timedelta(days=1)).date().isoformat()

    # explicit weekday name, e.g. "wednesday", "thursday morning"
    for w in WEEKDAYS:
        if w in p or w[:3] in p:
            idx = _weekday_index(w)
            if idx is not None and idx < 5:  # only Mon-Fri matter in this exercise
                target = WEEK_START + timedelta(days=idx)
                return target.date().isoformat()

    return None


def within_exercise_week(date_str: str) -> bool:
    try:
        d = datetime.fromisoformat(date_str)
    except ValueError:
        return False
    return WEEK_START <= d <= WEEK_END
