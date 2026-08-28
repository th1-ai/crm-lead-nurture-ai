#!/usr/bin/env python3
"""tools/run.py - CRM / Lead Nurture AI's main loop (Loop A - the pipeline).

    python3 tools/run.py --once
    python3 tools/run.py --watch
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --limit 5
    python3 tools/run.py --once --provider mock

One pass: read unread lead email, classify + price + draft each new one, then
sweep every follow-up task that has gone stale (docs/how-it-works.md design
decision 1) and draft a nudge. Loop B (outreach) is a separate tool -
`tools/outreach.py tick` - on its own schedule; see workflows/20-outreach.md.

Exit codes: 0 ok, 3 waiting on an `interactive` answer, 1 a real error.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_pms  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.store import Store, StoreError, utcnow  # noqa: E402

import engine  # noqa: E402

log = get_logger("run")


def sweep_stale(settings, store) -> dict:
    """Draft a nudge for every lead follow-up task that has come due."""
    stats = {"nudged": 0, "escalated": 0}
    for task in store.due_tasks(kind="lead_followup"):
        item = store.get_item(task.ref_id)
        if item is None or item.review_status == "rejected":
            store.close_task(task.id, status="done")
            continue
        if item.review_status not in ("sent", "auto_sent"):
            # our own reply has not gone out yet - nothing to chase up on.
            # A raw postpone, not advance_task(): this must NOT count against
            # max_follow_ups, or a slow human reviewer would trip the
            # escalation limit before a single guest-facing nudge went out.
            due = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
            store.db.execute("UPDATE tasks SET next_action_due=?, updated_at=? WHERE id=?",
                             (due, utcnow(), task.id))
            continue
        follow_up_count = task.follow_up_count + 1
        gap_days = int(settings.agent_get("pipeline.follow_up_gap_days", 4))
        advanced = store.advance_task(task.id, gap_days=gap_days,
                                      note=f"nudge #{follow_up_count} for {item.id}")
        if advanced.status == "escalated":
            store.transition(item.id, "needs_human", actor="agent",
                             detail={"reason": "no reply after follow-ups"})
            stats["escalated"] += 1
            continue
        payload = item.payload or {}
        nudge = engine.build_followup_nudge(hotel_name=settings.hotel.name,
                                            subject=payload.get("subject", "your enquiry"),
                                            follow_up_count=follow_up_count)
        nudge_item = store.upsert_item(
            "email", f"{item.id}:followup:{follow_up_count}", kind="lead_reply",
            payload={**payload, "follow_up_of": item.id})
        if nudge_item.review_status == "new":
            store.set_fields(nudge_item.id, draft=nudge, intent=payload.get("kind", "other"),
                             confidence=1.0)
            store.transition(nudge_item.id, "pending_review", actor="agent")
        stats["nudged"] += 1
    stale = store.mark_stale(older_than_hours=int(settings.agent_get("pipeline.stale_after_days", 3)) * 24)
    if stale:
        log.warn("items gone stale - unreviewed too long", count=len(stale))
    return stats


def one_pass(settings, store, pms, *, limit: int, provider: str | None) -> tuple[int, dict]:
    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "skipped": 0}
    # --dry-run writes nothing at all - not even a `runs` row or a stale sweep
    # write - so two dry passes over the same fixtures are both true no-ops.
    dry = settings.dry_run
    run = Run("pipeline", None if dry else settings, None if dry else store)
    with run:
        email = get_email(settings)
        messages = email.fetch_unread(limit=limit)
        # a pure read - safe even in --dry-run, and it correctly skips leads a
        # real (non-dry) pass already fully processed
        seen = store.already_processed("email", [m.id for m in messages])
        for msg in messages:
            if msg.id in seen:
                stats["skipped"] += 1
                continue
            try:
                item, did_work = engine.process_lead_email(settings, store, pms, msg,
                                                            provider=provider)
            except LLMPendingInteractive as exc:
                run.stats = dict(stats)
                print(str(exc))
                return 3, stats
            if not did_work:
                stats["skipped"] += 1
                continue
            stats["processed"] += 1
            stats["drafted"] += 1
            if item.review_status == "needs_human":
                stats["needs_human"] += 1
            log.info("queued", item_id=item.id, kind=item.intent, status=item.review_status)
        if dry:
            run.stats = dict(stats)
            return 0, stats

        nudge_stats = sweep_stale(settings, store)
        stats["drafted"] += nudge_stats["nudged"]
        reaped = store.reap_stuck_sending()
        if reaped:
            log.warn("reaped stuck sends", count=len(reaped))
        run.stats = dict(stats)
    return 0, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=20, help="max messages per pass")
    parser.add_argument("--provider", default=None, help="override llm.provider for this run")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 900)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(provider=args.provider, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    pms = get_pms(settings)
    try:
        if args.watch:
            poll_seconds = args.poll_seconds or int(settings.agent_get("poll_seconds", 900))
            while True:
                code, stats = one_pass(settings, store, pms, limit=args.limit,
                                       provider=args.provider)
                print(summary_line(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = one_pass(settings, store, pms, limit=args.limit, provider=args.provider)
        print(summary_line(stats, settings.mode))
        return code
    except (AdapterError, LLMError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
