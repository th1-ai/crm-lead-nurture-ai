#!/usr/bin/env python3
"""tools/coach.py - Email Optimizer / Coach AI: learn from human corrections.

    python3 tools/coach.py analyze     # cluster edits + rejections into proposals
    python3 tools/coach.py list        # pending proposals waiting on a human
    python3 tools/coach.py show <id>
    python3 tools/coach.py accept <id> [--note "..."]
    python3 tools/coach.py reject <id> [--note "..."]
    python3 tools/coach.py apply       # write accepted proposals into knowledge/rules.md

Never talks to a guest or a prospect, never changes behaviour on its own -
see docs/how-it-works.md and workflows/85-coach-weekly.md.

`learnings` (`core.store`, written automatically by `core.review.edit()` and
`core.review.reject()` on every kind of draft in this repo - lead replies,
outreach steps, outreach replies, win-back letters) are grouped by the intent
or kind they happened on. A group at or above `coach.min_cluster_size`
(config/agent.yaml, default 2) is a real pattern, not noise, and gets ONE
model call to turn it into a concrete suggestion - stored as a row in
`coach_proposals` (tools/store_ext.py) with `status='pending'`.

A proposal only ever changes anything after a human runs `accept` and then
`apply` - a rejected or still-pending proposal changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive, complete  # noqa: E402
from core.store import Store, StoreError, utcnow  # noqa: E402
from core.templates import build_prompt  # noqa: E402

import store_ext as sx  # noqa: E402

KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
RULES_FILE = KNOWLEDGE_DIR / "rules.md"


def _suggest(settings: Settings, store: Store, *, pattern: str, intent: str, cluster_size: int,
            example_before: str, example_after: str, provider: str | None) -> str:
    prompt = build_prompt("coach-suggestion", settings=settings, pattern=pattern, intent=intent,
                          cluster_size=cluster_size, example_before=example_before[:500],
                          example_after=example_after[:500])
    result = complete("coach-suggestion", prompt, None, settings=settings, provider=provider,
                      store=store)
    return result.text.strip()


def cluster_learnings(learnings: list[dict], min_cluster_size: int) -> list[dict]:
    """Group edits/rejections by the intent or kind they happened on.

    Deterministic on purpose - no fuzzy text matching. `applied_to` is set by
    `core.review.edit()` / `reject()` to `item.intent or item.kind`, so a
    cluster key like `conference` or `outreach_step` tells you exactly which
    part of the two loops is being corrected.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in learnings:
        key = row.get("applied_to") or "unknown"
        groups[key].append(row)
    clusters = []
    for key, rows in groups.items():
        if len(rows) < min_cluster_size:
            continue
        rows.sort(key=lambda r: r["ts"])
        latest = rows[-1]
        clusters.append({"pattern": key, "intent": key, "cluster_size": len(rows),
                         "example_before": latest.get("before") or "",
                         "example_after": latest.get("after") or ""})
    return clusters


def cmd_analyze(store: Store, settings: Settings, args) -> int:
    min_size = int(settings.agent_get("coach.min_cluster_size", 2))
    learnings = store.list_learnings(limit=500)
    clusters = cluster_learnings(learnings, min_size)
    created = 0
    skipped = 0
    for c in clusters:
        # Idempotency: `pattern` (== the cluster's intent/kind key from
        # cluster_learnings) is a stable fingerprint - it does not change
        # between runs even as the cluster grows or its example pair is
        # replaced by a newer edit. Skip BEFORE spending a model call /
        # interactive round-trip if this fingerprint already has an open
        # (pending) or accepted proposal on record - that is the common
        # case this guards: a second `analyze` before the first proposal is
        # decided, or the weekly job running twice. See
        # tests/test_crm_coach.py.
        already = store.db.execute(
            "SELECT id FROM coach_proposals WHERE pattern=? AND status IN "
            "('pending','accepted')", (c["pattern"],)).fetchone()
        if already is not None:
            skipped += 1
            continue
        try:
            suggestion = _suggest(settings, store, pattern=c["pattern"], intent=c["intent"],
                                  cluster_size=c["cluster_size"],
                                  example_before=c["example_before"],
                                  example_after=c["example_after"], provider=args.provider)
        except LLMPendingInteractive as exc:
            # A pause, not a failure - never swallowed into a canned fallback.
            # No row exists yet for this fingerprint (the check above only
            # skips once a proposal is actually on record), so answering the
            # prompt and re-running picks this cluster back up rather than
            # silently dropping it - never reserve the fingerprint before
            # the suggestion the reservation is supposed to guard actually
            # exists.
            print(str(exc))
            return 3
        # `store.upsert_unique` (core/store.py) is the same "did this thing
        # already happen" primitive every other agent in this family uses -
        # it closes the tiny race where two runs both passed the SELECT
        # above for the same fingerprint before either had inserted a row.
        # Reserved here, right alongside the insert it guards, never before
        # the model call above has actually produced a suggestion. The key
        # includes how many proposals this pattern has had before (0 for a
        # pattern's first-ever proposal) so a *legitimate* new proposal -
        # after the previous one for this pattern was rejected, per
        # workflows/85-coach-weekly.md - gets its own fresh reservation
        # rather than being blocked forever by the first one's marker.
        attempt = store.db.execute(
            "SELECT COUNT(*) AS n FROM coach_proposals WHERE pattern=?",
            (c["pattern"],)).fetchone()["n"]
        _marker, is_new = store.upsert_unique(
            "coach_proposal", f"{c['pattern']}:{attempt}", payload={"intent": c["intent"]})
        if not is_new:
            skipped += 1
            continue
        store.db.execute(
            "INSERT INTO coach_proposals (id, created_at, pattern, intent, cluster_size, "
            "example_before, example_after, suggested_fix, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (sx.new_id(), utcnow(), c["pattern"], c["intent"], c["cluster_size"],
             c["example_before"][:2000], c["example_after"][:2000], suggestion, "pending"))
        created += 1
    print(f"{created} proposal(s) from {len(clusters)} cluster(s) of {len(learnings)} "
         f"learning(s).")
    if skipped:
        print(f"{skipped} cluster(s) already have an open or accepted proposal - not "
             f"duplicated.")
    if created:
        print("Run `python3 tools/coach.py list` to review them.")
    return 0


