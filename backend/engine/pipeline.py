"""
Orchestrates the full pipeline and returns the final list of Task records
for a given simulated "today" (as_of). This is the single function the API
layer calls.

Two extraction paths, chosen automatically:
  - LLM path (engine/merge_llm.py + ai/extract_llm.py): used whenever a
    Groq/Gemini key is configured. Reads each thread with an LLM to
    generalize past the fixed keyword taxonomy — handles new topics,
    negation, quoted speech, paraphrased resolutions, and cross-references
    calendars. Cached per-thread so re-runs only reprocess changed threads.
  - Keyword path (normalize -> extract -> dedup -> classify -> status,
    below): the original deterministic pipeline. Always available, zero
    latency, zero API dependency — used when no LLM key is set, AND as an
    automatic fallback if the LLM path raises for any reason, so a live
    demo never breaks because of a flaky API call.

`build_tasks()` also returns which path actually ran (`used_llm`) so the
API/frontend can show it, the same way the Q&A layer shows which provider
answered.
"""
from __future__ import annotations
from typing import List, Optional
from datetime import datetime

from models.schemas import Task
from engine.normalize import load_messages, load_calendars, load_people
from engine.extract import extract_commitments
from engine.dedup import match_and_merge
from engine.classify import bucket_for, find_unclear_ownership_cases, ARJUN
from engine.status import compute_status
from ai.llm_client import available as llm_available

DEFAULT_AS_OF = "2026-09-25"  # Friday — end of the exercise week

_last_used_llm: bool = False


def used_llm_extraction() -> bool:
    """Which path the most recent build_tasks() call actually used —
    for /health and demoing the fallback live."""
    return _last_used_llm


def build_tasks(as_of: Optional[str] = None) -> List[Task]:
    global _last_used_llm
    as_of = as_of or DEFAULT_AS_OF

    if llm_available():
        try:
            from engine.merge_llm import build_tasks_llm
            messages = load_messages()
            calendars = load_calendars()
            people = load_people()
            tasks = build_tasks_llm(as_of, messages, calendars, people)
            _last_used_llm = True
            return tasks
        except Exception as e:
            import sys
            print(f"[pipeline] LLM extraction path failed, falling back to keyword "
                  f"pipeline: {e}", file=sys.stderr)

    _last_used_llm = False
    return _build_tasks_keyword(as_of)


def _build_tasks_keyword(as_of: str) -> List[Task]:
    """The original deterministic keyword-taxonomy pipeline — unchanged."""
    messages = load_messages()

    commitments = extract_commitments(messages)
    merged_groups = match_and_merge(commitments)

    tasks: List[Task] = []

    for key, g in merged_groups.items():
        if g.object_key == "mumbai_lease":
            continue  # handled separately below — never has a committing actor
        status, auto_evidence_id = compute_status(g.object_key, g.deadline_date, as_of, messages)
        evidence = list(g.evidence)
        if auto_evidence_id and auto_evidence_id not in evidence:
            evidence.append(auto_evidence_id)
        tasks.append(Task(
            id=key,
            actor=g.actor,
            action=g.action,
            object=g.object_key,
            recipient=g.recipient,
            deadline_date=g.deadline_date,
            deadline_history=g.deadline_history,
            bucket=bucket_for(g.actor),
            status=status,
            auto_status=status,
            evidence=evidence,
            created_at=g.created_at,
            last_updated=g.last_updated,
        ))

    for case in find_unclear_ownership_cases(messages):
        status, auto_evidence_id = compute_status(case["object_key"], case["deadline_date"], as_of, messages)
        evidence = list(case["evidence"])
        if auto_evidence_id and auto_evidence_id not in evidence:
            evidence.append(auto_evidence_id)
        tasks.append(Task(
            id=f"{case['object_key']}::unclear",
            actor="unclear",
            action=case["action"],
            object=case["object_key"],
            recipient=None,
            deadline_date=case["deadline_date"],
            deadline_history=[case["deadline_date"]] if case["deadline_date"] else [],
            bucket="unclear_ownership",
            status=status,
            auto_status=status,
            evidence=evidence,
            candidate_owners=case["candidate_owners"],
            declined_by=case["declined_by"],
            created_at=case["created_at"],
            last_updated=case["last_updated"],
        ))

    tasks.sort(key=lambda t: (t.status != "overdue", t.deadline_date or "9999-99-99"))
    return tasks


def apply_override(tasks: List[Task], task_id: str, value: str, note: Optional[str]) -> List[Task]:
    for t in tasks:
        if t.id == task_id:
            t.user_override = value
            t.override_note = note or "confirmed by user, no textual evidence found"
            t.status = "done" if value == "done" else t.auto_status
    return tasks
