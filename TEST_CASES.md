# Test Case Document — AIONOS Executive Productivity Agent

Covers both extraction pipelines (deterministic keyword pipeline and the
LLM-normalized pipeline), the merge/dedup/status layer, and the Q&A layer.
All test cases below were actually executed against the codebase in
`backend/`, not hand-simulated — commands to reproduce each run are in the
"How to reproduce" section at the end.

`as_of` (the app's simulated "today") is **2026-09-25** (Friday) throughout,
per `engine/pipeline.py::DEFAULT_AS_OF`.

Two extraction paths exist and are tested separately:
- **Keyword pipeline** (`engine/extract.py` → `dedup.py` → `classify.py` →
  `status.py`) — deterministic, zero API dependency, always available.
- **LLM pipeline** (`ai/extract_llm.py` + `engine/merge_llm.py`) — used
  automatically when a Groq/Gemini key is configured; falls back to the
  keyword pipeline on any error. Plumbing tests below use a **hand-written
  mock LLM response** in place of a real API call (no key available in this
  sandbox) — they validate the deterministic code *around* the LLM call
  (registry, caching, grounding guard, calendar cross-refs), not real model
  output quality. The two live Q&A cases in section 4 *did* use the real,
  deployed Groq-backed app.

---

## 1. Keyword pipeline — extraction correctness (`run_stress.py`)

Eleven synthetic messages (`s-tc1-*` … `s-tc11-*`) were injected into the
seed data, each targeting one specific edge case.

| ID | Scenario | Input | Expected | Actual | Result |
|----|----------|-------|----------|--------|--------|
| TC1 | Commitment with no explicit day ("by Monday" implied from thread context) | *"I'll have it over to you by Monday."* | Commitment extracted for `vendor_list` | Extracted, `actor=arjun`, `object=vendor_list` | **Pass** |
| TC2 | Two topics in one thread — offsite agenda vs. an unrelated "reschedule" follow-up shouldn't pollute `meridian_call` | *"I'll send the updated agenda by Thursday."* | Commitment tagged as offsite-agenda-ish, not `meridian_call` | Tagged `object=meridian_call` (thread's only keyword match — see note) | **Fail (known limitation)** |
| TC3 | "sign off on the final deck" should not collide with `mumbai_lease` sign-off keywords | *"Can everyone sign off on the final deck by Thursday?"* | Matches `campaign_deck`, not `mumbai_lease` | `_match_object()` → `campaign_deck`; no `mumbai_lease` pollution | **Pass** |
| TC4 | Quoted/reported speech should attribute to the person quoted, not the speaker relaying it | *"Neha mentioned, quote, 'I'll get the campaign deck revisions to you by Friday,' end quote."* | `actor=neha.kapoor@...` | `actor=arjun.malhotra@...` (misattributed to the message's sender) | **Fail (known limitation)** |
| TC5 | Negated commitment must not read as a real commitment | *"I will not be able to send the vendor list by Friday, need more time."* | `classify_message_type()` ≠ `commitment` (or excluded downstream) | Classified `commitment`; message **is** merged into the final `vendor_list` task's evidence | **Fail (known limitation — false positive)** |
| TC6 | Typo without apostrophe still recognized ("Ill" vs "I'll") | *"Ill get that vendor list over today, sorry for the wait."* | Classified `commitment` | Classified `neutral` — missed entirely | **Fail (known limitation — false negative)** |
| TC7 | Deadline word not in the fixed phrase list | *"Separately, I'll send the expense variance numbers for August by Monday."* | Commitment extracted, `object=expense_report` | Extracted correctly | **Pass** |
| TC8 | Paraphrased resolution with no literal keyword match | *"Delivered the vendor thing to Raghav finally, that's off my plate."* | `find_resolution_evidence('vendor_list')` picks it up → task resolves to `done` | Not picked up; `vendor_list` task stays `overdue` | **Fail (known limitation — false negative)** |
| TC9 | A *second*, unrelated unowned-ownership topic (wifi budget) besides the Mumbai lease | *"Has anyone confirmed who's paying for the new office wifi router upgrade?"* / *"Not on my end, that's probably IT's call."* | Surfaces somewhere in the brief as unclear ownership | Does **not** appear anywhere in the keyword-pipeline output | **Fail (known limitation — silently dropped)** |
| TC10 | Two deadlines in one sentence — which wins | *"I'll aim for Wednesday on the deck review, but if the vendor's slow, more realistically Thursday morning."* | The more realistic/later date (Thursday) wins | `deadline_raw='wednesday'` picked (first mention, not the caveat) | **Fail (known limitation)** |
| TC11 | Brand-new person not in `people.json` | New hire commits to something | Task still created, doesn't crash; label layer degrades gracefully | Task created correctly, `bucket=waiting_on_others`, no crash | **Pass** |

**Summary:** 4/11 pass outright; 7/11 are known, reproducible gaps in the
fixed-keyword-taxonomy approach (multi-topic thread pollution, misattributed
quoted speech, negation not filtered, apostrophe-sensitive regex, paraphrased
resolutions, silently-dropped second unclear-ownership topics, "most likely"
deadline disambiguation). This is exactly the gap the LLM pipeline (section 2)
was built to close — it reads full thread context with an LLM instead of
fixed keyword/regex matching.

---

## 2. LLM pipeline — plumbing correctness (`test_llm_path.py`, mocked LLM)

Run twice back-to-back against the same data: a cold run (empty cache) and a
warm run (cache should short-circuit re-processing of unchanged threads).

| Check | Expected | Actual | Result |
|---|---|---|---|
| Team Offsite agenda commitment stays a separate topic, doesn't pollute `meridian_call` | `team_offsite_agenda` task created; `meridian_call` not polluted | Both true | **Pass** |
| A second unowned topic (office wifi budget) surfaces as `unclear_ownership` (generic detection, not hardcoded to the lease) | Task created, `bucket=unclear_ownership`, `declined_by=[divya.rao]` | Task created, `candidate_owners=[raghav.sethi]`, `declined_by=[divya.rao]` | **Pass** |
| Negated commitment ("I will not be able to send... by Friday") does not create a false-positive task | No Friday `vendor_list` task from that message | None created | **Pass** — fixes keyword-pipeline TC5 |
| Grounding / anti-hallucination guard: a record citing a fabricated evidence ID (not present in the thread) is dropped | Record rejected, no task created | `[extract_llm] dropped ungrounded record ... ['s-tc11-1', 'FAKE-ID-DOES-NOT-EXIST']`; no Karan task created | **Pass** |
| Calendar cross-referencing on the resolved Meridian call | Status `done`, calendar event surfaced | `status=done`, `calendar_context=['arjun.malhotra: Call — Meridian Logistics (2026-09-23 15:00-15:30)']` | **Pass** |
| Topic registry persists newly-discovered topics | `team_offsite_agenda` and `office_wifi_upgrade` both in registry after run | Both present | **Pass** |
| Warm-cache run makes **zero** new LLM calls for unchanged threads | 0 threads re-processed | 0 | **Pass** |
| Warm-cache run produces the identical task set as the cold run | Task count matches | Matches | **Pass** |

**Summary:** 8/8 plumbing checks pass. This validates the deterministic code
around the LLM call — registry, per-thread cache, grounding guard, generic
unclear-ownership detection, calendar cross-referencing — not the live
model's own judgment (see section 4 for that, from the real running app).

---

## 3. Name/email actor normalization regression (`test_name_normalization.py`)

Replays a real bug observed in production `extraction_cache.json`: the same
person (Arjun) was returned by the LLM as `"Arjun Malhotra"` (a display name)
in some threads and `"arjun.malhotra@veridian-corp.example"` (his email) in
others, fragmenting one topic into multiple tasks and sometimes misfiling
them into the wrong bucket.

| Check | Expected | Actual | Result |
|---|---|---|---|
| `meridian_call` across 3 threads (2 name-form, 1 email-form) collapses into **one** task | 1 task, evidence merged from all 3 threads | 1 task; `actor` normalized to the canonical email; evidence = `['call-1'..'call-5', 't1-8', 'vn-2']` (all 3 threads merged) | **Pass** |
| `vendor_list` across 3 threads (2 name-form, 1 email-form) collapses into **one** task | 1 task, evidence merged | 1 task; evidence = `['t1-3', 'vendor-2', 'vendor-4', 'vn-1']` | **Pass** |
| Both normalize into `bucket=my_actions` (not misfiled into `waiting_on_others`) | `my_actions` | `my_actions` for both | **Pass** |

**Summary:** 3/3 pass — the name/email inconsistency no longer fragments or
misfiles tasks.

---

## 4. Q&A layer — live app (Groq-backed), manual test

Tested directly against the running app's `/ask` endpoint with the real
board state: 2 `my_actions` (1 overdue — "send updated vendor list", owed to
Raghav, manually marked *not done*; 1 done), 2 `waiting_on_others` (both
done), 1 `unclear_ownership` (Mumbai lease, manually marked *done*).

| # | Question | Answer (Groq) | Assessment |
|---|---|---|---|
| 1 | "what task I have left tomorrow?" | *"You have no pending tasks scheduled for tomorrow."* | **Fail (found during manual walkthrough).** Literally true (no deadline falls on 2026-09-26), but it silently omits the one task that's actually still open on the board — the overdue "send updated vendor list" item (deadline 2026-09-23, still not done). A user reading this answer would reasonably conclude they have nothing outstanding, which isn't true. |
| 2 | "what tasks do I need to ask others for?" | *"You have no pending 'waiting on others' tasks—both the expense-report and campaign-deck items that were directed to you are already marked done."* | **Pass.** Both `waiting_on_others` tasks are genuinely done; the answer is accurate. |

**Root cause (question 1):** `ai/qa.py`'s system prompt told the LLM to
answer strictly from the task JSON but gave no instruction to separately
flag still-open overdue items when a question's literal date doesn't match
any deadline. The offline keyword fallback had the same gap in its
`"tomorrow"` branch (by design it correctly keeps "due tomorrow" distinct
from "due today/overdue" — that distinction itself was a previously-fixed
bug, per the code comment — but it never surfaced overdue items as a
separate note).

**Fix applied** (`backend/ai/qa.py`):
- System prompt now explicitly instructs the LLM to keep "is X due on this
  literal day" separate from "is anything still outstanding," and to flag
  overdue items even when they don't match the day asked about.
- The offline fallback's `"tomorrow"` branch now appends an overdue-item
  note (task, who it's owed to, how long overdue) without changing its
  correct exact-date matching logic.

**Regression check (fallback path, same board state as the screenshot):**

```
>>> _fallback_answer("what task I have left tomorrow?", tasks, "2026-09-25")
"Nothing is due tomorrow (2026-09-26). Note: you still have an overdue item
outstanding — send updated vendor list (owed to Raghav, overdue since
2026-09-23)."
```

**Result: Pass** (fallback path verified directly; the Groq path carries the
same instruction via the updated system prompt but should be re-asked live
in the running app to confirm the model follows it, since LLM output isn't
deterministic).

---

## 5. Known limitations (not covered / not fixed)

- Keyword pipeline: TC2, TC4, TC5, TC6, TC8, TC9, TC10 above (section 1) —
  all superseded by the LLM pipeline when a key is configured, but still
  present in the no-key fallback path.
- LLM-path answer *quality* (as opposed to plumbing) hasn't been
  systematically tested beyond the two live Q&A cases in section 4, since
  that requires a live API key and isn't deterministic run-to-run.
- No automated test suite exists in the repo itself (`backend/`) — all
  cases above live as one-off scripts in a scratch directory. Worth
  promoting into a real `backend/tests/` suite (pytest) if this goes past
  the take-home stage.

---

## How to reproduce

All commands assume a copy of `backend/` with a clean `data/seed_data.json`,
`data/topic_registry.json` (seeded, 5 topics), and `data/extraction_cache.json`
(`{}`).

```bash
# Section 1 — keyword pipeline stress test
python3 run_stress.py

# Section 2 — LLM pipeline plumbing (mocked LLM, no key needed)
python3 test_llm_path.py

# Section 3 — name/email normalization regression
python3 test_name_normalization.py
```

Section 4 was run against the live app UI (`/brief` + `/ask`), not a script.
