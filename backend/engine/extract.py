"""
Stage 2: Extract structured Commitment records from commitment/revision
messages, using an object-keyword taxonomy tuned to this data pack's
five recurring action items. Follow-up and resolution messages don't
create new commitments — they're attached as evidence in later stages.
"""
from __future__ import annotations
import re
from typing import List, Optional
from models.schemas import Message, Commitment
from engine.dates import resolve_date_phrase

# object_key -> (keywords to match in text, human-readable action, default recipient hint)
OBJECT_TAXONOMY = {
    "vendor_list": {
        "keywords": ["vendor list"],
        "action": "send updated vendor list",
        "recipient": "raghav.sethi@veridian-corp.example",
    },
    "campaign_deck": {
        "keywords": ["campaign deck", "the deck", "deck review", "deck's", "deck is"],
        "action": "review Q3 campaign deck",
        "recipient": "arjun.malhotra@veridian-corp.example",
    },
    "meridian_call": {
        "keywords": ["meridian", "priya", "client call", "call reschedule", "reschedule"],
        "action": "confirm call time with Meridian Logistics",
        "recipient": "priya.nair@meridianlogistics.example",
    },
    "expense_report": {
        "keywords": ["expense variance", "variance report", "variance numbers"],
        "action": "deliver July expense variance report",
        "recipient": "arjun.malhotra@veridian-corp.example",
    },
    "mumbai_lease": {
        "keywords": ["mumbai", "lease", "renewal", "sign off", "signature"],
        "action": "sign off Mumbai office lease renewal",
        "recipient": None,
    },
}

# deadline phrase patterns to search for, longest/most specific first
DEADLINE_PHRASES = [
    "tomorrow morning", "tomorrow", "today",
    "wednesday evening", "wednesday morning", "wednesday",
    "thursday morning", "thursday",
    "friday, 25 september", "friday",
    "this week",
]


def combined_text(msg: Message) -> str:
    """Text plus context/subject-line, since a reply often doesn't repeat
    the topic noun but the thread subject (context) still names it —
    the same way a real email subject line carries the topic across replies."""
    return f"{msg.text} {msg.context}".lower()


def _match_object(text_lower: str) -> Optional[str]:
    for key, spec in OBJECT_TAXONOMY.items():
        if any(kw in text_lower for kw in spec["keywords"]):
            return key
    return None


def _find_deadline_phrase(text: str) -> Optional[str]:
    t = text.lower()
    # "shifting to Thursday instead of Wednesday" — strip the OLD value so
    # it doesn't get picked up as if it were still the live deadline
    t = re.sub(r"instead of \w+", "", t)
    for phrase in DEADLINE_PHRASES:
        if phrase in t:
            return phrase
    return None


def _determine_actor(msg: Message, object_key: str) -> str:
    """
    Who is committing to the action. Usually the speaker, except when the
    speaker is reporting someone ELSE's earlier commitment (rare in this
    data pack, kept simple: actor = speaker for commitment/revision msgs).
    """
    return msg.speaker


def extract_commitments(messages: List[Message]) -> List[Commitment]:
    """
    messages must already be chronologically sorted (normalize.load_messages
    guarantees this). We walk them in order and track the last object_key
    seen per `context` (thread/meeting), so a reply like "Yes, I'll have it
    ready Wednesday evening" that doesn't repeat the topic noun can still be
    tied to the right task via topic carry-over within the same thread —
    the same way a person reading the thread infers what "it" refers to.
    """
    commitments: List[Commitment] = []
    last_object_by_context: dict[str, str] = {}

    for msg in messages:
        text_lower = combined_text(msg)
        direct_match = _match_object(text_lower)
        if direct_match:
            last_object_by_context[msg.context] = direct_match

        if msg.msg_type not in ("commitment", "revision"):
            continue

        object_key = direct_match or last_object_by_context.get(msg.context)
        if object_key is None:
            continue  # general statement, nothing concrete to track

        spec = OBJECT_TAXONOMY[object_key]
        deadline_phrase = _find_deadline_phrase(msg.text)
        deadline_date = resolve_date_phrase(deadline_phrase, msg.timestamp) if deadline_phrase else None

        recipient = spec["recipient"]
        # a lease commitment has no fixed recipient — ownership is what's undetermined
        actor = _determine_actor(msg, object_key)

        commitments.append(Commitment(
            source_message_id=msg.id,
            actor=actor,
            action=spec["action"],
            object=object_key,
            recipient=recipient,
            deadline_raw=deadline_phrase,
            deadline_date=deadline_date,
            msg_type=msg.msg_type,
            timestamp=msg.timestamp,
        ))
    return commitments
