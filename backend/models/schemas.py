"""Pydantic data models shared across the engine and API layers."""
from __future__ import annotations
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


MessageType = Literal["commitment", "revision", "follow_up", "resolution", "decline", "neutral"]
Bucket = Literal["my_actions", "waiting_on_others", "unclear_ownership"]
Status = Literal["pending", "done", "overdue"]


class Message(BaseModel):
    id: str
    source_type: str  # transcript | email | voice_note
    context: str
    timestamp: str  # ISO datetime
    speaker: str
    recipients: List[str] = []
    text: str
    # filled in by the message classifier
    msg_type: Optional[MessageType] = None


class Commitment(BaseModel):
    """One extracted promise/statement, before dedup."""
    source_message_id: str
    actor: str            # who owns the action (email or name)
    action: str            # short verb phrase, e.g. "send vendor list"
    object: str             # the key noun phrase used for matching, e.g. "vendor list"
    recipient: Optional[str] = None  # who it's owed to, if anyone
    deadline_raw: Optional[str] = None   # as written, e.g. "tomorrow morning"
    deadline_date: Optional[str] = None  # resolved ISO date, e.g. 2026-09-23
    msg_type: MessageType
    timestamp: str


class Task(BaseModel):
    """A deduplicated, merged action item."""
    id: str
    actor: str
    action: str
    object: str
    recipient: Optional[str] = None
    deadline_date: Optional[str] = None
    deadline_history: List[str] = Field(default_factory=list)
    bucket: Bucket
    status: Status                 # final status shown to the user (override wins if set)
    auto_status: Optional[Status] = None  # what the engine inferred from evidence alone
    evidence: List[str] = Field(default_factory=list)       # message ids supporting current state
    candidate_owners: List[str] = Field(default_factory=list)  # people considered/declined ownership
    declined_by: List[str] = Field(default_factory=list)    # people who explicitly said "not me"
    user_override: Optional[Literal["done", "not_done"]] = None
    override_note: Optional[str] = None
    created_at: str
    last_updated: str
    # calendar cross-referencing (see engine/calendar_check.py) — informational only,
    # never affects status/bucket, so a calendar miss can never break the brief
    calendar_context: List[str] = Field(default_factory=list)
    calendar_conflict: Optional[str] = None


class LLMCommitmentRecord(BaseModel):
    """
    One structured record returned by the LLM normalization pass (ai/extract_llm.py)
    for a single thread. Validated strictly before it's allowed to touch the task
    list — this is the grounding contract that keeps the LLM step auditable:
    every record must cite the real message id(s) it was read from.
    """
    topic_key: str
    topic_label: str
    actor: Optional[str] = None          # None => nobody has committed to this yet
    action: Optional[str] = None
    recipient: Optional[str] = None
    deadline_date: Optional[str] = None
    deadline_time: Optional[str] = None  # "HH:MM" or "HH:MM-HH:MM", only when a specific time was said
    resolved: bool = False
    resolving_message_id: Optional[str] = None
    ownership_flag: Literal["claimed", "declined", "flagged_unowned", "none"] = "none"
    declined_by: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)

    def is_grounded(self, valid_message_ids: set) -> bool:
        """Anti-hallucination guard: a record with no evidence, or evidence
        pointing at a message id that doesn't actually exist, is discarded
        rather than trusted."""
        return bool(self.evidence) and all(e in valid_message_ids for e in self.evidence)


class AskRequest(BaseModel):
    question: str
    as_of: Optional[str] = None  # simulated "today", ISO date


class OverrideRequest(BaseModel):
    task_id: str
    value: Literal["done", "not_done"]
    note: Optional[str] = None
    as_of: Optional[str] = None  # the day view the button was actually clicked from
