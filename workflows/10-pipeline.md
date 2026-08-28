# Workflow: the pipeline (Loop A)

Objective: turn a new group/event/VIP enquiry into a priced, drafted reply
waiting for a human, and chase any lead that has gone quiet since we
answered - without ever sending anything on our own.

Run this every 15 minutes (`config/agent.yaml`'s `schedule.pipeline`) or on
demand. `tools/engine.py` does the work; this file is the SOP.

## Steps

1. **Run one pass.**
   ```bash
   make run
   make run ARGS="--limit 5"       # just the first five messages
   make run ARGS="--dry-run"       # compute everything, write nothing at all
   make run ARGS="--provider mock" # preview without a real model call
   ```
   Each unread enquiry is: classified (kind, headcount, nights, discount ask,
   language - one model call), checked against real availability
   (`tools/engine.py:nearest_midweek_block`, no model), priced (a formula, no
   model), and drafted (prose only, one model call, given the already-computed
   facts). See `docs/how-it-works.md` for the full flow and why the reply's
   language can differ from the enquiry's.

2. **If it exits 3**, `llm.provider: interactive` parked a prompt. Read
   `data/pending/*.prompt.md`, write your answer as JSON to the matching
   `*.answer.json`, and run the same command again.

3. **See what is waiting.**
   ```bash
   python3 tools/pipeline.py funnel
   make review
   ```
   `funnel` orders every live lead the way `pipeline.value_priority` says to
   (highest value first by default) and shows the open pipeline total.
   `make review` is the general queue - see `workflows/80-review.md` for
   approve/edit/reject/send.

4. **A lead the guest already replied to.** Once you have handled their reply
   by hand (or it shows up as a new inbound item next pass), close the
   follow-up task so the sweep does not chase a dead conversation:
   ```bash
   python3 tools/pipeline.py reply <item-id> [--stage qualified]
   ```

5. **Move a lead through the funnel.**
   ```bash
   python3 tools/pipeline.py advance-stage <item-id> --stage proposal
   ```
   Stages: `inquiry -> qualified -> proposal -> contract -> won | lost`.

6. **The stale sweep runs automatically** on every `make run` pass (see
   `docs/how-it-works.md` design decision 1): a lead whose reply was sent but
   who has gone quiet past `pipeline.stale_after_days` gets one nudge drafted
   and queued, up to `pipeline.max_follow_ups` times, then is escalated
   `needs_human` instead of chased again. Anything sitting unreviewed
   (nobody has even looked at the first draft) past the same window is marked
   `stale` - see them with:
   ```bash
   python3 tools/pipeline.py stale
   ```

## Rules

- **Group, multi-room and VIP only.** A single-room enquiry is classified
  `single_room`, always routed to a person, and the draft points the guest at
  the regular booking page - see `docs/safety.md`.
- **Discounting never exceeds the floor without sign-off.** `tools/engine.py`
  clamps the number before the model ever sees it - see
  `docs/how-it-works.md` design decision 3.
- **Never quotes space that was not checked.** If nothing is available in the
  published window, the draft says so honestly instead of inventing a date.
- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
