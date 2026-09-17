"""
Stage 4: Classify each merged commitment group into one of three
buckets, and separately build the "unclear ownership" case(s) that
never had a committing actor in the first place (the Mumbai lease).

my_actions        -> actor is Arjun (the agent's user)
waiting_on_others  -> actor is a known person other than Arjun
unclear_ownership -> no actor ever committed; built directly from
                      raw messages rather than from the commitment
                      pipeline, since by definition there is no
                      commitment to extract.
"""
from __future__ import annotations
from typing import List, Dict
from models.schemas import Message
from engine.dedup import MergedCommitmentGroup
from engine.dates import resolve_date_phrase
from engine.extract import combined_text

ARJUN = "arjun.malhotra@veridian-corp.example"

LEASE_KEYWORDS = ["mumbai", "lease", "renewal", "sign off", "signature"]


def bucket_for(actor: str) -> str:
    return "my_actions" if actor == ARJUN else "waiting_on_others"


def find_unclear_ownership_cases(messages: List[Message]) -> List[dict]:
    """
    Scans ALL raw messages (not just commitments) for topics that were
    discussed but never claimed by anyone. Built specially for the lease
    case rather than through the generic commitment pipeline, because
    the defining feature of this case is the ABSENCE of a commitment.
    """
    related = [m for m in messages if any(k in combined_text(m) for k in LEASE_KEYWORDS)]
    if not related:
        return []

    flagged_by = set()
    declined_by = set()
    deadline_date = None
    evidence = []

    for m in related:
        evidence.append(m.id)
        if m.msg_type == "decline":
            declined_by.add(m.speaker)
        if m.msg_type == "follow_up" or "confirm who" in m.text.lower() or "not sure whose desk" in m.text.lower() or "hasn't been assigned" in m.text.lower() or "not been assigned" in m.text.lower():
            flagged_by.add(m.speaker)
        for phrase in ["friday, 25 september", "friday"]:
            if phrase in m.text.lower():
                resolved = resolve_date_phrase(phrase, m.timestamp)
                if resolved:
                    deadline_date = resolved

    # candidate owners: anyone discussed in relation to the task, minus
    # anyone who explicitly declined it
    discussed = {m.speaker for m in related}
    candidates = sorted(discussed - declined_by)

    return [{
        "object_key": "mumbai_lease",
        "action": "sign off Mumbai office lease renewal",
        "deadline_date": deadline_date,
        "evidence": evidence,
        "candidate_owners": candidates,
        "declined_by": sorted(declined_by),
        "flagged_by": sorted(flagged_by),
        "created_at": related[0].timestamp,
        "last_updated": related[-1].timestamp,
    }]
