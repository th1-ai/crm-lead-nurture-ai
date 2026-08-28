# Measuring the benefit

## The promise

**Output.** Recovers 5–15% of otherwise-lost group and VIP bookings by
chasing stale leads.

**ROI.** +10% Group & VIP bookings recovered (revenue)

Those numbers come from the demo platform's roster and describe real
production use elsewhere in the family this repo was built from - they are
not a guarantee for your property. `make report` is how you find out what is
actually true for you.

## What to track

| Metric | Where it comes from | What it tells you |
|---|---|---|
| Pipeline volume and value | `python3 tools/pipeline.py funnel` | how many live leads, and how much open pipeline value, is sitting in each stage |
| Auto-handled % | items `sent`/`auto_sent` with no edit, over everything terminal | honestly close to 0% - every draft here waits for a human by design (see `docs/safety.md`) |
| Edit rate | edited vs. approved-unchanged, from `learnings`, across all four draft kinds | the number `workflows/85-coach-weekly.md` exists to bring down - below 10% is the roster's own bar for "earned autonomy" |
| Outreach funnel | `python3 tools/report.py` "Outreach funnel" | prospects queued, accepted, replied, withdrawn, booked |
| Win-back | `python3 tools/report.py` "Win-back" | cohort size, lifetime spend addressed, rebookings recorded (only if the sub-agent is enabled) |
| Spend | `core.llm`'s usage logging | LLM calls, tokens and cost from the two pipeline model calls (classify, draft) plus the coach - `0.00` is expected and correct on `mock`, `interactive` or `claude-code`; only `anthropic` bills per token |

Run it any time:

```bash
make report
python3 tools/report.py --json     # for a dashboard or a spreadsheet import
```

## Reading the numbers honestly

This repo ships in `mode: shadow` - every reply, every outreach step, every
win-back letter waits for a human before anything leaves the building. The
roster's "recovers 5-15% of otherwise-lost bookings" describes a property
that has gone live, tuned its `knowledge/` and `config/agent.yaml` numbers,
and let the pipeline's stale-lead sweep run for weeks - not day one on
`mock` fixtures. Watch the **edit rate falling** and the **open pipeline
value not quietly going stale** (`python3 tools/pipeline.py stale`) as the
honest leading indicators before that point.

## The revenue case

The claim is specifically about **group and VIP bookings that would
otherwise be lost** - not every enquiry this desk touches. `est_value` on
each pipeline item (visible in `python3 tools/pipeline.py funnel` and in
`python3 tools/review.py show <id>`) is what the stale-lead sweep and the
funnel ordering are protecting; comparing won/lost stages before and after
adopting this agent is the honest way to see whether the 5-15% recovery
figure holds for your property. There is no synthetic number this repo can
print that substitutes for that comparison.

## Caveats, plainly

- Numbers are only as good as `config/agent.yaml`'s pricing block
  (`event_room_type`, `day_delegate_fee`, `dinner_fee`) and `knowledge/`. A
  property still on the shipped defaults will see `est_value` figures that
  do not match its real rates.
- "Spots the ones going stale" (the roster's `why`) is implemented as a
  tickler over sent-but-unanswered replies plus an unreviewed-backlog flag -
  see `docs/how-it-works.md` design decision 1. It has no history to draw on
  until real leads have gone through at least one `pipeline.stale_after_days`
  cycle.
- Win-back's revenue number only exists if the sub-agent is turned on and
  `pms.list_reservations()` has real, checked-out history further back than
  `winback.lapse_after_days` - see `workflows/25-win-back.md`.
- `spend` only ever reflects the `anthropic` provider. Choosing
  `interactive` or `claude-code` to run on a subscription instead is a
  deliberate cost decision covered in `docs/safety.md` - `tools/report.py`
  will correctly show USD 0.00 in that case, which is not the same as "no
  cost".
