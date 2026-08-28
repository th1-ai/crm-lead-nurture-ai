"""Tests for tools/coach.py - clustering only, never touches a guest or
prospect, never auto-applies.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.store import Store

import coach
import store_ext as sx

from _helpers import hermetic_settings


def _learning(ts, applied_to, before="old text", after="new text"):
    return {"ts": ts, "applied_to": applied_to, "before": before, "after": after}


def test_cluster_learnings_groups_by_applied_to():
    learnings = [_learning("2026-01-01T00:00:00", "conference"),
                _learning("2026-01-02T00:00:00", "conference"),
                _learning("2026-01-03T00:00:00", "wedding")]
    clusters = coach.cluster_learnings(learnings, min_cluster_size=2)
    assert len(clusters) == 1
    assert clusters[0]["pattern"] == "conference"
    assert clusters[0]["cluster_size"] == 2


def test_a_cluster_below_the_threshold_is_not_a_proposal():
    learnings = [_learning("2026-01-01T00:00:00", "incentive")]
    clusters = coach.cluster_learnings(learnings, min_cluster_size=2)
    assert clusters == []


def test_analyze_is_idempotent_no_duplicate_proposal_per_cluster(tmp_path, monkeypatch):
    """Reproduces SIMULATION.md finding 1: two edits cluster on "wedding";
    running `analyze` twice (e.g. a retry after an interactive pause, or the
    weekly job running before the first proposal is decided) must produce
    ONE proposal, not two. Idempotency is `store.upsert_unique` keyed on the
    cluster's stable fingerprint (its `pattern` / intent key) - see
    tools/coach.py:cmd_analyze.
    """
    settings = hermetic_settings(tmp_path, monkeypatch)
    store = Store(settings, path=tmp_path / "coach_idempotent.db")
    sx.ensure_schema(store)
    for i in (1, 2):
        store.db.execute(
            "INSERT INTO learnings (ts, source_item, applied_to, before, after) "
            "VALUES (?,?,?,?,?)",
            (f"2026-08-2{i}T00:00:00", f"i{i}", "wedding", f"old{i}", f"new{i}"))

    from types import SimpleNamespace
    args = SimpleNamespace(provider=None)
    assert coach.cmd_analyze(store, settings, args) == 0
    assert coach.cmd_analyze(store, settings, args) == 0  # re-run, same clusters

    rows = store.db.execute(
        "SELECT id FROM coach_proposals WHERE pattern='wedding'").fetchall()
    assert len(rows) == 1, "a second analyze() must not spawn a duplicate proposal"
    store.close()


def test_apply_only_writes_accepted_proposals(tmp_path, monkeypatch):
    # RULES_FILE is a module-level path into the real repo's knowledge/ dir -
    # redirect it into tmp_path so this test never writes a real file that
    # would leak into git status or confuse `make doctor`.
    rules_file = tmp_path / "knowledge" / "rules.md"
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(coach, "RULES_FILE", rules_file)

    settings = hermetic_settings(tmp_path, monkeypatch)
    store = Store(settings, path=tmp_path / "coach.db")
    sx.ensure_schema(store)
    store.db.execute(
        "INSERT INTO coach_proposals (id, created_at, pattern, intent, cluster_size, "
        "example_before, example_after, suggested_fix, status) VALUES "
        "('p1','2026-01-01','conference','conference',2,'a','b','add a rule','pending')")
    from types import SimpleNamespace
    coach.cmd_apply(store, SimpleNamespace())  # nothing accepted yet
    assert not rules_file.exists()

    coach._decide(store, "p1", "accepted", "")
    coach.cmd_apply(store, SimpleNamespace())
    assert rules_file.exists()
    assert "add a rule" in rules_file.read_text(encoding="utf-8")
    row = store.db.execute("SELECT status FROM coach_proposals WHERE id='p1'").fetchone()
    assert row["status"] == "applied"
    store.close()
