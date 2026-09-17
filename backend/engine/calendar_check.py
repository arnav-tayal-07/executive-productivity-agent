"""
Deterministic calendar cross-referencing — pure interval/keyword arithmetic,
no LLM involved. Kept as plain code (not part of the LLM pass) because this
is exactly the kind of check that's already structured data: it should be
100% reliable, the same way date-overdue math is.

Two things this does:
  1. find_matching_events  — informational: does a calendar entry already
     exist for this task's topic (e.g. the "Call — Meridian Logistics" slot),
     so the brief can show it alongside the task as corroborating context.
  2. find_conflicts        — a warning: if a task implies a specific new
     time (deadline_time is set) and nobody's confirmed it yet, does that
     time collide with a "Blocked" slot on the relevant people's calendars.

Both are best-effort and never affect status/bucket — a calendar miss just
means no context/warning is attached, it never blocks or breaks a task.
"""
from __future__ import annotations
import re
from datetime import datetime, time
from typing import Dict, List, Optional

STOPWORDS = {"the", "a", "an", "of", "for", "to", "with", "and", "on", "in", "at"}


def _significant_words(label: str) -> set:
    words = re.findall(r"[a-z0-9]+", label.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def _parse_time(t: str) -> Optional[time]:
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(t.strip(), fmt).time()
        except ValueError:
            continue
    return None


def _parse_range(deadline_time: str) -> Optional[tuple]:
    """'15:00' -> (15:00, 15:30 default 30min); '15:00-15:30' -> (15:00, 15:30)."""
    if not deadline_time:
        return None
    if "-" in deadline_time:
        start_s, end_s = deadline_time.split("-", 1)
        start, end = _parse_time(start_s), _parse_time(end_s)
    else:
        start = _parse_time(deadline_time)
        end = None
    if not start:
        return None
    if not end:
        end_dt = datetime.combine(datetime.today(), start)
        end_dt = end_dt.replace(minute=(end_dt.minute + 30) % 60,
                                 hour=end_dt.hour + (1 if end_dt.minute + 30 >= 60 else 0))
        end = end_dt.time()
    return (start, end)


def find_matching_events(topic_label: str, date: Optional[str], calendars: Dict[str, list]) -> List[str]:
    """Any calendar entry, for any person, on `date`, whose event name shares
    a significant word with the topic label. Returned as display strings."""
    if not date:
        return []
    keywords = _significant_words(topic_label)
    if not keywords:
        return []
    matches = []
    for person, events in calendars.items():
        for ev in events:
            if ev.get("date") != date:
                continue
            ev_words = _significant_words(ev.get("event", ""))
            if keywords & ev_words:
                matches.append(f"{person.split('@')[0]}: {ev['event']} ({ev['date']} {ev['start']}-{ev['end']})")
    return matches


def find_conflicts(people: List[str], date: Optional[str], deadline_time: Optional[str],
                    calendars: Dict[str, list]) -> Optional[str]:
    """If a specific time is implied and it overlaps a Blocked slot on any of
    the given people's calendars, return a human-readable warning string."""
    if not date or not deadline_time:
        return None
    rng = _parse_range(deadline_time)
    if not rng:
        return None
    start, end = rng

    for person in people:
        if not person:
            continue
        for ev in calendars.get(person, []):
            if ev.get("date") != date:
                continue
            ev_start, ev_end = _parse_time(ev["start"]), _parse_time(ev["end"])
            if not ev_start or not ev_end:
                continue
            if start < ev_end and ev_start < end:  # interval overlap
                who = person.split("@")[0]
                return (f"Proposed time {deadline_time} on {date} overlaps {who}'s "
                        f"\"{ev['event']}\" ({ev['start']}-{ev['end']})")
    return None
