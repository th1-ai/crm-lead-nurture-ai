# Sub-agents in this repo

CRM / Lead Nurture AI folds in one sub-agent and one coach layer. Both share
this repo's `core/`, `data/agent.db` and review queue - there is nothing
extra to install. Each is off or on independently; see `config/agent.yaml`'s
`subagents` block.

## Win-Back / Loyalty AI - "The Diplomat"

**Does.** Spots lapsing past guests, ranks them by lifetime value, and
writes each a personal win-back citing the true reason to return - the
noisy-room issue you've since fixed, the suite they loved. When a guest says
yes it books the repeat stay straight into the PMS. Runs a light loyalty
program alongside (birthdays, anniversaries, return-stay perks).

**Won't.** Caps discounting; won't spam.

**Why.** A repeat guest is far cheaper than a new one; most properties never
re-market.

**Output.** 10–25% of past guests are reachable for a repeat stay with the
right nudge.

**Off by default.** The Pursuer's pipeline and outreach work fully without
it - turn it on once you trust the cohort your PMS history produces; see
`workflows/25-win-back.md`.

**What is honestly not built.** The "light loyalty program" half of the
promise - birthday, anniversary and return-stay-perk nudges on a calendar
trigger - has no code in this repo. `tools/winback.py` covers the win-back
half only (lapsed-guest detection, ranking, personal letters, the booking
record). Adding the loyalty half is real, separate work: a second workflow
reading a guest's date-of-birth/anniversary field and a perk ledger, on its
own schedule, sharing the same review queue (`kind: loyalty_nudge`) and the
same deterministic-letter pattern as `tools/winback.py:draft_letter`.

**"Books the repeat stay straight into the PMS", precisely.** No PMS
exposes a generic "create a reservation" call through one shared API shape,
so `core/adapters/base.py`'s `PMS` interface deliberately does not have one.
`tools/winback.py mark-accepted` records the win in this repo's own ledger,
adds a note to the guest's most recent PMS reservation if one exists, and
calls a `create_reservation` method automatically if your own PMS adapter
grows one - see `docs/how-it-works.md` design decision 6 and
`docs/integrations.md#implement-your-own`.

## Email Optimizer / Coach AI

**Does.** The coach class. Each week it reads every guest reply a human
edited, rejected, or thumbed-down, clusters the corrections into patterns,
applies the safe knowledge-base fixes itself, and proposes the rest. A
sibling captures every human edit as a training pair, so the whole roster
keeps getting sharper. A live quality board tracks the numbers that matter -
replies sent unchanged, edit severity, hand-off rate - so you watch each
agent earn its autonomy week by week.

**Won't.** Doesn't talk to guests. Holds the higher-judgement changes for a
human nod; applies the clear-cut ones itself.

**Output.** Drives the human-edit rate down week over week; agents graduate
to full autonomy as their edit rate falls below 10%.

**On by default** (analysis only - it never touches a lead, a prospect or a
guest, so leaving it on is safe from day one); see
`workflows/85-coach-weekly.md`. This build implements the clustering half of
the promise - the "applies the safe knowledge-base fixes itself" half is a
human-gated `apply` step, not an automatic one; see the workflow for why.
