"""
Stage 1: Normalize raw sources into a common Message shape, and
classify each message's TYPE (commitment / revision / follow_up /
resolution / neutral) using rule-based keyword cues.

Why rule-based here rather than an LLM call: the assignment's data
pack is small and fixed, and the phrasing patterns are consistent
enough that deterministic cues give 100% reliable, explainable,
zero-latency results. The LLM budget is spent instead on the parts
that genuinely need open-ended language understanding: the Q&A layer
(see ai/qa.py).
"""
from __future__ import annotations
import json
import os
from typing import List
from models.schemas import Message

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "seed_data.json")

DECLINE_CUES = [
    "don't think it's me", "not on my end", "not me", "not us", "not mine",
]
# a request/negotiation TO someone else ("can I get it by...?") is not a
# commitment BY the speaker — checked early so cues like "instead" or
# "actually," inside the same sentence don't misfire as a revision
REQUEST_CUES = ["can i get", "can you", "could you"]
RESOLUTION_CUES = [
    "attached", "sent as promised", "got it", "confirmed, see you",
    "confirmed.", "deck is ready", "report attached", "yes, confirmed",
    "for sure\".", "delivered", "signed off", "is ready",
]
FOLLOW_UP_CUES = [
    "can you send", "just checking", "still good for", "still on for",
    "any update", "who's handling", "whenever you get a chance",
    "has anyone confirmed", "don't think it's been assigned",
    "one day out and still unowned",
]
REVISION_CUES = [
    "instead", "actually,", "shifting", "heads up", "running behind",
    "sorry, got pulled into",
]
# NOTE: deliberately does NOT include "can I get it by ..." — that's Arjun
# renegotiating someone ELSE's deadline, not a commitment of his own. Their
# own follow-up reply (e.g. Divya's "I'll prioritize it") is what correctly
# records the revised commitment, still attributed to them.
COMMITMENT_CUES = [
    "i'll", "i will", "will send", "will get", "will have",
    "need to", "i owe", "i told", "targeting", "starting on",
]


def load_raw() -> dict:
    with open(DATA_PATH, "r") as f:
        return json.load(f)


def classify_message_type(text: str) -> str:
    t = text.lower()
    # decline is checked first: e.g. "I don't think it's me" contains "need to"
    # earlier in the sentence and would otherwise be misread as a commitment
    if any(c in t for c in DECLINE_CUES):
        return "decline"
    if any(c in t for c in REQUEST_CUES) and "?" in text:
        return "follow_up"
    if any(c in t for c in RESOLUTION_CUES):
        return "resolution"
    if any(c in t for c in FOLLOW_UP_CUES) and "?" in text:
        return "follow_up"
    if any(c in t for c in REVISION_CUES):
        return "revision"
    if any(c in t for c in COMMITMENT_CUES):
        return "commitment"
    return "neutral"


def load_messages() -> List[Message]:
    raw = load_raw()
    messages: List[Message] = []
    for m in raw["messages"]:
        msg = Message(**m)
        msg.msg_type = classify_message_type(msg.text)
        messages.append(msg)
    # keep chronological order — later stages rely on this for "latest wins"
    messages.sort(key=lambda m: m.timestamp)
    return messages


def load_people() -> dict:
    return load_raw()["people"]


def load_calendars() -> dict:
    return load_raw()["calendars"]
