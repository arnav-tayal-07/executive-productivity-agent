"""
Deterministic merge stage for the LLM-normalized pipeline. Takes the
per-thread LLMCommitmentRecord lists from ai/extract_llm.py and turns them
into the same Task shape the rest of the app already knows how to render —
matching, merging, bucketing, and status math are all plain code here,
exactly as auditable as the original keyword pipeline.

Unclear ownership is now GENERIC (fixes the stress-test bug where a second
unowned topic besides the Mumbai lease just vanished from the brief): any
topic_key that only ever appears with actor=None across every thread is
surfaced as unclear ownership, regardless of what the topic is.
"""
from __future__ import annotations
from typing import Dict, List, Optional
from datetime import datetime

from models.schemas import Message, Task, LLMCommitmentRecord
from ai.extract_llm import extract_all, load_registry
from engine import calendar_check

ARJUN = "arjun.malhotra@veridian-corp.example"


def bucket_for(actor: str) -> str:
    return "my_actions" if actor == ARJUN else "waiting_on_others"


def _compute_status(resolved: bool, deadline_date: Optional[str], as_of: str) -> str:
    if resolved:
        return "done"
    if not deadline_date:
        return "pending"
    try:
        if datetime.fromisoformat(as_of).date() > datetime.fromisoformat(deadline_date).date():
            return "overdue"
    except ValueError:
        pass
    return "pending"


def build_tasks_llm(as_of: str, messages: List[Message], calendars: Dict[str, list],
                     people: Dict[str, dict]) -> List[Task]:
    by_id = {m.id: m for m in messages}
    records_by_thread = extract_all(messages, calendars, people)
    registry = load_registry()

    # flat list of (context, record) so we can look up evidence timestamps
    flat: List[tuple] = [(ctx, r) for ctx, recs in records_by_thread.items() for r in recs]

    def latest_ts(rec: LLMCommitmentRecord) -> str:
        ts = [by_id[e].timestamp for e in rec.evidence if e in by_id]
        return max(ts) if ts else ""

    def earliest_ts(rec: LLMCommitmentRecord) -> str:
        ts = [by_id[e].timestamp for e in rec.evidence if e in by_id]
        return min(ts) if ts else ""

    # --- owned commitments: group by (topic_key, actor) across ALL threads ---
    owned_groups: Dict[tuple, List[LLMCommitmentRecord]] = {}
    topics_with_owner: set = set()
    for _ctx, r in flat:
        if r.actor:
            owned_groups.setdefault((r.topic_key, r.actor), []).append(r)
            topics_with_owner.add(r.topic_key)

    tasks: List[Task] = []
    for (topic_key, actor), recs in owned_groups.items():
        recs.sort(key=latest_ts)
        latest = recs[-1]
        deadline_history = []
        for r in recs:
            if r.deadline_date and (not deadline_history or deadline_history[-1] != r.deadline_date):
                deadline_history.append(r.deadline_date)
        resolved = any(r.resolved for r in recs)
        resolving_id = next((r.resolving_message_id for r in reversed(recs) if r.resolved and r.resolving_message_id), None)
        evidence = sorted({e for r in recs for e in r.evidence} | ({resolving_id} if resolving_id else set()))
        action = latest.action or next((r.action for r in reversed(recs) if r.action), None) or latest.topic_label
        recipient = latest.recipient or next((r.recipient for r in reversed(recs) if r.recipient), None)
        deadline_date = latest.deadline_date
        topic_label = registry.get(topic_key, {}).get("label", latest.topic_label)

        status = _compute_status(resolved, deadline_date, as_of)
        cal_context = calendar_check.find_matching_events(topic_label, deadline_date, calendars)
        cal_conflict = None
        if not resolved:
            cal_conflict = calendar_check.find_conflicts([actor, recipient], deadline_date, latest.deadline_time, calendars)

        tasks.append(Task(
            id=f"{topic_key}::{actor}",
            actor=actor,
            action=action,
            object=topic_key,
            recipient=recipient,
            deadline_date=deadline_date,
            deadline_history=deadline_history,
            bucket=bucket_for(actor),
            status=status,
            auto_status=status,
            evidence=evidence,
            declined_by=sorted({p for r in recs for p in r.declined_by}),
            created_at=earliest_ts(recs[0]),
            last_updated=latest_ts(latest),
            calendar_context=cal_context,
            calendar_conflict=cal_conflict,
        ))

    # --- unclear ownership: topics that NEVER get a real actor anywhere ---
    unclaimed_groups: Dict[str, List[tuple]] = {}
    for ctx, r in flat:
        if r.actor is None and r.ownership_flag in ("declined", "flagged_unowned"):
            unclaimed_groups.setdefault(r.topic_key, []).append((ctx, r))

    for topic_key, items in unclaimed_groups.items():
        if topic_key in topics_with_owner:
            continue  # someone claimed it elsewhere — not unclear after all
        recs = [r for _ctx, r in items]
        evidence = sorted({e for r in recs for e in r.evidence})
        declined_by = sorted({p for r in recs for p in r.declined_by})
        candidates = sorted({by_id[e].speaker for r in recs for e in r.evidence if e in by_id} - set(declined_by))
        deadline_date = next((r.deadline_date for r in recs if r.deadline_date), None)
        topic_label = registry.get(topic_key, {}).get("label", recs[0].topic_label)
        ts_all = [by_id[e].timestamp for r in recs for e in r.evidence if e in by_id]

        status = _compute_status(False, deadline_date, as_of)
        tasks.append(Task(
            id=f"{topic_key}::unclear",
            actor="unclear",
            action=recs[0].action or topic_label,
            object=topic_key,
            recipient=None,
            deadline_date=deadline_date,
            deadline_history=[deadline_date] if deadline_date else [],
            bucket="unclear_ownership",
            status=status,
            auto_status=status,
            evidence=evidence,
            candidate_owners=candidates,
            declined_by=declined_by,
            created_at=min(ts_all) if ts_all else "",
            last_updated=max(ts_all) if ts_all else "",
            calendar_context=calendar_check.find_matching_events(topic_label, deadline_date, calendars),
        ))

    tasks.sort(key=lambda t: (t.status != "overdue", t.deadline_date or "9999-99-99"))
    return tasks
