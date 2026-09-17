"""
Multi-provider LLM wrapper with automatic failover. Used by both the Q&A
layer (ai/qa.py, one call per question) and the LLM extraction path
(ai/extract_llm.py, one call per thread not already cached) — nowhere
else in the pipeline calls an LLM, so the core dedup/classification/
status logic stays deterministic, testable, and hallucination-free.

You can set MORE THAN ONE provider's key in .env at once. Providers are
tried in priority order (Groq -> Gemini, whichever keys are present).
Within EACH provider, a small primary->lighter model cascade is tried
before giving up on that provider — both Groq and (especially) Gemini's
free tier meter quota PER MODEL, not per account (see
GenerateRequestsPerDayPerProjectPerModel-FreeTier in a 429 body), so a
lighter model under the same key is a genuinely separate quota bucket,
not just a weaker retry. Only once every model of every configured
provider has failed does the caller drop to its offline keyword
fallback.
"""
from __future__ import annotations
import os
import sys

PROVIDER_PRIORITY = ["groq", "gemini"]

# Primary model first (overridable via GROQ_MODEL/GEMINI_MODEL), then
# lighter fallback model(s) on the SAME provider/key, tried before moving
# to the next provider. Override the fallback list via
# GROQ_MODEL_FALLBACKS / GEMINI_MODEL_FALLBACKS (comma-separated).
DEFAULT_FALLBACK_MODELS = {
    "groq": ["llama-3.1-8b-instant"],
    # gemini-2.0-flash-lite / gemini-2.0-flash were retired live (confirmed
    # via a 404 "no longer available" response naming their replacements)
    # — gemini-3.5-flash-lite is what Google's own error pointed at.
    "gemini": ["gemini-3.5-flash-lite"],
}


def _models_for(provider: str) -> list[str]:
    primary = os.environ.get(f"{provider.upper()}_MODEL") or {
        "groq": "openai/gpt-oss-120b",
        "gemini": "gemini-3.6-flash",
    }[provider]
    raw_fallbacks = os.environ.get(f"{provider.upper()}_MODEL_FALLBACKS")
    fallbacks = (
        [m.strip() for m in raw_fallbacks.split(",") if m.strip()]
        if raw_fallbacks is not None
        else DEFAULT_FALLBACK_MODELS.get(provider, [])
    )
    # de-dupe while preserving order, in case the primary is accidentally repeated
    seen = set()
    models = []
    for m in [primary] + fallbacks:
        if m not in seen:
            seen.add(m)
            models.append(m)
    return models


_clients: dict = {}          # provider -> client instance, or False if init failed
_configured: list | None = None
_last_used_provider: str | None = None
_last_used_model: str | None = None


def _configured_providers() -> list[str]:
    global _configured
    if _configured is not None:
        return _configured
    found = set()
    if os.environ.get("GROQ_API_KEY"):
        found.add("groq")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        found.add("gemini")
    _configured = [p for p in PROVIDER_PRIORITY if p in found]
    return _configured


def available() -> bool:
    return len(_configured_providers()) > 0


def last_used_provider() -> str | None:
    """Which provider actually answered the most recent successful call —
    useful for /health and for demoing the failover live."""
    return _last_used_provider


def last_used_model() -> str | None:
    """Which model (within last_used_provider()) actually answered —
    e.g. confirms a fallback model kicked in after the primary was
    rate-limited, without digging through logs."""
    return _last_used_model


# Hard cap on a single LLM call so a stalled network request can never hang
# the whole /brief (or /ask) request forever — it fails and falls over to
# the next provider (or the offline fallback) instead of hanging the page.
REQUEST_TIMEOUT_SECONDS = 25


def _get_client(provider: str):
    if provider in _clients:
        return _clients[provider]
    client = None
    try:
        if provider == "groq":
            import groq
            client = groq.Groq(api_key=os.environ["GROQ_API_KEY"], timeout=REQUEST_TIMEOUT_SECONDS)
        elif provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
            client = genai
    except Exception as e:
        print(f"[llm_client] could not initialize {provider}: {e}", file=sys.stderr)
        client = False
    _clients[provider] = client
    return client


def _call(provider: str, client, model: str, system: str, user: str, max_tokens: int) -> str:
    if provider == "groq":
        # Primary: openai/gpt-oss-120b, OpenAI's open-weight GPT model hosted
        # on Groq's hardware. Fallback: llama-3.1-8b-instant — smaller, and
        # (on Groq's free tier) rate-limited independently of the primary
        # model, so it's a genuinely separate budget, not just a weaker retry.
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        if "gpt-oss" in model:
            # gpt-oss is a REASONING model — by default it spends an
            # open-ended number of tokens "thinking" before it writes the
            # visible answer, and those reasoning tokens count against
            # max_tokens. Observed live: with a longer system prompt, this
            # ate the entire budget and the visible answer got cut off
            # after a single word ("Tomorrow"). This task is short grounded
            # Q&A / extraction, not a task that benefits from deep
            # reasoning, so cap reasoning effort low — leaves the budget
            # for the actual answer and burns fewer tokens per call too
            # (helps the same quota pressure the model cascade above is
            # for). Not passed for llama-3.1-8b-instant — a non-reasoning
            # model that doesn't accept this param.
            kwargs["reasoning_effort"] = "low"
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    if provider == "gemini":
        # Primary: gemini-3.6-flash — confirmed live against Google's actual
        # API; an earlier default (gemini-2.5-flash) came back 404 "no
        # longer available to new users". Fallback: gemini-3.5-flash-lite —
        # gemini-2.0-flash-lite/gemini-2.0-flash were tried first but came
        # back 404 retired, with Google's own error naming this as the
        # replacement. Google's free tier meters
        # GenerateRequestsPerDayPerProjectPerModel PER MODEL, so a 429 on
        # gemini-3.6-flash still leaves this with its own full quota.
        gen_model = client.GenerativeModel(model, system_instruction=system)
        resp = gen_model.generate_content(
            user, request_options={"timeout": REQUEST_TIMEOUT_SECONDS}
        )
        return resp.text

    raise ValueError(f"unknown provider: {provider}")


def complete(system: str, user: str, max_tokens: int = 500) -> str | None:
    """
    Tries each configured provider in priority order, and within each
    provider tries its model cascade (primary, then lighter fallback
    model(s) on the same key — see _models_for()) before moving to the
    next provider. On ANY failure — rate limit, quota exceeded, auth
    error, network error — moves to the next model/provider automatically.
    Returns None only if every model of every configured provider fails
    (or none are configured), which sends the caller (Q&A or LLM
    extraction) to its offline/deterministic fallback instead.
    """
    global _last_used_provider, _last_used_model
    for provider in _configured_providers():
        client = _get_client(provider)
        if not client:
            continue
        for model in _models_for(provider):
            try:
                result = _call(provider, client, model, system, user, max_tokens)
                if result:
                    _last_used_provider = provider
                    _last_used_model = model
                    return result
            except Exception as e:
                print(f"[llm_client] {provider}/{model} failed ({e}) — "
                      f"falling back to next model/provider", file=sys.stderr)
                continue
    return None
