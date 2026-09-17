"""
Stage 3: Dedup — match, then merge.

Matching: commitments are grouped by a normalized key of (object, actor).
The object taxonomy already gives us a stable key (see extract.py), so
matching here is a simple groupby rather than fuzzy text comparison —
that fuzziness was already resolved when we tagged each message with an
object_key during extraction.

Merging: within a matched group, sort by timestamp and let the LATEST
commitment's deadline win, while keeping every prior value in
deadline_history so the brief can say "originally promised X, now Y".
"""
from __future__ import annotations
from collections import defaultdict
from typing import List, Dict
from models.schemas import Commitment


class MergedCommitmentGroup:
    def __init__(self, object_key: str, actor: str):
        self.object_key = object_key
        self.actor = actor
        self.recipient = None
        self.action = None
        self.deadline_date = None
        self.deadline_history: List[str] = []
        self.evidence: List[str] = []
        self.created_at = None
        self.last_updated = None


def match_and_merge(commitments: List[Commitment]) -> Dict[str, MergedCommitmentGroup]:
    groups: Dict[str, List[Commitment]] = defaultdict(list)
    for c in commitments:
        key = f"{c.object}::{c.actor}"
        groups[key].append(c)

    merged: Dict[str, MergedCommitmentGroup] = {}
    for key, items in groups.items():
        items.sort(key=lambda c: c.timestamp)
        first, last = items[0], items[-1]
        g = MergedCommitmentGroup(object_key=first.object, actor=first.actor)
        g.recipient = first.recipient
        g.action = first.action
        g.created_at = first.timestamp
        g.last_updated = last.timestamp
        g.evidence = [c.source_message_id for c in items]
        for c in items:
            if c.deadline_date and (not g.deadline_history or g.deadline_history[-1] != c.deadline_date):
                g.deadline_history.append(c.deadline_date)
        g.deadline_date = g.deadline_history[-1] if g.deadline_history else None
        merged[key] = g
    return merged
