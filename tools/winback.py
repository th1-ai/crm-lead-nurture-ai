#!/usr/bin/env python3
"""tools/winback.py - Win-Back / Loyalty AI ("The Diplomat"), off by default.

    python3 tools/winback.py cohort              # compute the lapsed cohort from PMS history
    python3 tools/winback.py draft                # draft one letter per undrafted guest
    python3 tools/winback.py list [--status new]
    python3 tools/winback.py mark-accepted <cohort-id> [--checkin YYYY-MM-DD]
    python3 tools/winback.py mark-declined <cohort-id>

Enabled with `subagents.win_back.enabled: true` in config/agent.yaml - see
docs/sub-agents.md. Every letter is deterministic, not model-written
(docs/how-it-works.md design decision 5): the discount cap is enforced by
never generating a `%` sign, not by asking a model to remember a rule.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_pms  # noqa: E402
from core.adapters.base import AdapterError, AdapterNotImplemented, PMS, Reservation  # noqa: E402
from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError, utcnow  # noqa: E402

import store_ext as sx  # noqa: E402

#: keyword -> reason category. First match wins.
_CATEGORY_KEYWORDS = [
    ("noise", ["noise", "noisy", "renovat"]),
    ("spa", ["spa", "massage", "treatment"]),
    ("unsold", ["unsold", "open again", "available again", "midsummer", "seasonal"]),
    ("price", ["rate", "price", "cheaper", "coast", "competitor"]),
]

_LETTERS = {
    "noise": {
        "subject": "We fixed it - and I owe you a stay",
        "opening": ("I know exactly why you stopped coming: {reason}. I am writing because "
                   "we have since fixed it - it cannot happen again."),
        "offer": "the first evening's dinner is on us",
    },
    "spa": {
        "subject": "Something new, built for how you actually stayed with us",
        "opening": "I am writing because {reason}, and I wanted you to be the first to know "
                  "about something new.",
        "offer": "I will include a complimentary treatment",
    },
    "unsold": {
        "subject": "Your usual dates are open again",
        "opening": "I am writing because {reason}, and I would rather it went to you than a "
                  "stranger.",
        "offer": "I will hold it for you for 14 days, free of charge",
    },
    "price": {
        "subject": "Our direct rate now beats what you may be paying elsewhere",
        "opening": "Fair enough - but {reason}, and our direct rate is worth a second look "
                  "now.",
        "offer": "I will add late checkout and breakfast",
    },
    "general": {
        "subject": "{first}, your room is open again",
        "opening": "It has been a while, and {reason}.",
        "offer": "I will make sure you are personally looked after",
    },
}

#: adverbs a PMS note can lead with before its verb ("always booked...") -
#: the verb check below looks past these, not at them.
_LEADING_ADVERBS = {"always", "often", "rarely", "still", "once", "no longer", "never"}


def categorize_reason(note: str) -> str:
    lower = (note or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(k in lower for k in keywords):
            return category
    return "general"


def _needs_subject(reason: str) -> bool:
    """True when ``reason`` reads as a subjectless verb-phrase fragment.

    PMS notes are short staff shorthand (``reservation.notes``), not full
    sentences - e.g. "moved to a competitor..." or "always booked the same
    week...". Slotted straight into a template's "because {reason}" that
    reads as "because moved to a competitor" - missing the subject. A note
    that already opens with an article/pronoun/possessive ("the noise from
    the adjacent room...", "your last stay...") is already a full noun
    phrase and needs nothing added. Deterministic on purpose - see
    docs/how-it-works.md design decision 5; no model call for a fixed string
    swap. Regular past-tense verbs end "-ed"; this is a heuristic over short
    hotel-note shorthand, not a general English parser.
    """
    words = (reason or "").strip().split()
    if not words:
        return False
    first = words[0].lower()
    if first in _LEADING_ADVERBS and len(words) > 1:
        first = words[1].lower()
    return first.endswith("ed")


def draft_letter(*, guest_name: str, reason_to_return: str, reason_category: str,
                 hotel_name: str) -> dict:
    """Deterministic letter. Never emits a '%' or the word 'discount' - the cap
    is structural, not a rule the model has to remember."""
    template = _LETTERS.get(reason_category, _LETTERS["general"])
    first = (guest_name or "there").split(" ")[0]
    reason = reason_to_return or "your last stay is still one we remember well"
    if _needs_subject(reason):
        reason = f"you {reason}"
    opening = template["opening"].format(reason=reason, first=first)
    body = (f"Dear {first},\n\n{opening}\n\nIf you would like to come back, {template['offer']} "
           f"- no code, no minimum nights, just tell me and I will make it happen.\n\nWarm "
           f"regards,\n{hotel_name} Guest Relations")
    subject = template["subject"].format(first=first)
    return {"subject": subject, "body": body, "needs_human": False}


# --------------------------------------------------------------------------
# cohort - computed from PMS history, not seeded (docs/how-it-works.md #4)
# --------------------------------------------------------------------------
def compute_cohort(settings: Settings, store: Store, pms: PMS) -> int:
    today = date.today()
    start = (today - timedelta(days=365 * 6)).isoformat()
    reservations = pms.list_reservations(start, today.isoformat())
    lapse_days = int(settings.agent_get("winback.lapse_after_days", 270))
    min_stays = int(settings.agent_get("winback.min_qualifying_stays", 2))

    by_guest: dict[str, list[Reservation]] = defaultdict(list)
    for r in reservations:
        if r.check_out >= today.isoformat():
            continue  # still upcoming or in-house - not lapsed
        key = (r.guest.email or r.guest.full_name or r.id).lower()
        by_guest[key].append(r)

    created = 0
    for res_list in by_guest.values():
        stays = len(res_list)
        if stays < min_stays:
            continue
        most_recent = max(res_list, key=lambda r: r.check_out)
        age_days = (today - date.fromisoformat(most_recent.check_out)).days
        if age_days < lapse_days:
            continue
        lifetime_spend = round(sum(r.total for r in res_list), 2)
        reason_note = most_recent.notes or ""
        cid = sx.upsert_cohort_row(
            store, guest_email=most_recent.guest.email, guest_name=most_recent.guest.full_name,
            last_stay=most_recent.check_out, stays=stays, lifetime_spend=lifetime_spend,
            reason_to_return=reason_note, reason_category=categorize_reason(reason_note),
            prior_reservation_id=most_recent.id, prior_room_type=most_recent.room_type_id)
        created += 1
    return created


def cmd_cohort(store: Store, settings: Settings, pms: PMS, args) -> int:
    n = compute_cohort(settings, store, pms)
    rows = sx.list_cohort(store)
    total_spend = sum(r["lifetime_spend"] for r in rows)
    print(f"{len(rows)} lapsed guest(s) in the cohort ({n} new this run), EUR "
         f"{total_spend:,.2f} of lifetime spend addressed.")
    for r in rows[:3]:
        print(f"  top: {r['guest_name']} ({r['stays']} stays, EUR {r['lifetime_spend']:,.2f})")
    return 0


def cmd_list(store: Store, args) -> int:
    rows = sx.list_cohort(store, status=args.status)
    if not rows:
        print("Nothing in the cohort yet - run `python3 tools/winback.py cohort` first.")
        return 0
    for r in rows:
        print(f"  {r['id']}  {r['status']:<10} {r['guest_name']:<24} "
             f"stays={r['stays']} spend=EUR{r['lifetime_spend']:,.0f} [{r['reason_category']}]")
    return 0


def cmd_draft(store: Store, settings: Settings, args) -> int:
    rows = [r for r in sx.list_cohort(store, status="new")]
    drafted = 0
    for r in rows:
        letter = draft_letter(guest_name=r["guest_name"], reason_to_return=r["reason_to_return"],
                              reason_category=r["reason_category"], hotel_name=settings.hotel.name)
        item = store.upsert_item(
            "winback", r["id"], kind="winback_letter",
            payload={"cohort_id": r["id"], "to": r["guest_email"], "guest_name": r["guest_name"]})
        if item.review_status == "new":
            store.set_fields(item.id, draft=letter, intent=r["reason_category"], confidence=1.0)
            store.transition(item.id, "pending_review", actor="agent")
        sx.set_cohort_status(store, r["id"], "drafted", item_id=item.id)
        drafted += 1
    print(f"{drafted} letter(s) drafted. Each one waits for your approval before it reaches "
         f"the guest - see workflows/25-win-back.md.")
    return 0


# --------------------------------------------------------------------------
# acceptance - the only path in this repo that books a repeat stay
# (docs/how-it-works.md design decision 6: no PMS can generically create one)
# --------------------------------------------------------------------------
def cmd_mark_accepted(store: Store, settings: Settings, pms: PMS, args) -> int:
    row = sx.get_cohort_row(store, args.id)
    if row is None:
        print(f"error: no cohort row {args.id}", file=sys.stderr)
        return 1
    nights = int(settings.agent_get("winback.default_nights", 2))
    offset = int(settings.agent_get("winback.default_checkin_offset_days", 45))
    checkin = args.checkin or (date.today() + timedelta(days=offset)).isoformat()
    checkout = (date.fromisoformat(checkin) + timedelta(days=nights)).isoformat()
    room_type_id = row["prior_room_type"] or str(settings.agent_get("pipeline.event_room_type",
                                                                    "classic"))
    rates = pms.get_rates(checkin, checkout, room_type_id)
    nightly = rates[0].price if rates else 0.0
    total = round(nightly * nights, 2)
    room_type_name = next((rt.name for rt in pms.list_room_types() if rt.id == room_type_id),
                          room_type_id)
    note = f"Won back by AI outreach - cited {(row['reason_to_return'] or 'their history')[:140]}"

    pms_reservation_id = ""
    create = getattr(pms, "create_reservation", None)
    try:
        if callable(create):
            result = create({"guest_name": row["guest_name"], "room_type": room_type_id,
                             "check_in": checkin, "check_out": checkout, "total": total,
                             "channel": "Win-back", "notes": note})
            pms_reservation_id = str(result.get("id", "")) if isinstance(result, dict) else ""
        elif row["prior_reservation_id"]:
            pms.add_note(row["prior_reservation_id"], note)
            pms_reservation_id = row["prior_reservation_id"]
    except WriteBlocked as exc:
        print(f"note: PMS write blocked ({exc}) - recording the win here; enter the stay in "
             f"your PMS by hand, or approve the write once you trust it.")
    except AdapterNotImplemented:
        pass

    booking_id = sx.record_winback_booking(
        store, cohort_id=args.id, guest_name=row["guest_name"], checkin=checkin,
        checkout=checkout, room_type=room_type_name, total_eur=total,
        pms_reservation_id=pms_reservation_id)
    sx.set_cohort_status(store, args.id, "rebooked")
    print(f"booking {booking_id} recorded: {row['guest_name']}, {checkin} to {checkout}, "
         f"{room_type_name}, EUR {total:,.2f}."
         + (f" PMS reservation: {pms_reservation_id}." if pms_reservation_id else
            " Enter this stay in your PMS - see docs/how-it-works.md design decision 6."))
    return 0


def cmd_mark_declined(store: Store, args) -> int:
    sx.set_cohort_status(store, args.id, "declined")
    print(f"{args.id}: marked declined.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("cohort", help="compute the lapsed cohort from PMS history")
    sub.add_parser("draft", help="draft one letter per undrafted guest")
    p_list = sub.add_parser("list", help="show the cohort")
    p_list.add_argument("--status", default=None)
    p_acc = sub.add_parser("mark-accepted", help="the guest said yes - record the rebooking")
    p_acc.add_argument("id")
    p_acc.add_argument("--checkin", default="")
    p_dec = sub.add_parser("mark-declined", help="the guest said no")
    p_dec.add_argument("id")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    sx.ensure_schema(store)
    pms = get_pms(settings)
    try:
        if args.command == "cohort":
            return cmd_cohort(store, settings, pms, args)
        if args.command == "draft":
            return cmd_draft(store, settings, args)
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "mark-accepted":
            return cmd_mark_accepted(store, settings, pms, args)
        if args.command == "mark-declined":
            return cmd_mark_declined(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except (AdapterError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
