# Workflow: shadow to live

Objective: decide, together with the hotel, whether CRM / Lead Nurture AI is
ready to send approved drafts on its own instead of only queuing them - and
make the change safely if so.

This is the hotel's decision, never the agent's. Do not suggest it until the
checklist below is genuinely true, and when you do raise it, say plainly
what changes.

## Checklist

- [ ] `make doctor` is clean (no `FAIL` lines). `warn` on `mode` is expected
      until you flip it.
- [ ] `config/hotel.yaml` has the real property name, address, contact
      details and languages; `knowledge/property.md`, `knowledge/faq.md` and
      `knowledge/signature.md` exist and are accurate (not the shipped
      examples).
- [ ] `config/agent.yaml`'s `pipeline:` block has real numbers -
      `event_room_type`, `day_delegate_fee`, `dinner_fee`,
      `discount_floor_pct` - not the shipped defaults, unless they happen to
      already be right.
- [ ] At least a few days of real `make run` passes have gone through the
      review queue, not just the demo fixtures - for the pipeline and, if
      you use it, outreach.
- [ ] The edit rate (`make report`) is one you would be comfortable with a
      real lead seeing unedited. `workflows/85-coach-weekly.md` is the loop
      that brings it down.
- [ ] A real mailbox is connected (`systems.email.adapter: imap` or `gmail`)
      and, if you use outreach, a real messaging channel too - `make doctor`
      shows both healthy.
- [ ] **Clear the shadow-era backlog.**
      ```bash
      python3 tools/review.py stale
      ```
      Everything approved or still waiting from before go-live is marked
      `stale` - it was queued under shadow rules and may be out of date. A
      human can revive one that still matters (`stale -> pending_review` via
      the review tool); nothing old goes out by surprise the moment you flip
      the switch.

## Making the change

1. Edit `config/hotel.yaml`:
   ```yaml
   mode: live
   ```
2. `review.require_approval_for` still lists `send_email` and `send_message`
   by default - it should. Going live means **approved drafts get sent**,
   not that the agent starts sending unapproved ones. There is no config
   that changes that for a single-room enquiry, a flagged discount, or an
   unsupported language - those still always need a human.
3. Run `make doctor` again to confirm.
4. Run one real pass and manually watch a send go through:
   ```bash
   make run ARGS="--limit 1"
   python3 tools/review.py list
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
5. Tell the hotel exactly what just changed: an approved draft now actually
   leaves the mailbox (or messaging channel) the next time someone (or a
   scheduled job) runs `python3 tools/review.py send` - it is still never
   automatic before that approval, and everything except an approved item
   still waits for a person.

## Going back to shadow

```yaml
mode: shadow
```
in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env` for one run. Either
stops every outbound action on the next pass, mid-schedule, with no other
change required.
