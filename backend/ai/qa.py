"""
Q&A layer. The LLM (when available) is grounded strictly on the
already-deduplicated Task list — it never sees raw transcripts/emails
directly, so it cannot invent a fact that isn't in the structured data.

Without an API key, a small keyword-based fallback handles the two
question shapes the assignment calls out explicitly ("what did I
promise X" / "what needs action today") so the app still answers
end-to-end offline.

Unlike LLM extraction (ai/extract_llm.py), which caches per-thread to
disk, every /ask call used to hit the LLM fresh — including literal
repeats of the same question against unchanged data (very common while
manually testing/demoing the "Ask the agent" box, or clicking a quick
question chip more than once). That's pure wasted quota, so answers are
cached in-process, keyed on (question, as_of, a hash of the current task
list). It's intentionally NOT persisted to disk or shared across
processes — a restart or a genuinely new task set always asks fresh.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timedelta
from typing import List
from models.schemas import Task
from ai.llm_client import complete, available

PEOPLE_NAMES = {
    "arjun.malhotra@veridian-corp.example": "Arjun",
    "neha.kapoor@veridian-corp.example": "Neha",
    "raghav.sethi@veridian-corp.example": "Raghav",
    "divya.rao@veridian-corp.example": "Divya",
    "priya.nair@meridianlogistics.example": "Priya",
    "unclear": "Unowned",
}
ARJUN = "arjun.malhotra@veridian-corp.example"

SYSTEM_PROMPT = (
    "You are the Q&A layer of an Executive Productivity Agent for Arjun Malhotra. "
    "You will be given a JSON list of already-deduplicated task records and a question. "
    "Answer ONLY using the given task records — never invent a fact, name, date, or status "
    "that isn't present in the data. If the data doesn't answer the question, say so plainly. "
    "Keep the answer to 1-3 short sentences, in a direct, brief tone.\n\n"
    "Each task has a 'bucket': 'my_actions' (Arjun owes someone else), "
    "'waiting_on_others' (someone else owes Arjun), or 'unclear_ownership' (no one has "
    "confirmed ownership yet). When a question asks what is due/left/outstanding on a given "
    "day (today, tomorrow, a date), or asks generally what's outstanding, you MUST check "
    "'deadline_date' across ALL THREE buckets, not just 'my_actions' — an unclear_ownership "
    "task with a matching, unresolved (non-'done') deadline is still something Arjun is "
    "tracking on his own board and needs to mention, even though no one owns it yet. Do not "
    "silently restrict 'what do I have due' to tasks where actor is Arjun.\n\n"
    "A task with status 'overdue' is still open and unresolved — its deadline has simply "
    "already passed. When a question asks about a specific day and nothing has a deadline "
    "that literally matches that day, do NOT just say 'nothing is due/pending' and stop "
    "there: separately flag any overdue task (any bucket) that is still outstanding. Keep "
    "the literal answer about that day and the overdue flag clearly distinct (e.g. \"Nothing "
    "is due tomorrow specifically, but note <task> is still overdue from <date>\") — never "
    "imply the person has nothing left simply because nothing matches the exact day asked "
    "about or because a matching task isn't in 'my_actions'."
)


def _label(email_or_key: str) -> str:
    return PEOPLE_NAMES.get(email_or_key, email_or_key)


def _fallback_answer(question: str, tasks: List[Task], as_of: str) -> str:
    q = question.lower()

    for email, name in PEOPLE_NAMES.items():
        if name.lower() in q and ("promise" in q or "owe" in q):
            mine = [t for t in tasks if t.actor == ARJUN and t.recipient == email]
            if not mine:
                return f"You have no open commitments to {name} right now."
            lines = [f"{t.action} (status: {t.status}, deadline: {t.deadline_date or 'unspecified'})" for t in mine]
            return f"You promised {name}: " + "; ".join(lines)

    # "today" and "tomorrow" must resolve to DIFFERENT dates, not both fall
    # through to the same as_of-based filter — that was a real bug: asking
    # for tomorrow returned today's (already-overdue) items.
    if "tomorrow" in q:
        target = (datetime.fromisoformat(as_of) + timedelta(days=1)).date().isoformat()
        due = [t for t in tasks if t.status == "pending" and t.deadline_date == target]
        # overdue items are still open regardless of what day is being asked
        # about — don't let a literal "nothing matches tomorrow's date" read
        # as "you have nothing left".
        overdue = [t for t in tasks if t.status == "overdue" and t.actor == ARJUN]
        overdue_note = ""
        if overdue:
            lines = [f"{t.action} (owed to {_label(t.recipient)}, overdue since {t.deadline_date})" for t in overdue]
            overdue_note = " Note: you still have an overdue item outstanding — " + "; ".join(lines) + "."
        if not due:
            return f"Nothing is due tomorrow ({target})." + overdue_note
        lines = [f"{_label(t.actor)}: {t.action}" for t in due]
        return f"Due tomorrow ({target}): " + "; ".join(lines) + "." + overdue_note

    if "today" in q or "action" in q:
        due = [t for t in tasks if t.status in ("pending", "overdue") and (t.deadline_date == as_of or t.status == "overdue")]
        if not due:
            return "Nothing needs action today."
        lines = [f"{_label(t.actor)}: {t.action} ({t.status})" for t in due]
        return "Needs action: " + "; ".join(lines)

    return "I can currently answer questions like 'What did I promise <name>?' or 'What needs action today?' — try rephrasing along those lines."


_answer_cache: dict[str, str] = {}
MAX_CACHE_ENTRIES = 500  # simple bound so a long-running dev process can't grow this unboundedly


def _cache_key(question: str, tasks: List[Task], as_of: str) -> str:
    # Task list is already the deduplicated/merged output (stable field
    # order per Task, since model_dump() follows the schema's declared
    # field order), so a plain json dump is a fine cache-busting signature
    # — any real change to status/overrides/evidence changes the hash.
    payload = [t.model_dump() for t in tasks]
    raw = json.dumps({"q": question.strip().lower(), "as_of": as_of, "tasks": payload}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def ask(question: str, tasks: List[Task], as_of: str) -> str:
    key = _cache_key(question, tasks, as_of)
    if key in _answer_cache:
        return _answer_cache[key]

    if available():
        payload = [t.model_dump() for t in tasks]
        user_msg = f"as_of date: {as_of}\n\nTasks:\n{json.dumps(payload, indent=2)}\n\nQuestion: {question}"
        # 700, not 300: gpt-oss-120b (Groq) is a reasoning model whose
        # thinking tokens count against this budget, and 300 was observed
        # live to get eaten entirely by reasoning, truncating the visible
        # answer to a single word. reasoning_effort=low (llm_client.py)
        # is the primary fix; this is a safety margin on top of it.
        answer = complete(SYSTEM_PROMPT, user_msg, max_tokens=700)
        if answer:
            answer = answer.strip()
            _store_answer(key, answer)
            return answer

    answer = _fallback_answer(question, tasks, as_of)
    # cache the offline fallback too — cheap to skip re-computing, and
    # keeps behavior identical if a later call hits this same key
    _store_answer(key, answer)
    return answer


def _store_answer(key: str, answer: str) -> None:
    if len(_answer_cache) >= MAX_CACHE_ENTRIES:
        _answer_cache.pop(next(iter(_answer_cache)))  # drop oldest (insertion order)
    _answer_cache[key] = answer
