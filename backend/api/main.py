"""
FastAPI app for the Executive Productivity Agent.

Endpoints:
  GET  /brief?as_of=YYYY-MM-DD   -> daily brief, grouped into 3 buckets
  POST /ask                       -> {question, as_of} -> {answer}
  POST /override                  -> {task_id, value, note} -> updated task
  POST /messages                   -> append a new message (email/transcript/
                                       voice note) and pick it up on the next
                                       /brief call — only its thread gets
                                       reprocessed when the LLM path is on
                                       (see ai/extract_llm.py's per-thread cache)
  POST /calendar-events            -> append a calendar event for a person
  GET  /health                    -> simple liveness check

Run: uvicorn api.main:app --reload --port 8000   (from the backend/ folder)
"""
from __future__ import annotations
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()  # must run before ai.llm_client reads GROQ_API_KEY / GEMINI_API_KEY from os.environ

from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models.schemas import AskRequest, OverrideRequest, Task, Message
from engine.pipeline import build_tasks, apply_override, DEFAULT_AS_OF, used_llm_extraction
from engine.normalize import DATA_PATH, classify_message_type
from ai.qa import ask as ask_llm
from ai.llm_client import available as llm_available, last_used_provider, last_used_model

app = FastAPI(title="Executive Productivity Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# simple in-memory stores. Overrides are kept separately, keyed by task_id,
# and re-applied to every as_of view (not just the one open when the
# override was made) — a manual "done" should stick regardless of which
# day's brief you're looking at.
_task_cache: Dict[str, List[Task]] = {}
_overrides: Dict[str, dict] = {}  # task_id -> {"value": ..., "note": ...}


def _get_tasks(as_of: str) -> List[Task]:
    if as_of not in _task_cache:
        tasks = build_tasks(as_of)
        for t in tasks:
            if t.id in _overrides:
                apply_override(tasks, t.id, _overrides[t.id]["value"], _overrides[t.id]["note"])
        _task_cache[as_of] = tasks
    return _task_cache[as_of]


@app.get("/health")
def health():
    return {
        "status": "ok",
        "llm_available": llm_available(),
        "llm_last_used_provider": last_used_provider(),  # which provider answered most recently (shows failover)
        "llm_last_used_model": last_used_model(),  # e.g. confirms a lighter fallback model kicked in
        "extraction_method": "llm" if used_llm_extraction() else "keyword",  # which pipeline built the last brief
    }


@app.get("/brief")
def brief(as_of: str = DEFAULT_AS_OF):
    tasks = _get_tasks(as_of)
    grouped = {"my_actions": [], "waiting_on_others": [], "unclear_ownership": []}
    for t in tasks:
        grouped[t.bucket].append(t.model_dump())
    return {
        "as_of": as_of,
        "extraction_method": "llm" if used_llm_extraction() else "keyword",
        **grouped,
    }


@app.post("/ask")
def ask(req: AskRequest):
    as_of = req.as_of or DEFAULT_AS_OF
    tasks = _get_tasks(as_of)
    answer = ask_llm(req.question, tasks, as_of)
    return {
        "answer": answer,
        "as_of": as_of,
        "llm_available": llm_available(),
        "llm_provider": last_used_provider(),  # None means the offline keyword fallback answered
        "llm_model": last_used_model(),
    }


@app.post("/override")
def override(req: OverrideRequest):
    # Validate against the SAME as_of view the button was actually clicked
    # from (the frontend now sends it), not a hardcoded DEFAULT_AS_OF.
    #
    # Why this matters now and didn't before: on the old fully-deterministic
    # pipeline, task ids were identical across every as_of, so checking
    # against any fixed date was always safe. On the LLM extraction path,
    # checking a date that hasn't been loaded yet forces a brand new
    # build_tasks() call inside this request — which, under any transient
    # flakiness, could take a while or (in a worst case) resolve a task's
    # topic/actor slightly differently than the view the user is actually
    # looking at, so the id the button sent wouldn't be found. Checking the
    # caller's own as_of means this call almost always just reads a view
    # that's already cached, since the frontend just loaded it.
    as_of = req.as_of or DEFAULT_AS_OF
    tasks = _get_tasks(as_of)
    if not any(t.id == req.task_id for t in tasks):
        raise HTTPException(status_code=404, detail=f"task '{req.task_id}' not found in as_of={as_of}")

    _overrides[req.task_id] = {"value": req.value, "note": req.note}
    # re-apply to every already-cached as_of view so it's consistent everywhere immediately
    for cached_tasks in _task_cache.values():
        apply_override(cached_tasks, req.task_id, req.value, req.note)
    return {"ok": True}


class NewMessageRequest(BaseModel):
    id: str
    source_type: str          # transcript | email | voice_note
    context: str               # thread/subject — same context string as any
                                # earlier message in this thread, so it gets
                                # tied to that thread rather than starting a new one
    timestamp: str
    speaker: str
    recipients: List[str] = []
    text: str


class NewCalendarEventRequest(BaseModel):
    person: str                # email — must match an existing person key
    date: str
    start: str
    end: str
    event: str


def _load_raw_data() -> dict:
    with open(DATA_PATH) as f:
        return json.load(f)


def _save_raw_data(data: dict) -> None:
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_PATH)


@app.post("/messages")
def add_message(req: NewMessageRequest):
    """
    Append a new email/transcript/voice-note message — the point of the
    incremental design: this does NOT trigger a full re-analysis. On the
    LLM path, only the thread this message belongs to (matched by `context`)
    gets re-sent to the LLM on the next /brief call; every other thread is
    served from cache. On the keyword path it's just picked up naturally
    since that pipeline is already cheap enough to rerun in full.
    """
    data = _load_raw_data()
    if any(m["id"] == req.id for m in data["messages"]):
        raise HTTPException(status_code=400, detail=f"message id '{req.id}' already exists")

    msg = req.model_dump()
    data["messages"].append(msg)
    _save_raw_data(data)

    _task_cache.clear()  # stale until the affected thread(s) are reprocessed
    return {"ok": True, "id": req.id, "msg_type": classify_message_type(req.text)}


@app.post("/calendar-events")
def add_calendar_event(req: NewCalendarEventRequest):
    data = _load_raw_data()
    if req.person not in data.get("people", {}):
        raise HTTPException(status_code=404, detail=f"unknown person '{req.person}'")

    data.setdefault("calendars", {}).setdefault(req.person, []).append({
        "date": req.date, "start": req.start, "end": req.end, "event": req.event,
    })
    _save_raw_data(data)

    _task_cache.clear()  # a new calendar entry can change conflict warnings
    return {"ok": True}
