"""tools/enrichment.py - contact enrichment (Hunter.io / Findymail).

Not a `core/adapters` family - these are single-purpose, account-specific
lookup APIs, not a protocol every mailbox/PMS/chat system shares the shape of
(docs/how-it-works.md design decision 9). `mock` needs no key and is what
`make demo` uses; it mirrors the source engine's fidelity notes - a
`f.last@domain` variant every 5th lookup, diacritics stripped, and a couple of
lookups that come back genuinely not-found, "so the run isn't suspiciously
perfect".
"""

from __future__ import annotations

import hashlib
import os
import unicodedata

from core.adapters._http import request_json
from core.config import Settings

COST_PER_LOOKUP = {"hunter": 0.034, "findymail": 0.049, "mock": 0.0}


class EnrichmentError(RuntimeError):
    """Raised when a real provider is selected but not configured."""


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text or "")
                  if unicodedata.category(c) != "Mn")


def _digest_int(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


def find_email_mock(prospect: dict) -> tuple[str, str, str]:
    """Deterministic, zero-credential stand-in. Returns (email, status, provider).

    Hashes on the prospect's own facts (org + name), never the database id -
    the id is a fresh random uuid every time fixtures are re-seeded, and this
    stays the same fixture prospect run after run.
    """
    n = _digest_int(prospect.get("org", ""), prospect.get("first_name", ""),
                    prospect.get("last_name", ""))
    if n % 7 == 0:
        return "", "not_found", "mock"
    first = strip_accents(prospect.get("first_name", "")).lower()
    last = strip_accents(prospect.get("last_name", "")).lower()
    domain = prospect.get("domain") or "example.com"
    local = f"{first[:1]}.{last}" if n % 5 == 0 else f"{first}.{last}"
    provider = "findymail" if (first and last) else "hunter"
    return f"{local}@{domain}", "found", provider


def find_email_hunter(prospect: dict) -> tuple[str, str, str]:
    key = os.environ.get("HUNTER_API_KEY")
    if not key:
        raise EnrichmentError(
            "HUNTER_API_KEY is not set. Add it to .env, or set "
            "enrichment.provider: mock in config/agent.yaml for a zero-credential demo.")
    data = request_json("GET", "https://api.hunter.io/v2/email-finder", params={
        "domain": prospect.get("domain"), "first_name": prospect.get("first_name"),
        "last_name": prospect.get("last_name"), "api_key": key})
    email = ((data.get("data") or {}).get("email")) or ""
    return (email, "found" if email else "not_found", "hunter")


def find_email_findymail(prospect: dict) -> tuple[str, str, str]:
    key = os.environ.get("FINDYMAIL_API_KEY")
    if not key:
        raise EnrichmentError(
            "FINDYMAIL_API_KEY is not set. Add it to .env, or set "
            "enrichment.provider: mock in config/agent.yaml for a zero-credential demo.")
    data = request_json("POST", "https://api.findymail.com/api/search/name",
                        headers={"Authorization": f"Bearer {key}"},
                        json_body={"domain": prospect.get("domain"),
                                  "name": f"{prospect.get('first_name')} {prospect.get('last_name')}"})
    contact = data.get("contact") or {}
    email = contact.get("email") or ""
    return (email, "found" if email else "not_found", "findymail")


def find_email(prospect: dict, settings: Settings) -> tuple[str, str, str, float]:
    """Returns (email, status, provider, cost_eur)."""
    provider = str(settings.agent_get("enrichment.provider", "mock"))
    fn = {"mock": find_email_mock, "hunter": find_email_hunter,
         "findymail": find_email_findymail}.get(provider, find_email_mock)
    email, status, used_provider = fn(prospect)
    costs = settings.agent_get("enrichment.cost_per_lookup", {}) or COST_PER_LOOKUP
    cost = float(costs.get(used_provider, COST_PER_LOOKUP.get(used_provider, 0.0))) \
        if status == "found" else 0.0
    return email, status, used_provider, cost
