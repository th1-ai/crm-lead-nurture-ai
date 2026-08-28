# Workflow: outreach (Loop B)

Objective: run the outbound side of the desk - target avatars, signal-driven
prospecting, enrichment, a multi-channel sequence inside safe sending
limits, and a reply inbox that hands a booked meeting back to the pipeline.

Entirely deterministic (`docs/how-it-works.md` design decision 7) - no model
call anywhere in this file's tools. `tools/outreach.py tick` runs every 30
minutes (`config/agent.yaml`'s `schedule.outreach_tick`, skips weekends).

## First time: build an audience

1. **See the signal sources.**
   ```bash
   python3 tools/outreach.py sources
   ```
   A new source starts `pending`. Only `approved`/`testing` sources feed a
   scan while `outreach.source_vetting` is on.
   ```bash
   python3 tools/outreach.py approve-source <id>
   ```

2. **Scan for prospects matching an avatar.**
   ```bash
   python3 tools/outreach.py scan av-mice
   ```
   Prints how many prospects were revealed and which sources were held out
   for not being vetted yet.

3. **Find verified contact details.**
   ```bash
   python3 tools/outreach.py enrich av-mice
   ```
   Costs real money per lookup (`enrichment.cost_per_lookup` in
   `config/agent.yaml`) - `enrichment.monthly_budget_eur` is the ceiling.
   Do-not-contact prospects are skipped, never enriched. See
   `docs/integrations.md` for Hunter.io / Findymail setup, or leave
   `enrichment.provider: mock` for a zero-credential preview.

4. **Build and launch a campaign.**
   ```bash
   python3 tools/outreach.py generate-campaign av-mice --name "Autumn MICE push"
   python3 tools/outreach.py launch <campaign-id>
   ```
   Launch is pre-flight gated: at least one prospect with a verified email
   and no do-not-contact flag, and `outreach.deliverability` (SPF/DKIM/DMARC)
   all green. A domain that fails deliverability blocks the launch until you
   fix your DNS records or swap the sending inbox - see `docs/integrations.md`.

## Every tick (scheduled)

```bash
python3 tools/outreach.py tick
```

Works every step whose turn has come, in order, respecting every guardrail:
- **Safe caps** (`outreach.daily_caps`, `outreach.safe_caps`): a channel over
  its cap for the day queues to tomorrow, visibly, in the tick's own summary
  line.
- **Warm-up ramp** (`outreach.warmup_ramp`): a channel's first
  `outreach.warmup_weeks` weeks are capped at `outreach.warmup_ramp_week1`,
  not its full daily cap.
- **Weekend pause** (`outreach.weekend_pause`): the whole tick is a no-op on
  Saturday and Sunday.
- **Stop on reply**: an enrollment marked `replied` (see below) is never
  advanced again.
- **Do-not-contact**: suppressed at scan/enrich time and again here as a
  backstop.
- **Stale LinkedIn invites**: an unaccepted connection past
  `outreach.withdraw_stale_invite_days` is marked withdrawn and logged - no
  send, no queue item, just a record (there is no universal "withdraw an
  invite" API - see `docs/how-it-works.md` design decision 8).

Every real send - including the "connect" step's manual visit-and-like
checklist, which routes through `messaging.notify_staff()` - is queued into
the ONE review queue first. Nothing here calls an adapter directly; see
`workflows/80-review.md`.

## When a prospect responds

```bash
python3 tools/outreach.py log-accept <enrollment-id>     # LinkedIn invite accepted
python3 tools/outreach.py log-reply <enrollment-id> --message "..."   # stops the sequence
python3 tools/outreach.py draft-reply <enrollment-id> --message "..." # queues a reply
python3 tools/outreach.py book-meeting <enrollment-id>                # a tracked link
python3 tools/outreach.py accept-meeting <meeting-id> --slot "Tue 11:00-11:20"
```

These are operator-triggered, not scheduled: check your inbox as you
normally would, and log what happened. `accept-meeting` hands a fresh lead
back to `tools/pipeline.py` at stage `inquiry` - this is the loop closing,
outreach feeding the pipeline it sits next to.

## Rules

- `outreach.ai_personalization` off makes every hook generic
  ("I'll keep this short.") - the provable toggle from the source engine.
  Leave it on for real sends.
- Nothing here auto-sends, in shadow or in live mode - see
  `docs/how-it-works.md` design decision 10.
- LinkedIn and Instagram automation route through `systems.messaging` -
  UniPile supports both in reality; the `mock`/`webhook` adapters simulate or
  forward. See `docs/integrations.md`.
