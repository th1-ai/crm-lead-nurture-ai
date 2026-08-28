#!/usr/bin/env python3
"""tools/demo.py - the whole loop on the bundled fixtures, zero credentials.

    make demo
    python3 tools/demo.py

`load_settings(demo=True)` forces `mock` provider, `shadow` mode and the
`mock` adapter for every system, whatever config/hotel.yaml says. Runs
against its own database (`data/demo/demo.db`), never `data/agent.db`, so
running it twice always shows the same story and never collides with a real
run. Walks all three parts of this repo: the pipeline (Loop A), outreach
(Loop B), and win-back (the sub-agent, run once here so the demo proves it
works even though it ships disabled).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_pms  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.llm import LLMError  # noqa: E402
from core.log import summary_line  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

import engine  # noqa: E402
import outreach  # noqa: E402
import store_ext as sx  # noqa: E402
import winback  # noqa: E402


class _Args:
    """A tiny stand-in for argparse.Namespace, for calling outreach cmd_* directly."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def main() -> int:
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    try:
        demo_db = sub_data_dir("demo") / "demo.db"
        if demo_db.exists():
            demo_db.unlink()
        store = Store(settings, path=demo_db)
        sx.ensure_schema(store)
        pms = get_pms(settings)
        email = get_email(settings)

        print(f"CRM / Lead Nurture AI demo - {settings.hotel.name}\n")

        # -- Loop A: the pipeline ------------------------------------------------
        messages = email.fetch_unread(limit=50)
        if not messages:
            print("no fixtures found in fixtures/inbound/ - nothing to demo", file=sys.stderr)
            return 1
        stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0}
        print(f"Pipeline - {len(messages)} inbound enquiry/enquiries:")
        for msg in messages:
            item, _ = engine.process_lead_email(settings, store, pms, msg, provider="mock")
            stats["processed"] += 1
            stats["drafted"] += 1
            if item.review_status == "needs_human":
                stats["needs_human"] += 1
            payload = item.payload or {}
            print(f"  {msg.id}: \"{msg.subject}\" -> kind={item.intent} "
                 f"est_value=EUR{payload.get('est_value', 0):,.0f} status={item.review_status}")
        print(f"{stats['needs_human']} of {stats['processed']} need a person to look first.\n")

        # -- Loop B: outreach ------------------------------------------------------
        print("Outreach:")
        outreach.cmd_scan(store, settings, _Args(avatar="av-mice"))
        outreach.cmd_enrich(store, settings, _Args(avatar="av-mice"))
        outreach.cmd_generate_campaign(store, _Args(avatar="av-mice", name="MICE prospecting",
                                                    kind="mice"))
        campaign_id = sx.list_campaigns(store)[-1]["id"]
        outreach.cmd_launch(store, settings, _Args(campaign_id=campaign_id))
        outreach.cmd_tick(store, settings, _Args(limit=50))
        outreach_queued = len(store.list_items(kind="outreach_step", limit=200))
        print(f"  {outreach_queued} outreach step(s) queued for review.\n")

        # -- Win-back (off by default in config/agent.yaml - proven here anyway) --
        print("Win-back (sub-agent, off by default):")
        winback.cmd_cohort(store, settings, pms, _Args())
        winback.cmd_draft(store, settings, _Args())
        winback_queued = len(store.list_items(kind="winback_letter", limit=200))
        print(f"  {winback_queued} win-back letter(s) queued for review.\n")

        stats["sent"] = 0
        print("Nothing was sent: mode is shadow, and demo never calls send() at all.")
        print("Next: `make review` to see the drafts, or read workflows/10-pipeline.md.\n")
        print(f"DEMO OK — {summary_line(stats, settings.mode)}, {outreach_queued} outreach step(s), "
             f"{winback_queued} win-back letter(s)")
        store.close()
        return 0
    except (AdapterError, LLMError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