def cmd_list(store: Store, args) -> int:
    rows = store.db.execute(
        "SELECT * FROM coach_proposals WHERE status='pending' ORDER BY created_at ASC").fetchall()
    if not rows:
        print("No pending proposals.")
        return 0
    print(f"{len(rows)} proposal(s) waiting:\n")
    for r in rows:
        print(f"  {r['id']}  {r['pattern']:<16} cluster={r['cluster_size']:<3} "
             f"{r['suggested_fix'][:70]}")
    print("\nRun `python3 tools/coach.py show <id>` for the full example.")
    return 0


def cmd_show(store: Store, args) -> int:
    row = store.db.execute("SELECT * FROM coach_proposals WHERE id=?", (args.id,)).fetchone()
    if row is None:
        print(f"error: no proposal {args.id}", file=sys.stderr)
        return 1
    for k, v in dict(row).items():
        print(f"{k}: {v}")
    return 0


def _decide(store: Store, proposal_id: str, status: str, note: str) -> int:
    row = store.db.execute("SELECT id FROM coach_proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        print(f"error: no proposal {proposal_id}", file=sys.stderr)
        return 1
    store.db.execute("UPDATE coach_proposals SET status=?, decided_at=? WHERE id=?",
                     (status, utcnow(), proposal_id))
    store.record_event(None, "human", f"coach_proposal_{status}",
                       {"proposal_id": proposal_id, "note": note})
    print(f"{status} {proposal_id}")
    return 0


def cmd_accept(store: Store, args) -> int:
    return _decide(store, args.id, "accepted", args.note or "")


def cmd_reject(store: Store, args) -> int:
    return _decide(store, args.id, "rejected", args.note or "")


def cmd_apply(store: Store, args) -> int:
    rows = store.db.execute(
        "SELECT * FROM coach_proposals WHERE status='accepted' AND "
        "(applied_at IS NULL OR applied_at='') ORDER BY created_at ASC").fetchall()
    if not rows:
        print("Nothing accepted is waiting to be applied.")
        return 0
    is_new = not RULES_FILE.exists()
    with RULES_FILE.open("a", encoding="utf-8") as fh:
        if is_new:
            fh.write("# Rules learned from human corrections\n\n"
                    "<!-- Written by `tools/coach.py apply`. One line per accepted proposal, "
                    "oldest first. Edit or delete lines by hand any time - this is a plain "
                    "file, not a database. See knowledge/README.md. -->\n\n")
        for row in rows:
            fh.write(f"- ({row['intent'] or 'general'}) {row['suggested_fix']}\n")
    ids = [r["id"] for r in rows]
    store.db.executemany("UPDATE coach_proposals SET status='applied', applied_at=? WHERE id=?",
                         [(utcnow(), i) for i in ids])
    print(f"Applied {len(rows)} proposal(s) to {RULES_FILE}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="cluster learnings + suggest fixes")
    p_analyze.add_argument("--provider", default=None)

    sub.add_parser("list", help="pending proposals")

    p_show = sub.add_parser("show", help="full detail for one proposal")
    p_show.add_argument("id")

    p_accept = sub.add_parser("accept", help="human accepts a proposal")
    p_accept.add_argument("id")
    p_accept.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="human rejects a proposal")
    p_reject.add_argument("id")
    p_reject.add_argument("--note", default="")

    sub.add_parser("apply", help="write accepted proposals into knowledge/rules.md")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    sx.ensure_schema(store)
    try:
        if args.command == "analyze":
            return cmd_analyze(store, settings, args)
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "accept":
            return cmd_accept(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "apply":
            return cmd_apply(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except (LLMError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
