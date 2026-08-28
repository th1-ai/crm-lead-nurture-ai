"""Tests for tools/review.py's `send` - shadow mode must never lose an
approval when a write is blocked (docs/safety.md, CLAUDE.md rule 1).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.store import Store

import review
import store_ext as sx

from _helpers import hermetic_settings


def test_send_blocked_in_shadow_mode_returns_item_to_approved(tmp_path, monkeypatch, capsys):
    """SIMULATION.md's shadow-send check, made permanent: a WriteBlocked send
    (mode: shadow, the default here) must not leave the item `failed` - that
    would make a hotel think their approval was lost. It goes back to
    `approved`, ready to actually send the moment they flip to live -
    matches core/store.py's own TRANSITIONS comment ("approved: a guard
    (shadow mode) blocked the send - the approval stands") and the reference
    agent's tools/review.py:cmd_send.
    """
    settings = hermetic_settings(tmp_path, monkeypatch)  # demo=True -> mode=shadow, mock adapters
    store = Store(settings, path=tmp_path / "review_send.db")
    sx.ensure_schema(store)

    item = store.upsert_item("mock", "lead-01", kind="lead_reply",
                             payload={"from": "guest@example.com"})
    store.set_fields(item.id, draft={"subject": "Re: enquiry", "body": "Thanks for writing."})
    store.transition(item.id, "pending_review", "agent")
    store.transition(item.id, "approved", "human")

    exit_code = review.cmd_send(store, settings, SimpleNamespace(limit=20))
    out = capsys.readouterr().out

    updated = store.get_item(item.id)
    assert updated.review_status == "approved", (
        "a shadow-blocked send must return the item to 'approved', not 'failed'")
    assert "approval kept" in out
    assert exit_code == 1  # nothing actually sent this pass
    store.close()


def test_sample_item_shows_marker_in_list_line_and_show(tmp_path, monkeypatch, capsys):
    """core/store.py tags an item read through a mock adapter outside `make
    demo` as `_sample` (`Item.is_sample`) - a human working the real queue
    must see that at a glance, in both `list` and `show`."""
    settings = hermetic_settings(tmp_path, monkeypatch)
    store = Store(settings, path=tmp_path / "review_sample.db")
    sx.ensure_schema(store)

    item = store.upsert_item("email", "sample-marker-1", kind="lead_reply",
                             payload={"subject": "Group booking enquiry",
                                      "from": "guest@example.com", "_sample": True})
    assert item.is_sample

    capsys.readouterr()
    review._print_item_line(item)
    assert "[SAMPLE DATA]" in capsys.readouterr().out

    rc = review.cmd_show(store, SimpleNamespace(id=item.id))
    assert rc == 0
    assert "[SAMPLE DATA]" in capsys.readouterr().out
    store.close()
