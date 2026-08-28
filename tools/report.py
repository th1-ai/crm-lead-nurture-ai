#!/usr/bin/env python3
"""tools/report.py - what the agent did, and what it cost.

    make report
    python3 tools/report.py
    python3 tools/report.py --json

Reads data/agent.db - nothing here calls a model or an adapter. Numbers tied
to roster claims (README.md section 2, docs/benefits.md):

``volumes``            items by kind and by review_status right now.
``auto-handled %``     of everything terminal, the share sent with no edit.
``edit %``              of everything approved/edited, how often a human had
                        to rewrite it - the number workflows/85-coach-weekly.md
                        exists to drive down.
``outreach funnel``     enrolled / accepted / replied / withdrawn / booked.
``winback``             cohort size, lifetime spend addressed, bookings recorded.
``spend``               LLM calls, tokens and cost (core.store.usage_totals).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.store import Store, StoreError, TERMINAL  # noqa: E402

import store_ext as sx  # noqa: E402


def volumes(store: Store) -> dict:
    by_status = store.counts()
    rows = store.db.execute("SELECT kind, COUNT(*) AS n FROM items GROUP BY kind").fetchall()
    by_kind = {r["kind"]: r["n"] for r in rows}
    return {"by_kind": by_kind, "by_status": by_status, "total": sum(by_kind.values())}


def auto_handled(store: Store) -> dict:
    counts = store.counts()
    total_terminal = sum(counts.get(s, 0) for s in TERMINAL)
    auto = counts.get("auto_sent", 0)
    return {"auto_sent": auto, "terminal": total_terminal,
           "rate": (auto / total_terminal) if total_terminal else 0.0}


def edit_stats(store: Store) -> dict:
    rows = store.db.execute(
        "SELECT item_id, action FROM events WHERE action IN "
        "('status:edited', 'status:approved')").fetchall()
    edited = {r["item_id"] for r in rows if r["action"] == "status:edited"}
    approved = {r["item_id"] for r in rows if r["action"] == "status:approved"} - edited
    total = len(edited) + len(approved)
    return {"edited": len(edited), "approved_unchanged": len(approved),
           "rate": (len(edited) / total) if total else 0.0}


def outreach_funnel(store: Store) -> dict:
    rows = store.db.execute(
        "SELECT status, COUNT(*) AS n FROM outreach_enrollments GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def winback_summary(store: Store) -> dict:
    rows = sx.list_cohort(store)
    bookings = sx.list_winback_bookings(store)
    return {"cohort": len(rows), "lifetime_spend_addressed": sum(r["lifetime_spend"] for r in rows),
           "rebooked": sum(1 for r in rows if r["status"] == "rebooked"),
           "booking_revenue": sum(b["total_eur"] for b in bookings)}


def spend(store: Store, since: str | None = None) -> dict:
    return store.usage_totals(since=since)


def build_report(store: Store, since: str | None = None) -> dict:
    return {"volumes": volumes(store), "auto_handled": auto_handled(store),
           "edits": edit_stats(store), "outreach_funnel": outreach_funnel(store),
           "winback": winback_summary(store), "spend": spend(store, since=since)}


def print_report(report: dict) -> None:
    v = report["volumes"]
    print("CRM / Lead Nurture AI - report\n")
    print(f"Items: {v['total']} total")
    if v["by_kind"]:
        print("  by kind:   " + ", ".join(f"{k}={n}" for k, n in sorted(v["by_kind"].items())))
    if v["by_status"]:
        print("  by status: " + ", ".join(f"{k}={n}" for k, n in sorted(v["by_status"].items())))

    a = report["auto_handled"]
    print(f"\nAuto-handled: {a['auto_sent']}/{a['terminal']} terminal item(s) "
         f"({a['rate']*100:.0f}%) needed no human touch at all.")

    e = report["edits"]
    print(f"Edit rate: {e['edited']}/{e['edited'] + e['approved_unchanged']} approved draft(s) "
         f"needed a rewrite ({e['rate']*100:.0f}%). See workflows/85-coach-weekly.md.")

    f = report["outreach_funnel"]
    if f:
        print("\nOutreach funnel: " + ", ".join(f"{k}={n}" for k, n in sorted(f.items())))
    else:
        print("\nOutreach funnel: no campaigns launched yet.")

    w = report["winback"]
    print(f"\nWin-back: {w['cohort']} guest(s) in the cohort, EUR "
         f"{w['lifetime_spend_addressed']:,.2f} of lifetime spend addressed, "
         f"{w['rebooked']} rebooked, EUR {w['booking_revenue']:,.2f} recorded.")

    s = report["spend"]
    print(f"\nSpend: {s['calls']} LLM call(s), {s['input_tokens']} input + "
         f"{s['output_tokens']} output token(s), USD {s['cost_usd']:.4f}.")
    if s["calls"] and s["cost_usd"] == 0.0:
        print("  (0.00 is expected on provider=mock, interactive or claude-code - only the "
             "anthropic provider bills per token.)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--since", default=None, help="ISO timestamp - only spend since then")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    sx.ensure_schema(store)
    try:
        report = build_report(store, since=args.since)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
