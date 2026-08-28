"""Tests for the Win-Back / Loyalty AI sub-agent (tools/winback.py).

Deterministic letters, computed cohort - see docs/how-it-works.md design
decisions 4 and 5. No network, no credentials, no live config.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_pms
from core.store import Store

import store_ext as sx
import winback

from _helpers import hermetic_settings


def test_categorize_reason_matches_keywords():
    assert winback.categorize_reason("the noise from next door") == "noise"
    assert winback.categorize_reason("used the spa every day") == "spa"
    assert winback.categorize_reason("midsummer week is open again") == "unsold"
    assert winback.categorize_reason("moved to a competitor for the rate") == "price"
    assert winback.categorize_reason("") == "general"


def test_draft_letter_never_offers_a_discount_or_a_percentage():
    for category in ("noise", "spa", "unsold", "price", "general"):
        letter = winback.draft_letter(guest_name="Alex Guest", reason_to_return="a reason",
                                      reason_category=category, hotel_name="Hotel Aurora")
        text = (letter["subject"] + " " + letter["body"]).lower()
        assert "%" not in text
        assert "discount" not in text
        assert "code" not in text or "no code" in text


def test_draft_letter_is_grammatical_for_every_bundled_fixture_note():
    """SIMULATION.md finding 3: fixtures/hotel/reservations.json notes are PMS
    shorthand - subjectless verb fragments like "moved to a competitor..." or
    "always booked the same midsummer week...", not full sentences. Slotted
    straight into a "because {reason}" template that reads "because moved to
    a competitor" - missing the subject. These are the exact reason strings
    that ship in fixtures/hotel/reservations.json for the five demo win-back
    guests (Fatima/unsold, Marco/spa, Elin/noise, Sara/general, Owen/price).
    """
    fixture_reasons = {
        "unsold": "always booked the same midsummer week, which is open again this year",
        "spa": "used the spa every day of the last stay - a new treatment now matches "
              "that pattern",
        "noise": "the noise from the adjacent room on the last stay - that floor has "
                "since been fully renovated",
        "price": "moved to a competitor along the coast after our direct rate crept up",
        "general": "",  # blank note -> draft_letter's own fallback reason
    }
    broken_openers = ("because always", "because moved", "because used",
                      ": used ", "but moved", "if you moved on - but moved")
    for category, reason in fixture_reasons.items():
        letter = winback.draft_letter(guest_name="Alex Guest", reason_to_return=reason,
                                      reason_category=category, hotel_name="Hotel Aurora")
        body_lower = letter["body"].lower()
        for broken in broken_openers:
            assert broken not in body_lower, (
                f"{category} letter reads ungrammatically: {letter['body']!r}")


def test_needs_subject_detects_bare_verb_fragments_not_noun_phrases():
    assert winback._needs_subject("moved to a competitor along the coast")
    assert winback._needs_subject("always booked the same midsummer week")
    assert winback._needs_subject("used the spa every day of the last stay")
    assert not winback._needs_subject("the noise from the adjacent room")
    assert not winback._needs_subject("your last stay is still one we remember well")
    assert not winback._needs_subject("")


def test_compute_cohort_excludes_recent_and_single_stay_guests(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "winback.db")
    sx.ensure_schema(store)
    winback.compute_cohort(settings, store, pms)
    names = {r["guest_name"] for r in sx.list_cohort(store)}
    assert "Elin Karlsson" in names       # lapsed, 2 qualifying stays
    assert "Petra Lindqvist" not in names  # only one past stay
    assert "Noah Fischer" not in names     # most recent stay is too recent
    store.close()


def test_cohort_is_ranked_by_lifetime_spend_times_stays(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "winback2.db")
    sx.ensure_schema(store)
    winback.compute_cohort(settings, store, pms)
    rows = sx.list_cohort(store)
    scores = [r["lifetime_spend"] * r["stays"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    store.close()


def test_draft_creates_one_pending_review_item_per_cohort_guest(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "winback3.db")
    sx.ensure_schema(store)
    winback.compute_cohort(settings, store, pms)
    from types import SimpleNamespace
    winback.cmd_draft(store, settings, SimpleNamespace())
    items = store.list_items(kind="winback_letter", limit=50)
    cohort = sx.list_cohort(store)
    assert len(items) == len(cohort)
    assert all(i.review_status == "pending_review" for i in items)
    store.close()
