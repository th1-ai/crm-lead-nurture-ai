#!/usr/bin/env python3
"""tools/pipeline.py - operator commands for the lead pipeline (Loop A).

    python3 tools/pipeline.py funnel                     # the desk, ordered by rule
    python3 tools/pipeline.py reply <item-id>             # log that the guest replied
    python3 tools/pipeline.py advance-stage <item-id> --stage qualified
    python3 tools/pipeline.py stale                       # what has gone quiet

Stages: inquiry -> qualified -> proposal -> contract -> won | lost.
`reply` closes the open follow-up task so `tools/run.py`'s stale sweep does
not chase a lead that has already answered - see docs/how-it-works.md design
decision 1 and `tools/engine.py:process_lead_email`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

import engine  # noqa: E402

STAGES = ("inquiry", "qualified", "proposal", "contract", "won", "lost")


def cmd_funnel(store: Store, settings, args) -> int:
    rows = []
    for item in store.list_items(kind="lead_reply", limit=500):
        payload = item.payload or {}
        if not payload.get("stage"):
            continue
        rows.append({"id": item.id, "est_value": payload.get("est_value", 0),
                    "received_at": item.created_at, "stage": payload.get("stage"),
                    "kind": payload.get("kind"), "status": item.review_status})
    value_priority = bool(settings.agent_get("pipeline.value_priority", True))
    ordered = engine.order_queue(rows, value_priority=value_priority)
    open_value = sum(r["est_value"] for r in ordered if r["stage"] not in ("won", "lost"))
    print(f"{len(ordered)} live lead(s), EUR {open_value:,.2f} of open pipeline "
         f"(ordered by {'value' if value_priority else 'arrival'}).\n")
    for r in ordered:
        print(f"  {r['id']}  {r['stage']:<10} {r['kind']:<11} EUR{r['est_value']:>9,.0f}  "
             f"{r['status']}")
    return 0


def cmd_reply(store: Store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    for task in store.due_tasks(kind="lead_followup", limit=1000):
        if task.ref_id == args.id:
            store.close_task(task.id, status="replied")
    if args.stage:
        payload = dict(item.payload or {})
        payload["stage"] = args.stage
        store.set_fields(item.id, payload=payload)
    print(f"{args.id}: reply logged, follow-up task closed"
         + (f", stage set to {args.stage}." if args.stage else "."))
    return 0


def cmd_advance_stage(store: Store, args) -> int:
    if args.stage not in STAGES:
        print(f"error: stage must be one of {', '.join(STAGES)}", file=sys.stderr)
        return 1
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    payload = dict(item.payload or {})
    payload["stage"] = args.stage
    store.set_fields(item.id, payload=payload)
    print(f"{args.id}: stage -> {args.stage}")
    return 0


def cmd_stale(store: Store, args) -> int:
    rows = store.list_items(status="stale", kind="lead_reply", limit=200)
    if not rows:
        print("Nothing has gone stale.")
        return 0
    for item in rows:
        payload = item.payload or {}
        print(f"  {item.id}  {payload.get('kind', '-'):<11} "
             f"{payload.get('subject', payload.get('subject', ''))[:50]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("funnel", help="the desk, ordered by the value-priority rule")

    p_reply = sub.add_parser("reply", help="log that the guest replied")
    p_reply.add_argument("id")
    p_reply.add_argument("--stage", default="")

    p_adv = sub.add_parser("advance-stage", help="move a lead to a new stage")
    p_adv.add_argument("id")
    p_adv.add_argument("--stage", required=True)

    sub.add_parser("stale", help="leads nobody has reviewed in a while")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.command == "funnel":
            return cmd_funnel(store, settings, args)
        if args.command == "reply":
            return cmd_reply(store, args)
        if args.command == "advance-stage":
            return cmd_advance_stage(store, args)
        if args.command == "stale":
            return cmd_stale(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
