# Workflow: Win-Back / Loyalty AI ("The Diplomat")

**Off by default.** The Pursuer works fully without this - see
`docs/sub-agents.md`. Turn it on in `config/agent.yaml`:

```yaml
subagents:
  win_back:
    enabled: true
```

Objective: find guests who have quietly stopped coming back, rank them by
what they are worth, and write each one a personal letter citing their real
history - never "we miss you", never a discount.

## Steps (run monthly, or on demand)

1. **Compute the cohort.**
   ```bash
   python3 tools/winback.py cohort
   ```
   Reads `pms.list_reservations()` and groups by guest: lapsed means no stay
   in `winback.lapse_after_days` (default ~9 months), qualifying means
   `winback.min_qualifying_stays` (default 2) or more past stays. Prints the
   cohort size, total lifetime spend addressed, and the top three by
   `lifetime_spend x stays`.

2. **Review the ranking.**
   ```bash
   python3 tools/winback.py list
   ```
   Each row shows stays, lifetime spend and the reason category
   (`noise` / `spa` / `unsold` / `price` / `general`) the letter will cite.

3. **Draft the letters.**
   ```bash
   python3 tools/winback.py draft
   ```
   One deterministic letter per undrafted guest, queued into the same review
   queue as everything else (`kind=winback_letter`). Never model-written -
   see `docs/how-it-works.md` design decision 5 - so the "no discount" rule
   is structural, not a prompt instruction that could be ignored.

4. **Approve, edit or reject** exactly as in `workflows/80-review.md`.

5. **When a guest says yes.**
   ```bash
   python3 tools/winback.py mark-accepted <cohort-id> [--checkin YYYY-MM-DD]
   ```
   Prices the stay with the house rate formula, records the rebooking in this
   agent's own ledger (the source for the Win-back revenue number in
   `make report`), and tries to note it on the guest's PMS record. **No PMS
   has a generic "create a reservation" call** - see `docs/how-it-works.md`
   design decision 6 - so unless your PMS adapter has grown a
   `create_reservation` method, you still enter the stay in your PMS
   yourself; the command tells you plainly when that is the case.
   ```bash
   python3 tools/winback.py mark-declined <cohort-id>   # if they say no
   ```

## Rules

- **Never a discount.** `winback.max_discount_pct` is `0`; the letter
  templates cannot emit a `%` sign - see `tools/winback.py:draft_letter`.
- **One letter per guest per run.** No follow-up sequence for win-back - a
  second nudge is a deliberate non-feature (`docs/how-it-works.md`).
- **Nothing sends itself.** Every letter waits for a human, exactly like the
  pipeline and outreach.
- The loyalty half of the roster promise (birthdays, anniversaries, return-
  stay perks) is not implemented - see `docs/sub-agents.md` for what would be
  needed to add it.
