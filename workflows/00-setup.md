# Workflow: first-run setup

Objective: get CRM / Lead Nurture AI from a fresh clone to a working demo,
then to real config, in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet - it never overwrites
   your own copies). `make doctor` will show a `FAIL` on "hotel identity"
   right after setup - expected, it means the property name is still the
   shipped placeholder "Hotel Aurora". Everything else should be `ok` or
   `warn`.

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect to see 4 sample enquiries priced and drafted, 2 outreach steps
   queued, 5 win-back letters drafted, and the line `DEMO OK — 4 items
   processed, 4 drafted, 0 sent (shadow), 2 outreach step(s), 5 win-back
   letter(s)`. If you do not see that, stop and read
   `workflows/99-troubleshooting.md` before going further.

3. **Fill in the property.** Edit `config/hotel.yaml` (name, address, contact,
   languages - only enquiries in a language you list get a reply in that
   language; anything else falls back to your default and waits for a
   person, see `docs/how-it-works.md`). Then:
   ```bash
   cp knowledge/property.example.md   knowledge/property.md
   cp knowledge/faq.example.md        knowledge/faq.md
   cp knowledge/signature.example.md  knowledge/signature.md
   cp knowledge/disclosure.example.md knowledge/disclosure.md
   ```
   Replace the Hotel Aurora content with the real property's facts - group
   capacity, meeting space, wedding season, discounting policy. Edit
   `knowledge/signature.md` to your own sign-off; it is appended to every
   outbound email automatically (`core/adapters/base.py:Email.with_signature`)
   and is where the AI-disclosure line lives - see `docs/safety.md`.
   `disclosure.md` is the matching one-sentence line for the LinkedIn/
   WhatsApp/Instagram outreach steps (`Messaging.with_disclosure()`), also
   the EU AI Act Article 50 line.

4. **Set the pipeline's own numbers.** Copy `config/agent.example.yaml` to
   `config/agent.yaml` (done by `make setup`) and edit the `pipeline:` block:
   `event_room_type` (must match a room type id your PMS adapter returns),
   `day_delegate_fee`, `dinner_fee`, `discount_floor_pct`,
   `large_group_headcount`. See `docs/how-it-works.md` for the pricing
   formula these feed.

5. **Pick how the agent thinks.** `config/hotel.yaml`'s `llm.provider` starts
   as `interactive` - it asks you, in this Claude Code session, instead of
   calling a model. That costs nothing extra and is the best way to see how
   the pipeline reasons. `docs/how-it-works.md` and `docs/safety.md` explain
   the other three providers (`mock`, `claude-code`, `anthropic`).

6. **Connect a real mailbox and PMS (optional for now).**
   `systems.email.adapter` and `systems.pms.adapter` in `config/hotel.yaml`
   start as `mock`, which only ever sees the fixtures. `docs/integrations.md`
   covers `csv`/`cloudbeds` for the PMS and `imap`/`gmail` for email. Run
   `make doctor` after changing either.

7. **Decide on the outbound side and the sub-agent.** Loop B (outreach) needs
   at least one avatar and a source before it does anything - see
   `workflows/20-outreach.md`. Win-Back / Loyalty AI stays off
   (`subagents.win_back.enabled: false`) until you turn it on - see
   `docs/sub-agents.md` and `workflows/25-win-back.md`.

8. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real and `knowledge/property.md` exists, the
   "hotel identity" and "knowledge" lines turn green. Move on to
   `workflows/10-pipeline.md` to run the loop for real.
