"""
LLM-based extraction pass — the "narrow LLM slice" that replaces keyword
matching for reading raw messages, while everything downstream (matching,
merging, bucketing, overdue math) stays deterministic (see engine/merge_llm.py).

Design goals (see README / project discussion for the full rationale):

1. Generalization: understands "reschedule our 1:1" vs "reschedule the
   Meridian call" from context, not a fixed keyword list. Understands
   negation, quoted/reported speech, and paraphrased resolutions.
2. Future-proof: works incrementally. Messages are grouped by thread
   (`context`) and each thread's extraction is cached against the exact
   set of message ids it contained last time. A thread is only re-sent to
   the LLM when it has new/changed messages, so cost and latency stay flat
   as more emails/transcripts/calendars are added over time.
3. Consistent across runs: a persisted topic registry (data/topic_registry.json)
   is handed to the LLM on every call so it reuses existing topic_keys
   instead of inventing a new one for a topic it has already seen.
4. Grounded: every record must cite real message ids from its own thread.
   Anything that fails validation (bad JSON, hallucinated id, wrong shape)
   is dropped, never trusted onto the task list.
5. Safe to fail: any error here (bad LLM response, no key configured,
   network error) should let the caller fall back to the deterministic
   keyword pipeline rather than crash the brief.
"""
from __future__ import annotations
import json
import os
import re
import sys
from typing import Dict, List, Optional

from models.schemas import Message, LLMCommitmentRecord
from ai.llm_client import complete

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "topic_registry.json")
CACHE_PATH = os.path.join(DATA_DIR, "extraction_cache.json")

SYSTEM_PROMPT = (
    "You read workplace messages (meeting transcripts, emails, voice-note "
    "transcripts) for one person, Arjun Malhotra, and extract structured "
    "commitment records — who owes what to whom, by when.\n\n"
    "Rules:\n"
    "- Read the WHOLE thread before answering; if a later message revises or "
    "resolves an earlier one, output only the CURRENT state (latest deadline, "
    "correct resolved flag), not one record per message.\n"
    "- The actor is whoever is ACTUALLY committing to the action, even if "
    "someone else is the one speaking or writing (e.g. person A quoting "
    "person B's promise means the actor is B, not A).\n"
    "- actor, recipient, and every entry in declined_by MUST be the exact "
    "email address as it appears in that message's speaker/recipients "
    "fields below — NEVER a person's name, NEVER a first name only, even "
    "if the message itself only uses a name or \"I\"/\"me\"/\"myself\". Look "
    "up the matching email from the speaker/recipients field of the "
    "message you're citing as evidence. Getting this wrong silently merges "
    "or misfiles tasks downstream, so it matters more than anything else "
    "in this schema.\n"
    "- Negation matters: \"I will not be able to send it by Friday\" is NOT a "
    "commitment to Friday — either skip it or represent it honestly (no actor "
    "commitment, or a flagged/at-risk state), never as a positive commitment.\n"
    "- A question or a request TO someone else (\"can you send me the list?\") "
    "is not that speaker's own commitment.\n"
    "- If a topic is discussed but nobody ever actually commits to owning it, "
    "and especially if someone explicitly declines (\"not on my end\", \"not "
    "me\"), output a record with actor=null and ownership_flag set to "
    "\"declined\" or \"flagged_unowned\" as appropriate — do not just omit it.\n"
    "- Reuse an existing topic_key from the registry below if this thread is "
    "about one of those topics. Only invent a new topic_key (short, "
    "lowercase, snake_case) if it's genuinely a different topic.\n"
    "- Resolve relative dates (\"tomorrow\", \"Wednesday\", \"next Monday\") "
    "into absolute ISO dates using each message's own timestamp as the "
    "anchor for what \"today\"/\"tomorrow\" meant when it was said.\n"
    "- Mark resolved=true once the COMMITMENT itself is fulfilled — which, "
    "for a \"confirm/schedule a time\" type commitment, means both sides "
    "explicitly agreeing on a time (e.g. \"Yes, confirmed, see you at 3\"), "
    "NOT that the meeting has already happened by the time you're reading "
    "this. For a \"send/deliver something\" commitment, resolved means "
    "explicit confirming evidence it was sent/delivered/attached. Never "
    "mark resolved=true just because a deadline passed.\n"
    "- Every record MUST include an `evidence` list of the real message ids "
    "(from the ids given below) that support it. Never invent a message id, "
    "a date, a name, or a fact not present in the messages.\n\n"
    "Return ONLY a JSON array of records, no prose, no markdown fences. Each "
    "record: {\"topic_key\": str, \"topic_label\": str, \"actor\": str|null, "
    "\"action\": str|null, \"recipient\": str|null, \"deadline_date\": "
    "\"YYYY-MM-DD\"|null, \"deadline_time\": \"HH:MM\" or \"HH:MM-HH:MM\"|null, "
    "\"resolved\": bool, \"resolving_message_id\": str|null, "
    "\"ownership_flag\": \"claimed\"|\"declined\"|\"flagged_unowned\"|\"none\", "
    "\"declined_by\": [str], \"evidence\": [str]}"
)


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_registry() -> Dict[str, dict]:
    return _load_json(REGISTRY_PATH, {})


