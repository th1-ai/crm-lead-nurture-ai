#!/usr/bin/env python3
"""tools/outreach.py - Loop B: avatars, signals, enrichment, campaigns (outbound).

    python3 tools/outreach.py sources                           # list signal sources
    python3 tools/outreach.py approve-source <id>
    python3 tools/outreach.py scan <avatar>                     # reveal prospects matching a signal
    python3 tools/outreach.py enrich <avatar>                   # find verified emails
    python3 tools/outreach.py generate-campaign <avatar> --name "..." [--kind mice|wedding]
    python3 tools/outreach.py launch <campaign-id>
    python3 tools/outreach.py tick                              # send due steps (respects caps)
    python3 tools/outreach.py log-accept <enrollment-id>        # prospect accepted the connection
    python3 tools/outreach.py log-reply <enrollment-id> --message "..."   # stops the sequence
    python3 tools/outreach.py draft-reply <enrollment-id> --message "..."
    python3 tools/outreach.py book-meeting <enrollment-id>
    python3 tools/outreach.py accept-meeting <meeting-id> --slot "Tue 11:00-11:20"

No LLM anywhere in this file, mirroring the source engine's own contract
(docs/how-it-works.md design decision 7): avatar targeting, signal scanning,
enrichment, message rendering and the sequence are all plain functions over
seed data and config, so the same inputs always produce the same outputs.

Campaign sends are gated exactly like every other draft in this family - see
docs/how-it-works.md design decision 10: a launched campaign queues at most
`outreach.daily_caps` drafts a day into the ONE review queue
(`core.store`'s `items`, `kind='outreach_step'`); `tools/review.py send`
is what actually calls the adapter. Nothing here auto-sends, in shadow or in
live mode - `review.require_approval_for` is a hotel-wide setting and this
repo does not carve out a bypass for outreach.
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.llm import LLMError  # noqa: E402
from core.log import get_logger  # noqa: E402
from core.store import Store, StoreError, utcnow  # noqa: E402

from enrichment import EnrichmentError  # noqa: E402
import store_ext as sx  # noqa: E402

log = get_logger("outreach")

#: the 6-step ladder - docs/how-it-works.md design decisions 7-8.
SEQUENCE = [
    {"idx": 0, "key": "connect", "channel": "linkedin", "condition": "always"},
    {"idx": 1, "key": "message", "channel": "linkedin", "condition": "if_accepted"},
    {"idx": 2, "key": "email_1", "channel": "email", "condition": "if_no_reply"},
    {"idx": 3, "key": "email_2", "channel": "email", "condition": "if_no_reply"},
    {"idx": 4, "key": "channel_2", "channel": "whatsapp", "condition": "if_no_reply"},
    {"idx": 5, "key": "breakup", "channel": "email", "condition": "if_no_reply"},
]
DONE_STATUSES = {"replied", "withdrawn", "stopped", "skipped_dnc", "booked", "accepted_final"}

#: hookFor() - the per-lead opener, ported verbatim from the source engine.
_HOOK_RULES = [
    ("office", "Saw that {signal} - congrats, a new office usually means a team worth celebrating."),
    ("hiring", "Noticed {signal} - teams that grow that fast usually need a day out of the building to stay one team."),
    ("raised", "Congratulations - {signal}. The next milestone deserves a better view."),
    ("funding", "Congratulations - {signal}. The next milestone deserves a better view."),
    ("engaged", "Congratulations on the engagement - we would love to help you celebrate."),
]


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text or "")
                  if unicodedata.category(c) != "Mn")


def hook_for(prospect: dict, *, ai_personalization: bool) -> tuple[str, str]:
    """Returns (hook, signal_text). Off -> the generic fallback - the provable toggle."""
    if not ai_personalization:
        return "I'll keep this short.", "your events calendar"
    signal = prospect.get("signal") or ""
    for keyword, template in _HOOK_RULES:
        if keyword in signal.lower():
            return template.format(signal=signal), signal
    if signal:
        return f"Saw that {signal} - that's what made me reach out.", signal
    return f"Your work at {prospect.get('org', 'your company')} keeps coming up in our circle.", \
        "your work"


def channel_for_avatar(avatar_kind: str) -> str:
    return "instagram_dm" if avatar_kind == "wedding" else "whatsapp"


# --------------------------------------------------------------------------
# sending caps + warm-up ramp (docs/how-it-works.md #7)
# --------------------------------------------------------------------------
def _today() -> str:
    return date.today().isoformat()


def channel_sent_today(store: Store, channel: str) -> int:
    return int(store.get_cursor(f"outreach_sent:{channel}:{_today()}", 0) or 0)


def record_send(store: Store, channel: str) -> None:
    key = f"outreach_sent:{channel}:{_today()}"
    store.set_cursor(key, channel_sent_today(store, channel) + 1)
    if not store.get(f"outreach_first_send:{channel}"):
        store.set(f"outreach_first_send:{channel}", _today())


def channel_cap(store: Store, settings: Settings, channel: str) -> int:
    caps = settings.agent_get("outreach.daily_caps", {}) or {}
    full_cap = int(caps.get(channel, 8))
    if not settings.agent_get("outreach.warmup_ramp", True):
        return full_cap
    first = store.get(f"outreach_first_send:{channel}")
    if not first:
        return int(settings.agent_get("outreach.warmup_ramp_week1", 3))
    age_days = (date.today() - date.fromisoformat(first)).days
    weeks = int(settings.agent_get("outreach.warmup_weeks", 3))
    if age_days < 7 * weeks:
        return int(settings.agent_get("outreach.warmup_ramp_week1", 3))
    return full_cap


def under_cap(store: Store, settings: Settings, channel: str) -> bool:
    if not settings.agent_get("outreach.safe_caps", True):
        return True
    return channel_sent_today(store, channel) < channel_cap(store, settings, channel)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


# --------------------------------------------------------------------------
# sources + scan + enrich
# --------------------------------------------------------------------------
def cmd_sources(store: Store, args) -> int:
    rows = sx.list_sources(store)
    if not rows:
        print("No sources yet - fixtures/outreach/sources.json seeds them on first scan.")
        return 0
    for r in rows:
        print(f"  {r['id']}  {r['status']:<10} {r['name']}")
    return 0


def cmd_approve_source(store: Store, args) -> int:
    sx.set_source_status(store, args.id, "approved")
    print(f"approved source {args.id}")
    return 0


def _load_fixture_prospects() -> list[dict]:
    import json
    path = REPO_ROOT / "fixtures" / "outreach" / "prospects.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _load_fixture_sources() -> list[dict]:
    import json
    path = REPO_ROOT / "fixtures" / "outreach" / "sources.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def ensure_seed_sources(store: Store) -> dict[str, str]:
    """Seed fixtures/outreach/sources.json once. Returns {name: id}."""
    ids = {r["name"]: r["id"] for r in sx.list_sources(store)}
    for s in _load_fixture_sources():
        if s["name"] not in ids:
            ids[s["name"]] = sx.upsert_source(store, s["name"], status=s.get("status", "pending"),
                                              seeded=True)
    return ids


def cmd_scan(store: Store, settings: Settings, args) -> int:
    source_ids = ensure_seed_sources(store)
    vetting = bool(settings.agent_get("outreach.source_vetting", True))
    revealed, blocked_sources = 0, set()
    for p in _load_fixture_prospects():
        if p.get("avatar") != args.avatar:
            continue
        sid = source_ids.get(p.get("source_name", ""))
        if not sx.source_usable(store, sid, vetting=vetting):
            blocked_sources.add(p.get("source_name", "unknown source"))
            continue
        sx.upsert_prospect(store, avatar=p["avatar"], org=p["org"], first_name=p["first_name"],
                          last_name=p["last_name"], role=p.get("role", ""),
                          domain=p.get("domain", ""), city=p.get("city", ""),
                          signal=p.get("signal", ""), source_id=sid or "",
                          do_not_contact=bool(p.get("do_not_contact", False)), seeded=True)
        revealed += 1
    print(f"{revealed} prospect(s) revealed for avatar '{args.avatar}'.")
    if blocked_sources:
        print(f"  held out (not vetted yet): {', '.join(sorted(blocked_sources))}")
    return 0


def cmd_enrich(store: Store, settings: Settings, args) -> int:
    import enrichment
    prospects = [p for p in sx.list_prospects(store, avatar=args.avatar)
                if p["email_status"] == "missing" and not p["do_not_contact"]]
    suppressed = len(sx.list_prospects(store, avatar=args.avatar)) - len(prospects)
    found, total_cost = 0, 0.0
    for p in prospects:
        email, status, provider, cost = enrichment.find_email(p, settings)
        sx.set_prospect_email(store, p["id"], email=email, status=status, provider=provider,
                              cost=cost)
        if status == "found":
            found += 1
            total_cost += cost
    print(f"{found}/{len(prospects)} email(s) found for '{args.avatar}' - EUR {total_cost:.2f}.")
    if suppressed:
        print(f"  {suppressed} do-not-contact prospect(s) skipped (not enriched).")
    return 0


# --------------------------------------------------------------------------
# campaigns
# --------------------------------------------------------------------------
def cmd_generate_campaign(store: Store, args) -> int:
    campaign_id = sx.create_campaign(store, name=args.name, avatar=args.avatar,
                                     kind=args.kind or "mice", steps=SEQUENCE)
    print(f"campaign {campaign_id} drafted with {len(SEQUENCE)} steps - not launched yet.")
    print(f"  python3 tools/outreach.py launch {campaign_id}")
    return 0


def _deliverability_ok(settings: Settings) -> list[str]:
    problems = []
    d = settings.agent_get("outreach.deliverability", {}) or {}
    for flag, label in (("spf", "SPF"), ("dkim", "DKIM"), ("dmarc", "DMARC")):
        if not d.get(flag, True):
            problems.append(label)
    return problems


def cmd_launch(store: Store, settings: Settings, args) -> int:
    campaign = sx.get_campaign(store, args.campaign_id)
    if campaign is None:
        print(f"error: no campaign {args.campaign_id}", file=sys.stderr)
        return 1
    prospects = [p for p in sx.list_prospects(store, avatar=campaign["avatar"])
                if p["email"] and not p["do_not_contact"]]
    problems = _deliverability_ok(settings)
    if problems:
        print(f"blocked: deliverability is not green ({', '.join(problems)} failing) - fix your "
             f"DNS records or swap the sending inbox before launching.")
        return 1
    if not prospects:
        print("blocked: no prospect with a verified email and no do-not-contact flag - run "
             "`enrich` first.")
        return 1
    enrolled = 0
    for p in prospects:
        eid, created = sx.enroll(store, args.campaign_id, p["id"])
        if created:
            store.upsert_task("outreach_step", f"{eid}:0", next_action_due=utcnow())
            enrolled += 1
    sx.launch_campaign(store, args.campaign_id)
    print(f"launched: {enrolled} prospect(s) enrolled. Run `tools/outreach.py tick` to work "
         f"the due steps.")
    return 0


# --------------------------------------------------------------------------
# message rendering (deterministic - docs/how-it-works.md #7)
# --------------------------------------------------------------------------
def render_step(step: dict, prospect: dict, *, hook: str, hotel_name: str) -> tuple[str, str]:
    """Returns (subject, body). ``connect``'s body includes the visit+like nudge -
    see docs/how-it-works.md design decision 8: no universal API visits or likes
    a LinkedIn profile, so that step becomes one manual-checklist item."""
    first, org = prospect["first_name"], prospect["org"]
    if step["key"] == "connect":
        note = f"{hook} Worth a quick hello - {org} keeps coming up on my radar."[:300]
        body = (f"Manual step first - no universal API can do this part:\n"
               f"1. Visit {first} {prospect['last_name']}'s LinkedIn profile.\n"
               f"2. Like their most recent company post.\n"
               f"3. Send this connection note ({len(note)}/300 chars):\n\n{note}")
        return f"LinkedIn connect - {org}", body
    if step["key"] == "message":
        return (f"LinkedIn message - {org}",
               f"Hi {first}, {hook} Would a short call make sense sometime soon?")
    if step["key"] == "email_1":
        return (f"A quick idea for {org}",
               f"Hi {first},\n\n{hook}\n\nWe host groups and events built around exactly this "
               f"- happy to send a short brief if it is useful, and if not, tell me and I will "
               f"close the file.\n\nWarm regards,\n{hotel_name} Sales & Events")
    if step["key"] == "email_2":
        return ("One example, then I will leave it",
               f"Hi {first},\n\nA short case study from a group similar to yours, in case it "
               f"is useful. What we do not have is a reason to write more than once a quarter, "
               f"which is roughly how often I intend to.\n\nWarm regards,\n{hotel_name} Sales "
               f"& Events")
    if step["key"] == "channel_2":
        return "", f"Hi {first} - following up on my note, happy to send dates if useful."
    if step["key"] == "breakup":
        return (f"Closing the file - {org}",
               f"Hi {first},\n\nI'll stop here - inboxes are sacred. If a plan ever comes "
               f"together, you know where to find us.\n\nWarm regards,\n{hotel_name} Sales & "
               f"Events")
    return "", ""


def _step_send_channel(step: dict, campaign: dict) -> str:
    if step["key"] == "connect":
        return "staff"
    if step["channel"] == "whatsapp":
        return channel_for_avatar(campaign.get("kind", "mice"))
    return step["channel"]


def _connect_sent_at(enrollment: dict) -> str | None:
    for h in enrollment.get("history", []):
        if h.get("step_idx") == 0 and h.get("status") == "sent":
            return h.get("ts")
    return None


# --------------------------------------------------------------------------
# tick - work every due step, respecting every guardrail
# --------------------------------------------------------------------------
def cmd_tick(store: Store, settings: Settings, args) -> int:
    stats = {"queued_for_review": 0, "waiting": 0, "capped": 0, "withdrawn": 0, "skipped_dnc": 0}
    today = date.today()
    if settings.agent_get("outreach.weekend_pause", True) and is_weekend(today):
        print("OUTREACH TICK - weekend pause, nothing runs today.")
        return 0

    withdraw_days = int(settings.agent_get("outreach.withdraw_stale_invite_days", 7))
    suppress_dnc = bool(settings.agent_get("outreach.suppress_dnc", True))
    ai_personalization = bool(settings.agent_get("outreach.ai_personalization", True))

    for task in store.due_tasks(kind="outreach_step", limit=int(args.limit)):
        eid, _, idx_str = task.ref_id.rpartition(":")
        enrollment = sx.get_enrollment(store, eid)
        if enrollment is None or enrollment["status"] in DONE_STATUSES:
            store.close_task(task.id, status="done")
            continue
        prospect = sx.get_prospect(store, enrollment["prospect_id"])
        campaign = sx.get_campaign(store, enrollment["campaign_id"])
        if prospect is None or campaign is None:
            store.close_task(task.id, status="done")
            continue

        if prospect["do_not_contact"] and suppress_dnc:
            sx.advance_enrollment(store, eid, step_idx=enrollment["step_idx"],
                                  status="skipped_dnc", note="do-not-contact")
            store.close_task(task.id, status="skipped_dnc")
            stats["skipped_dnc"] += 1
            continue

        step_idx = int(idx_str)
        if step_idx != enrollment["step_idx"] or step_idx >= len(SEQUENCE):
            store.close_task(task.id, status="done")  # stale task from an earlier step
            continue
        step = SEQUENCE[step_idx]

        if step["condition"] == "if_accepted" and enrollment["status"] != "accepted":
            connect_at = _connect_sent_at(enrollment)
            age_days = ((datetime.now().astimezone()
                        - datetime.fromisoformat(connect_at)).days if connect_at else 0)
            if connect_at and age_days >= withdraw_days:
                sx.advance_enrollment(store, eid, step_idx=step_idx, status="withdrawn",
                                      note=f"invite not accepted after {age_days}d")
                store.record_event(None, "agent", "outreach_withdraw_stale_invite",
                                   {"prospect": prospect["org"], "days": age_days})
                store.close_task(task.id, status="withdrawn")
                stats["withdrawn"] += 1
                log.info("withdrawing stale invite", org=prospect["org"], days=age_days)
                continue
            store.advance_task(task.id, gap_days=1, note="waiting on LinkedIn acceptance")
            stats["waiting"] += 1
            continue

        channel = _step_send_channel(step, campaign)
        if channel != "staff" and not under_cap(store, settings, channel):
            store.advance_task(task.id, gap_days=1, note=f"queued: {channel} over daily cap")
            stats["capped"] += 1
            continue

        hook, _signal = hook_for(prospect, ai_personalization=ai_personalization)
        subject, body = render_step(step, prospect, hook=hook, hotel_name=settings.hotel.name)
        to = prospect["email"] if channel == "email" else \
            (prospect.get("email") or prospect["org"])
        item = store.upsert_item(
            "outreach", task.ref_id, kind="outreach_step",
            payload={"enrollment_id": eid, "prospect_id": prospect["id"],
                    "campaign_id": enrollment["campaign_id"], "step_idx": step_idx,
                    "channel": channel, "to": to, "org": prospect["org"]})
        if item.review_status == "new":
            store.set_fields(item.id, draft={"subject": subject, "body": body})
            store.transition(item.id, "pending_review", actor="agent")
        store.close_task(task.id, status="queued_for_review")
        stats["queued_for_review"] += 1

    print(f"OUTREACH TICK - {stats['queued_for_review']} queued for review, "
         f"{stats['capped']} queued to tomorrow (cap), {stats['waiting']} waiting on "
         f"acceptance, {stats['withdrawn']} stale invite(s) withdrawn, "
         f"{stats['skipped_dnc']} do-not-contact skipped.")
    return 0


# --------------------------------------------------------------------------
# post-send hook, called by tools/review.py after a successful send
# --------------------------------------------------------------------------
def advance_after_send(store: Store, settings: Settings, payload: dict) -> None:
    eid, step_idx, channel = payload["enrollment_id"], payload["step_idx"], payload["channel"]
    enrollment = sx.get_enrollment(store, eid)
    if enrollment is None:
        return
    if channel != "staff":
        record_send(store, channel)
    status = "active" if enrollment["status"] == "queued" else enrollment["status"]
    sx.advance_enrollment(store, eid, step_idx=step_idx, status=status, note="sent")
    next_idx = step_idx + 1
    if next_idx >= len(SEQUENCE):
        sx.advance_enrollment(store, eid, step_idx=next_idx, status="stopped", note="sequence complete")
        return
    delays = settings.agent_get("outreach.step_delays_days", {}) or {}
    delay = int(delays.get(SEQUENCE[next_idx]["key"], 3))
    due = (datetime.now().astimezone() + timedelta(days=delay)).isoformat(timespec="seconds")
    store.upsert_task("outreach_step", f"{eid}:{next_idx}", next_action_due=due)


def cmd_log_accept(store: Store, args) -> int:
    enrollment = sx.get_enrollment(store, args.id)
    if enrollment is None:
        print(f"error: no enrollment {args.id}", file=sys.stderr)
        return 1
    sx.advance_enrollment(store, args.id, step_idx=enrollment["step_idx"], status="accepted",
                          note="LinkedIn invite accepted")
    print(f"{args.id}: marked accepted - the LinkedIn message step can now go out on the "
         f"next tick.")
    return 0


def cmd_log_reply(store: Store, settings: Settings, args) -> int:
    enrollment = sx.get_enrollment(store, args.id)
    if enrollment is None:
        print(f"error: no enrollment {args.id}", file=sys.stderr)
        return 1
    stop = bool(settings.agent_get("outreach.stop_on_reply", True))
    status = "replied" if stop else enrollment["status"]
    sx.advance_enrollment(store, args.id, step_idx=enrollment["step_idx"], status=status,
                          note=f"reply logged: {(args.message or '')[:200]}")
    if stop:
        store.close_task(f"{args.id}:{enrollment['step_idx']}", status="replied")
        print(f"{args.id}: sequence stopped - a reply arrived. Draft a reply with "
             f"`python3 tools/outreach.py draft-reply {args.id} --message \"...\"`.")
    else:
        print(f"{args.id}: reply logged, but outreach.stop_on_reply is off - the sequence "
             f"keeps sending (this is the demo's explicit 'never do this' case).")
    return 0


# --------------------------------------------------------------------------
# inbox - deterministic reply drafting (docs/how-it-works.md #7)
# --------------------------------------------------------------------------
def draft_inbox_reply(message: str, prospect: dict, hotel_name: str) -> tuple[str, str, bool, bool]:
    """Returns (subject, body, propose_meeting, deferred). Branches on the last
    message's content - ported from the source's draftInboxReply, deterministic."""
    text = (message or "").lower()
    org, first = prospect["org"], prospect["first_name"]
    if "not this year" in text or "2027" in text or "next year" in text:
        body = (f"Completely understood - file closed for this year. I'll make one note to "
               f"come back to {org} next year, and nothing else lands in your inbox before "
               f"then. Thanks for the straight answer.")
        return f"Understood - {org}", body, False, True
    if any(k in text for k in ("rate card", "rfp", "brochure")):
        body = (f"Hi {first}, sending our rate card and event brief over now, and I can hold "
               f"provisional dates while you compare. Would a short call help too?")
        return f"Rate card - {org}", body, True, False
    if any(k in text for k in ("call", "meet", "coffee")):
        body = f"Hi {first}, a call works well for me - here is a link to grab a slot directly."
        return f"Let's talk - {org}", body, True, False
    body = f"Hi {first}, thanks for coming back to me - here is a link if a quick call helps."
    return f"Good to hear from you - {org}", body, True, False


def cmd_draft_reply(store: Store, settings: Settings, args) -> int:
    enrollment = sx.get_enrollment(store, args.id)
    if enrollment is None:
        print(f"error: no enrollment {args.id}", file=sys.stderr)
        return 1
    prospect = sx.get_prospect(store, enrollment["prospect_id"])
    subject, body, propose_meeting, deferred = draft_inbox_reply(
        args.message, prospect, settings.hotel.name)
    item = store.upsert_item(
        "outreach", f"{args.id}:reply", kind="outreach_reply",
        payload={"enrollment_id": args.id, "prospect_id": prospect["id"], "channel": "email",
                "to": prospect.get("email", ""), "propose_meeting": propose_meeting})
    if item.review_status == "new":
        store.set_fields(item.id, draft={"subject": subject, "body": body})
        store.transition(item.id, "pending_review", actor="agent")
    if deferred:
        sx.advance_enrollment(store, args.id, step_idx=enrollment["step_idx"], status="stopped",
                              note="guest deferred")
    print(f"drafted reply {item.id} for {prospect['org']}"
         + (" (booking link offered)" if propose_meeting else ""))
    return 0


def cmd_book_meeting(store: Store, settings: Settings, args) -> int:
    from core.adapters import get_stub
    from core.adapters.base import AdapterNotImplemented
    from core.review import WriteBlocked as _WriteBlocked
    enrollment = sx.get_enrollment(store, args.id)
    if enrollment is None:
        print(f"error: no enrollment {args.id}", file=sys.stderr)
        return 1
    prospect = sx.get_prospect(store, enrollment["prospect_id"])
    slug = (f"meet.{settings.hotel.name}/sales/{prospect['first_name']}-{prospect['last_name']}"
           .lower().replace(" ", "-"))
    domain = prospect.get("domain") or ""
    provider = "outlook" if len(domain) % 2 == 0 else "google"  # placeholder - see docs/integrations.md
    try:
        get_stub("calendar", settings).create_event(
            {"title": f"Intro call - {prospect['org']}", "attendee": prospect.get("email", "")})
    except (AdapterNotImplemented, _WriteBlocked):
        pass  # expected: calendar is a stub (or shadow mode) until you connect one -
              # see docs/integrations.md. The tracked link below works either way.
    meeting_id = sx.record_meeting(store, prospect["id"], slug=slug, provider=provider)
    print(f"meeting {meeting_id} created: {slug} (provider guess: {provider} - connect a real "
         f"Calendar adapter to replace this heuristic, see docs/integrations.md)")
    return 0


def cmd_accept_meeting(store: Store, settings: Settings, args) -> int:
    meeting = sx.get_meeting(store, args.id)
    if meeting is None:
        print(f"error: no meeting {args.id}", file=sys.stderr)
        return 1
    slot = args.slot or "Tue 11:00-11:20"
    sx.set_meeting_status(store, args.id, "accepted", slot_at=slot)
    prospect = sx.get_prospect(store, meeting["prospect_id"])
    handoff_value = float(settings.agent_get("outreach.handoff_est_value", 12500))
    item = store.upsert_item(
        "outreach", f"meeting:{args.id}", kind="lead_reply",
        payload={"from": prospect.get("email", ""),
                "from_name": f"{prospect['first_name']} {prospect['last_name']}",
                "subject": f"Intro call - {prospect['org']}",
                "body": f"Booked via outreach - {slot}.", "stage": "inquiry", "kind": "group",
                "headcount": 0, "est_value": handoff_value,
                "next_step": f"Intro call {slot} - booked by outreach"})
    if item.review_status == "new":
        store.set_fields(item.id, intent="group", confidence=1.0)
        store.transition(item.id, "needs_human", actor="agent",
                         detail={"reason": "outreach handoff"})
    print(f"meeting accepted ({slot}) - handed off to the pipeline as lead {item.id}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="list signal sources")
    p_appr = sub.add_parser("approve-source", help="vet a pending signal source")
    p_appr.add_argument("id")

    p_scan = sub.add_parser("scan", help="reveal prospects matching a vetted signal")
    p_scan.add_argument("avatar")

    p_enrich = sub.add_parser("enrich", help="find verified emails for an avatar")
    p_enrich.add_argument("avatar")

    p_gen = sub.add_parser("generate-campaign", help="draft the 6-step sequence for an avatar")
    p_gen.add_argument("avatar")
    p_gen.add_argument("--name", required=True)
    p_gen.add_argument("--kind", default="mice")

    p_launch = sub.add_parser("launch", help="enroll every eligible prospect, pre-flight gated")
    p_launch.add_argument("campaign_id")

    p_tick = sub.add_parser("tick", help="work every due step, respecting every guardrail")
    p_tick.add_argument("--limit", type=int, default=50)

    p_acc = sub.add_parser("log-accept", help="the prospect accepted the LinkedIn invite")
    p_acc.add_argument("id")

    p_reply = sub.add_parser("log-reply", help="a reply arrived - stops the sequence")
    p_reply.add_argument("id")
    p_reply.add_argument("--message", default="")

    p_draft = sub.add_parser("draft-reply", help="draft a reply to an inbound reply")
    p_draft.add_argument("id")
    p_draft.add_argument("--message", default="")

    p_meet = sub.add_parser("book-meeting", help="create a tracked meeting link")
    p_meet.add_argument("id")

    p_accmeet = sub.add_parser("accept-meeting", help="the slot was accepted - hands off to the pipeline")
    p_accmeet.add_argument("id")
    p_accmeet.add_argument("--slot", default="")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    sx.ensure_schema(store)
    try:
        if args.command == "sources":
            return cmd_sources(store, args)
        if args.command == "approve-source":
            return cmd_approve_source(store, args)
        if args.command == "scan":
            return cmd_scan(store, settings, args)
        if args.command == "enrich":
            return cmd_enrich(store, settings, args)
        if args.command == "generate-campaign":
            return cmd_generate_campaign(store, args)
        if args.command == "launch":
            return cmd_launch(store, settings, args)
        if args.command == "tick":
            return cmd_tick(store, settings, args)
        if args.command == "log-accept":
            return cmd_log_accept(store, args)
        if args.command == "log-reply":
            return cmd_log_reply(store, settings, args)
        if args.command == "draft-reply":
            return cmd_draft_reply(store, settings, args)
        if args.command == "book-meeting":
            return cmd_book_meeting(store, settings, args)
        if args.command == "accept-meeting":
            return cmd_accept_meeting(store, settings, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except (AdapterError, LLMError, StoreError, EnrichmentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
