# Workflow: Email Optimizer / Coach AI - weekly review

Objective: turn a week of human corrections - across the pipeline, outreach
and win-back - into concrete improvements, without the coach ever touching a
lead, a prospect or a guest, or changing anything on its own.

Run this weekly (`config/agent.yaml`'s `schedule.coach`, Monday 03:00 by
default). It reads `learnings` (every edit and reject from
`workflows/80-review.md`, across all four draft kinds) - see
`docs/how-it-works.md` and `tools/coach.py`.

## Steps

1. **Analyze the week.**
   ```bash
   python3 tools/coach.py analyze
   ```
   Corrections are grouped by the intent or kind they happened on
   (`tools/coach.py:cluster_learnings`) - a lead reply groups by its
   `conference`/`wedding`/`incentive`/... kind, an outreach correction groups
   by `outreach_step`, a win-back correction by its reason category. A group
   at or above `coach.min_cluster_size` (default 2) is a real pattern and
   gets one model call to turn it into a concrete suggestion - a proposal,
   `status: pending`.

2. **Review the proposals.**
   ```bash
   python3 tools/coach.py list
   python3 tools/coach.py show <id>
   ```
   Each one names a pattern, how many corrections it came from, a
   before/after example, and a suggested fix pointing at a specific file -
   usually a line to add to `config/agent.yaml` (a pricing or threshold
   rule), `knowledge/property.md`, or `knowledge/rules.md`.

3. **Decide, one at a time.**
   ```bash
   python3 tools/coach.py accept <id> [--note "..."]
   python3 tools/coach.py reject <id> [--note "why not"]
   ```
   A rejected proposal changes nothing and is not retried automatically.

4. **Apply what was accepted.**
   ```bash
   python3 tools/coach.py apply
   ```
   Writes one line per accepted, not-yet-applied proposal to
   `knowledge/rules.md` (created on first use - see `knowledge/README.md`)
   and marks each `applied`. `knowledge/rules.md` is not read into a prompt
   automatically by every task in this repo - check `prompts/draft.md` and
   `prompts/classify.md` reference it, or fold a rule into
   `config/agent.yaml` directly when it is really a threshold, not a fact.

5. **Watch the trend.**
   ```bash
   make report
   ```
   The edit rate this prints is the number this whole loop exists to bring
   down - the roster's promise is that an agent's edit rate should fall below
   10% as it earns full autonomy.

## Rules

- The coach never talks to a lead, a prospect or a guest, and never changes
  a prompt, a config value or a knowledge file on its own - only `apply`,
  after a human `accept`, does that, and only for the one proposal accepted.
- A proposal below `coach.min_cluster_size` stays a `learnings` row, not
  noise thrown away - it counts again next week if the pattern repeats.
- If `llm.provider: interactive` and `analyze` pauses mid-batch (exit code
  3), answer the parked prompt and re-run - proposals already created stand,
  the run picks up the remaining clusters.
- **`analyze` is safe to run more than once.** A cluster that already has a
  `pending` or `accepted` proposal is skipped, not re-proposed - no
  duplicate proposal and no duplicate model call. Running the weekly job
  twice, or retrying after an interactive pause, never doubles up.
- If a suggestion is vague or wrong, `reject` it and, if you can, tighten
  `prompts/coach-suggestion.md` rather than trying to fix it by hand - the
  next cluster will hit the same prompt.
