"""Tests for the outbound loop (tools/outreach.py, Loop B) - deterministic,
no LLM (docs/how-it-works.md design decision 7). No network, no credentials.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.store import Store

import outreach
import store_ext as sx

from _helpers import hermetic_settings


def _store(tmp_path, monkeypatch, name="outreach.db"):
    settings = hermetic_settings(tmp_path, monkeypatch)
    store = Store(settings, path=tmp_path / name)
    sx.ensure_schema(store)
    return settings, store


def test_hook_for_ai_personalization_on_uses_the_signal():
    hook, signal = outreach.hook_for({"signal": "hiring 15 engineers", "org": "Acme"},
                                     ai_personalization=True)
    assert "hiring" in signal
    assert hook != "I'll keep this short."


def test_hook_for_ai_personalization_off_is_the_generic_fallback():
    hook, signal = outreach.hook_for({"signal": "hiring 15 engineers", "org": "Acme"},
                                     ai_personalization=False)
    assert hook == "I'll keep this short."
    assert signal == "your events calendar"


def test_scan_holds_out_prospects_from_a_pending_source(tmp_path, monkeypatch):
    settings, store = _store(tmp_path, monkeypatch)
    outreach.cmd_scan(store, settings, SimpleNamespace(avatar="av-mice"))
    prospects = sx.list_prospects(store, avatar="av-mice")
    orgs = {p["org"] for p in prospects}
    assert "Northbridge Partners" in orgs           # approved source - revealed
    assert "Atlas Fieldwork" not in orgs             # pending source - held out
    store.close()


def test_approving_a_source_and_rescanning_reveals_more_prospects(tmp_path, monkeypatch):
    settings, store = _store(tmp_path, monkeypatch)
    outreach.cmd_scan(store, settings, SimpleNamespace(avatar="av-mice"))
    before = {p["org"] for p in sx.list_prospects(store, avatar="av-mice")}
    sources = {s["name"]: s["id"] for s in sx.list_sources(store)}
    sx.set_source_status(store, sources["Regional exhibitor directory (AI-found)"], "approved")
    outreach.cmd_scan(store, settings, SimpleNamespace(avatar="av-mice"))
    after = {p["org"] for p in sx.list_prospects(store, avatar="av-mice")}
    assert "Atlas Fieldwork" in after
    assert after > before
    store.close()


def test_enrich_skips_do_not_contact_prospects(tmp_path, monkeypatch):
    settings, store = _store(tmp_path, monkeypatch)
    outreach.cmd_scan(store, settings, SimpleNamespace(avatar="av-mice"))
    outreach.cmd_enrich(store, settings, SimpleNamespace(avatar="av-mice"))
    harborline = next(p for p in sx.list_prospects(store, avatar="av-mice")
                      if p["org"] == "Harborline Logistics")
    assert harborline["do_not_contact"] == 1
    assert harborline["email_status"] == "missing"  # never enriched
    store.close()


def test_channel_cap_and_warmup_ramp(tmp_path, monkeypatch):
    settings, store = _store(tmp_path, monkeypatch)
    # a fresh channel is capped at warmup_ramp_week1, not its full daily_cap
    assert outreach.channel_cap(store, settings, "email") == \
        settings.agent_get("outreach.warmup_ramp_week1", 3)
    for _ in range(3):
        outreach.record_send(store, "email")
    assert outreach.under_cap(store, settings, "email") is False
    store.close()


def test_safe_caps_off_removes_the_limit(tmp_path, monkeypatch):
    settings, store = _store(tmp_path, monkeypatch)
    settings.agent["outreach"]["safe_caps"] = False
    for _ in range(50):
        outreach.record_send(store, "email")
    assert outreach.under_cap(store, settings, "email") is True
    store.close()


def test_launch_blocks_when_deliverability_is_not_green(tmp_path, monkeypatch):
    settings, store = _store(tmp_path, monkeypatch)
    settings.agent["outreach"]["deliverability"] = {"spf": True, "dkim": True, "dmarc": False}
    outreach.cmd_scan(store, settings, SimpleNamespace(avatar="av-mice"))
    outreach.cmd_enrich(store, settings, SimpleNamespace(avatar="av-mice"))
    outreach.cmd_generate_campaign(store, SimpleNamespace(avatar="av-mice", name="t", kind="mice"))
    campaign_id = sx.list_campaigns(store)[-1]["id"]
    code = outreach.cmd_launch(store, settings, SimpleNamespace(campaign_id=campaign_id))
    assert code == 1
    assert sx.list_enrollments(store) == []
    store.close()