def _save_registry(registry: Dict[str, dict]) -> None:
    _save_json(REGISTRY_PATH, registry)


def _load_cache() -> Dict[str, dict]:
    return _load_json(CACHE_PATH, {})


def _save_cache(cache: Dict[str, dict]) -> None:
    _save_json(CACHE_PATH, cache)


def _group_by_thread(messages: List[Message]) -> Dict[str, List[Message]]:
    threads: Dict[str, List[Message]] = {}
    for m in messages:
        threads.setdefault(m.context, []).append(m)
    for ctx in threads:
        threads[ctx].sort(key=lambda m: m.timestamp)
    return threads


def _build_name_lookup(people: Dict[str, dict]) -> Dict[str, str]:
    """lowercase full name / first name / last name -> canonical email.
    Belt-and-suspenders alongside the prompt instruction: the prompt asks
    the LLM to always use the email, but it doesn't always comply (observed
    live: "Arjun Malhotra" instead of his email in some threads, "Divya" in
    others) — actor/recipient equality is exactly what dedup/merge and
    bucket_for() key off, so a name slipping through silently fragments or
    misfiles a task. This normalizes whatever the model actually said back
    to the one true email before it's used for anything."""
    lookup: Dict[str, str] = {}
    for email, info in people.items():
        name = (info.get("name") or "").strip()
        if not name:
            continue
        lookup.setdefault(name.lower(), email)
        parts = name.lower().split()
        if parts:
            lookup.setdefault(parts[0], email)   # first name
        if len(parts) > 1:
            lookup.setdefault(parts[-1], email)  # last name
    return lookup


def _normalize_person(value: Optional[str], lookup: Dict[str, str]) -> Optional[str]:
    if not value:
        return value
    v = value.strip()
    if "@" in v:
        return v.lower()  # already an email
    return lookup.get(v.lower(), v)  # try a name match; keep original if nothing matches


def _normalize_record(rec: LLMCommitmentRecord, lookup: Dict[str, str]) -> LLMCommitmentRecord:
    rec.actor = _normalize_person(rec.actor, lookup)
    rec.recipient = _normalize_person(rec.recipient, lookup)
    rec.declined_by = [_normalize_person(p, lookup) for p in rec.declined_by]
    return rec


