# Workflow: troubleshooting

Read the whole error before doing anything - every tool here is written to
say what broke and what to do about it. If you fix something not covered
below, add it here.

## `make doctor` shows a FAIL

Each `FAIL` line has a `->` fix hint right under it. Common ones:

- **`hotel identity`: name is still 'Hotel Aurora'.** Expected on a fresh
  clone. Edit `config/hotel.yaml`.
- **`pipeline rules`: missing ... in config/agent.yaml.** Copy
  `config/agent.example.yaml` to `config/agent.yaml`.
- **`llm provider`: claude-code selected but `claude` is not on PATH.**
  Install Claude Code, or switch `llm.provider` to `interactive` or
  `anthropic`.
- **`llm provider`: ANTHROPIC_API_KEY is not set.** Add it to `.env`, or
  switch `llm.provider` to `claude-code` or `interactive`.
- **An adapter shows FAIL, not warn.** `universal`/`built` adapters fail
  loud when misconfigured (a `warn` is reserved for stubs). Read the
  `detail` column - it names the missing file or variable.

## `make demo` does not print `DEMO OK`

- Make sure `make setup` ran first (`.venv` must exist).
- `tools/demo.py` uses `load_settings(demo=True)`, which forces
  `llm.provider=mock` and the `mock` adapter for every system, and reads
  `fixtures/inbound/*.json` and `fixtures/hotel/*.json` - if you deleted or
  renamed those files, restore them from git.
- Read the traceback if there is one; `tools/demo.py` does not swallow
  errors on purpose, so a fixture problem shows up immediately.

## `make run` exits with code 3

Not an error. `llm.provider: interactive` parked a prompt. Read
`data/pending/*.prompt.md`, write your answer as JSON to the matching
`*.answer.json` (matching the schema shown, no prose, no code fence), and
run the same command again. `tools/coach.py analyze` behaves the same way.

## `make run ARGS="--dry-run"` and nothing shows up in `make review`

That is correct - `--dry-run` computes everything (including a real model
call, if you want to preview a prompt change) and writes nothing at all, not
even the item row. Run without `--dry-run` to actually queue it.

## An item is stuck at `sending`

A process died between claiming an item and finishing the send.
`tools/run.py` calls `store.reap_stuck_sending()` on every pipeline pass,
which moves anything stuck for more than 30 minutes to `failed` so you see
it in the queue instead of it vanishing. Use
`python3 tools/review.py retry <id>` once the cause is fixed.

## An enquiry keeps needing a human even though it looks routine

Check `config/agent.yaml`'s `pipeline.large_group_headcount` and
`pipeline.confidence_threshold` - this desk is built for group/multi-room/
VIP business, so most enquiries it handles are meant to trip that gate. If
the enquiry's language is not in `hotel.languages`, that alone forces
`needs_human` - add the language if you want it handled directly.

## Outreach: "blocked: no prospect with a verified email"

`launch` needs at least one enriched, non-do-not-contact prospect. Run
`python3 tools/outreach.py scan <avatar>` then `enrich <avatar>` first, and
check `python3 tools/outreach.py sources` - a source stuck `pending` holds
its prospects out of every scan until you `approve-source` it.

## Outreach: "blocked: deliverability is not green"

Fix your SPF/DKIM/DMARC records for the sending domain, or point
`systems.email` at an inbox that already passes, then update
`config/agent.yaml`'s `outreach.deliverability` block once you have
verified it yourself - this repo does not check DNS for you.

## Win-back: the cohort is empty

`tools/winback.py cohort` needs real, checked-out reservation history in
your PMS (or `fixtures/hotel/reservations.json` in `mock`/`csv`) spanning
further back than `winback.lapse_after_days`, with guests who have at least
`winback.min_qualifying_stays` past stays. A brand-new property has nobody
to win back yet - that is expected, not a bug.

## `python3 tools/*.py` says `ModuleNotFoundError: No module named 'core'`

You ran it with a Python that is not the repo's virtualenv, or from outside
the repo root. Use `make run` / `make doctor` / etc. (they call
`.venv/bin/python` for you), or run `.venv/bin/python tools/run.py` directly
from the repo root.

## Still stuck

`data/logs/*.jsonl` has every decision the agent made, in order, with a run
id. `python3 tools/review.py show <id>` has the full event trail for one
item. If neither explains it, that is a real bug - describe exactly what you
ran and what you expected, and ask.
