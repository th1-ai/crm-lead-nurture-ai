# Workflow: working the review queue

Objective: turn a queued draft into a decision - approve, edit, or reject -
and, once approved, actually send it.

Nothing reaches a lead, a prospect or a lapsed guest without going through
this. `mode: shadow` blocks every guarded write, approved or not - approving
in shadow mode records your decision but nothing leaves the building until
`mode: live` (`workflows/90-go-live.md`). One queue, four kinds of draft:
`lead_reply` (the pipeline), `outreach_step` and `outreach_reply` (outbound),
`winback_letter` (the sub-agent, if enabled).

## Steps

1. **See what is waiting.**
   ```bash
   make review
   python3 tools/review.py list --kind lead_reply
   ```
   Each line shows the item id, status, kind, the pipeline's `kind`
   classification (conference/wedding/incentive/...), and a label (subject,
   org or guest name).

2. **Read one in full.**
   ```bash
   python3 tools/review.py show <id>
   ```
   Prints the original message, the facts the draft was built from, the
   draft itself, and the full event history. Summarise it in plain language -
   who this is, what they need, what was drafted, why it needs you - do not
   paste the raw JSON at a hotel operator.

3. **Decide.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py edit <id> --body-file my-version.txt [--subject "..."]
   python3 tools/review.py reject <id> --reason "wrong tone"
   ```
   `edit` records the before/after pair as a `learnings` row - that is what
   `workflows/85-coach-weekly.md` clusters into concrete fixes.

4. **Send what was approved.**
   ```bash
   python3 tools/review.py send
   ```
   Claims everything `approved`/`edited` and dispatches by channel: `email`
   (with the signature and AI-disclosure line appended automatically),
   `staff` (an internal nudge - `messaging.notify_staff()`, used for the
   LinkedIn manual-checklist step), or `linkedin`/`whatsapp`/`instagram_dm`
   (`messaging.send()`). Sending an `outreach_step` also advances that
   prospect's enrollment and schedules its next step; sending a
   `winback_letter` marks that cohort row `sent`.

5. **A failed send.**
   ```bash
   python3 tools/review.py retry <id>
   ```
   Re-queues it for another attempt once you have fixed the cause (usually a
   mailbox or messaging credential - `make doctor` will say which).

## Rules

- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
- A single-room enquiry (`kind: single_room`) always needs a human - it is
  out of scope for this desk, and the draft says so honestly.
- A discount ask above the floor is always flagged - read why before
  approving; the number is already clamped, but the sign-off is still yours.
- Confirm with the hotel before sending anything, even an approved item, the
  first few times. `workflows/90-go-live.md` covers when to stop doing that.
