"""Tests for the pipeline loop (tools/engine.py, Loop A) against the bundled
fixtures, provider=mock. No network, no credentials, no live config - see
tests/_helpers.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_email, get_pms
from core.store import Store

import engine

from _helpers import hermetic_settings

EXPECTED_KINDS = {
    "lead-01-conference-rfp": "conference",
    "lead-02-celebration-fr": "wedding",
    "lead-03-incentive-discount": "incentive",
    "lead-04-single-room": "single_room",
}


def _messages(settings):
    return get_email(settings).fetch_unread(limit=50)


def test_four_fixtures_are_present(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    messages = _messages(settings)
    assert len(messages) == 4
    assert {m.id for m in messages} == set(EXPECTED_KINDS)


def test_classify_matches_the_expected_fixture_for_every_lead(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "pipeline.db")
    for msg in _messages(settings):
        item, did_work = engine.process_lead_email(settings, store, pms, msg, provider="mock")
        assert did_work is True
        assert item.intent == EXPECTED_KINDS[msg.id]
        assert item.draft is not None
        assert item.draft.get("body")
    store.close()


def test_single_room_is_out_of_scope_and_always_needs_human(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "pipeline2.db")
    msg = next(m for m in _messages(settings) if m.id == "lead-04-single-room")
    item, _ = engine.process_lead_email(settings, store, pms, msg, provider="mock")
    assert item.review_status == "needs_human"
    store.close()


def test_discount_ask_over_the_floor_is_clamped_and_flagged():
    applied, flagged = engine.clamp_discount(22, 15)
    assert applied == 15
    assert flagged is True
    applied, flagged = engine.clamp_discount(10, 15)
    assert applied == 10
    assert flagged is False


def test_shadow_mode_never_sends_anything(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "pipeline3.db")
    for msg in _messages(settings):
        engine.process_lead_email(settings, store, pms, msg, provider="mock")
    counts = store.counts()
    assert counts.get("sent", 0) == 0
    assert counts.get("auto_sent", 0) == 0
    store.close()


def test_rerun_is_idempotent_and_does_not_reprocess(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "pipeline4.db")
    messages = _messages(settings)
    for msg in messages:
        engine.process_lead_email(settings, store, pms, msg, provider="mock")
    for msg in messages:
        item, did_work = engine.process_lead_email(settings, store, pms, msg, provider="mock")
        assert did_work is False
    assert len(store.list_items()) == 4
    store.close()


def test_a_follow_up_task_is_scheduled_for_every_in_scope_lead(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "pipeline5.db")
    for msg in _messages(settings):
        engine.process_lead_email(settings, store, pms, msg, provider="mock")
    rows = store.db.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='lead_followup'").fetchone()
    assert rows["n"] == 3  # single_room is excluded by process_lead_email
    store.close()


def test_dry_run_writes_nothing_and_is_safe_to_run_twice(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch, dry_run=True)
    pms = get_pms(settings)
    store = Store(settings, path=tmp_path / "dry.db")
    for msg in _messages(settings):
        item, did_work = engine.process_lead_email(settings, store, pms, msg, provider="mock")
        assert did_work is True
        assert item.draft is not None  # still computed and returned for preview
    # a second dry pass over the same fixtures must be an identical no-op
    for msg in _messages(settings):
        engine.process_lead_email(settings, store, pms, msg, provider="mock")
    assert store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0
    assert store.db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
    store.close()


def test_a_language_not_in_hotel_languages_falls_back_and_needs_a_human(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    assert "de" not in settings.hotel.languages
    assert engine.needs_human_for("conference", 2, 0.99, False, False, settings,
                                  language_unsupported=True) is True