def _extract_json_array(raw: str) -> Optional[list]:
    """LLMs sometimes wrap JSON in ```json fences or add a stray sentence —
    pull out the first top-level [...] block rather than failing outright."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _call_llm_for_thread(context: str, thread_msgs: List[Message], registry: Dict[str, dict],
                          relevant_calendar_lines: List[str],
                          name_lookup: Dict[str, str]) -> Optional[List[LLMCommitmentRecord]]:
    registry_block = "\n".join(f"- {k}: {v['label']}" for k, v in registry.items()) or "(none yet)"
    msg_lines = []
    for m in thread_msgs:
        msg_lines.append(
            f'id={m.id} | {m.timestamp} | {m.speaker} -> {", ".join(m.recipients) or "(n/a)"} | "{m.text}"'
        )
    cal_block = "\n".join(relevant_calendar_lines) or "(no related calendar entries)"

    user_msg = (
        f"Thread subject/context: {context}\n\n"
        f"Known topics so far:\n{registry_block}\n\n"
        f"Messages in this thread, chronological:\n" + "\n".join(msg_lines) + "\n\n"
        f"Related calendar entries (for cross-checking meeting/call commitments):\n{cal_block}\n\n"
        "Extract the current-state commitment record(s) for this thread as a JSON array."
    )

    # Bumped from 900: same reasoning-token risk as ai/qa.py when Groq's
    # gpt-oss model is in play (see llm_client.py's reasoning_effort="low"
    # for the primary fix) — this thread's JSON record(s) need headroom on
    # top of whatever reasoning the model does before writing them.
    raw = complete(SYSTEM_PROMPT, user_msg, max_tokens=1400)
    if not raw:
        return None
    parsed = _extract_json_array(raw)
    if parsed is None:
        print(f"[extract_llm] could not parse JSON for thread '{context}': {raw[:200]}", file=sys.stderr)
        return None

    valid_ids = {m.id for m in thread_msgs}
    records: List[LLMCommitmentRecord] = []
    for item in parsed:
        try:
            rec = LLMCommitmentRecord(**item)
        except Exception as e:
            print(f"[extract_llm] dropped malformed record in '{context}': {e}", file=sys.stderr)
            continue
        rec = _normalize_record(rec, name_lookup)
        if not rec.is_grounded(valid_ids):
            print(f"[extract_llm] dropped ungrounded record in '{context}' "
                  f"(evidence not in thread): {rec.evidence}", file=sys.stderr)
            continue
        records.append(rec)
    return records


MAX_PARALLEL_THREAD_CALLS = 5  # bounded so a large history doesn't fire off dozens of concurrent calls at once


def extract_all(messages: List[Message], calendars: Dict[str, list],
                 people: Dict[str, dict]) -> Dict[str, List[LLMCommitmentRecord]]:
    """
    Returns {context: [LLMCommitmentRecord, ...]} for every thread, using the
    per-thread cache so only new/changed threads actually hit the LLM.

    Threads that need a live call are sent CONCURRENTLY (bounded pool) rather
    than one-by-one — on a cold cache this is the single biggest latency
    win, since a thread-by-thread cold run pays every network round-trip
    sequentially. Registry/cache writes still happen back on the main
    thread as each call finishes, so there's no risk of two threads racing
    on the same dict. The one known tradeoff: if two DIFFERENT threads in
    the same batch both introduce the same brand-new topic for the first
    time, they read the registry before either of them has added it, so
    they could independently pick two different topic_keys for it — the
    next run reconciles since both keys are now in the registry and future
    threads about it will match one of them, but this run could briefly
    show it as two tasks. Narrow, one-run-only edge case; not worth losing
    the speed for.

    A single bad thread (parse failure, one dropped record) never blocks the
    rest of the brief — it just contributes nothing for that thread this
    run, and gets retried next time since failures aren't cached. But if
    EVERY thread that actually needed a live call this run failed (API key
    invalid, both providers down, rate-limited everywhere), returning here
    would silently hand back a near-empty brief — worse than just falling
    back. So that specific case raises, letting engine/pipeline.py's
    try/except fall back to the deterministic keyword pipeline instead.
    """
    registry = load_registry()
    cache = _load_cache()
    threads = _group_by_thread(messages)
    name_lookup = _build_name_lookup(people)
    results: Dict[str, List[LLMCommitmentRecord]] = {}
    registry_dirty = False
    cache_dirty = False

    to_process = []  # (context, thread_msgs, signature, cal_lines)
    for context, thread_msgs in threads.items():
        signature = sorted(m.id for m in thread_msgs)
        cached = cache.get(context)
        if cached and cached.get("signature") == signature:
            results[context] = [LLMCommitmentRecord(**r) for r in cached.get("records", [])]
            continue

        people_in_thread = {m.speaker for m in thread_msgs} | {r for m in thread_msgs for r in m.recipients}
        cal_lines = []
        for person in people_in_thread:
            for ev in calendars.get(person, []):
                cal_lines.append(f"{person} | {ev['date']} {ev['start']}-{ev['end']} | {ev['event']}")
        to_process.append((context, thread_msgs, signature, cal_lines))

    attempted = len(to_process)
    failed = 0

    if to_process:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_THREAD_CALLS, len(to_process))) as pool:
            future_to_job = {
                pool.submit(_call_llm_for_thread, context, thread_msgs, registry, cal_lines, name_lookup): (context, signature)
                for context, thread_msgs, signature, cal_lines in to_process
            }
            for future in as_completed(future_to_job):
                context, signature = future_to_job[future]
                try:
                    records = future.result()
                except Exception as e:
                    print(f"[extract_llm] extraction failed for thread '{context}': {e}", file=sys.stderr)
                    records = None

                if records is None:
                    failed += 1
                    results[context] = []  # don't cache a failure — retry next time
                    continue

                results[context] = records
                cache[context] = {"signature": signature, "records": [r.model_dump() for r in records]}
                cache_dirty = True

                for r in records:
                    if r.topic_key not in registry:
                        registry[r.topic_key] = {
                            "label": r.topic_label,
                            "first_seen": context,
                            "last_seen": context,
                        }
                        registry_dirty = True
                    else:
                        registry[r.topic_key]["last_seen"] = context

    if registry_dirty:
        _save_registry(registry)
    if cache_dirty:
        _save_cache(cache)

    if attempted > 0 and failed == attempted:
        raise RuntimeError(
            f"all {attempted} live extraction call(s) failed this run — "
            f"treating the LLM path as unavailable for this build"
        )

    return results


def invalidate_thread(context: str) -> None:
    """Call this if a thread's messages are ever edited/deleted (not just
    appended to) so the next extract_all() call is forced to re-process it."""
    cache = _load_cache()
    if context in cache:
        del cache[context]
        _save_cache(cache)
