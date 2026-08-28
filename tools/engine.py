"""tools/engine.py - CRM / Lead Nurture AI's pipeline logic (Loop A).

Deterministic decisioning, LLM for language: `classify_lead` and
`draft_lead_reply` are the only two model calls (`core.llm.complete`, always
with a JSON schema). Availability, pricing, the discount floor and the
needs-human gate are plain Python over the facts - see docs/how-it-works.md
design decisions 1-3.

Shared by `tools/run.py` (the real loop) and `tools/demo.py` (the
zero-credential walkthrough), so both exercise exactly the same code path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from core.adapters.base import EmailMessage, PMS, RateRow
from core.config import Settings
from core.llm import LLMResult, LLMSchemaError, complete
from core.store import Item, Store
from core.templates import build_prompt

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


CLASSIFY_SCHEMA = _schema("classify")
DRAFT_SCHEMA = _schema("draft")

OUT_OF_SCOPE_KINDS = ("single_room",)


def email_to_dict(msg: EmailMessage) -> dict:
    return {"id": msg.id, "from": msg.from_email, "from_name": msg.from_name,
           "subject": msg.subject, "body": msg.body_text, "received_at": msg.received_at}


def round_to(value: float, step: int) -> float:
    return round(value / step) * step


# --------------------------------------------------------------------------
# availability - "never quote space you have not checked"
# --------------------------------------------------------------------------
@dataclass
class Availability:
    found: bool
    checkin: str = ""
    checkout: str = ""
    nightly_rate: float = 0.0
    occupancy_pct: float = 0.0
    line: str = ""


def _occupancy_pct(row: RateRow, total_rooms: int) -> float:
    if not total_rooms:
        return 0.0
    return round(max(0.0, (1 - row.available / total_rooms)) * 100)


def nearest_midweek_block(rates: list[RateRow], *, today: date, window_days: int,
                          event_room_type: str, total_rooms: int) -> Availability:
    """First Mon-Thu two-night block with availability, inside ``window_days``.

    Mirrors the source engine's honesty rule: the live rate calendar only runs
    a few weeks out, so a request for a date outside it is quoted against the
    nearest comparable block instead of an invented number - see
    docs/how-it-works.md design decision 2.
    """
    by_date = {r.date: r for r in rates
              if not r.room_type_id or r.room_type_id == event_room_type}
    d = today
    for _ in range(window_days):
        if d.weekday() in (0, 1, 2, 3):  # Mon..Thu start
            r1 = by_date.get(d.isoformat())
            r2 = by_date.get((d + timedelta(days=1)).isoformat())
            if (r1 and r2 and not r1.closed and not r2.closed
                    and r1.available > 0 and r2.available > 0):
                checkin, checkout = d.isoformat(), (d + timedelta(days=2)).isoformat()
                occ = _occupancy_pct(r1, total_rooms)
                line = (f"Our live rate calendar publishes {window_days} days out, so I "
                       f"checked the nearest comparable midweek two-night block: "
                       f"{checkin} to {checkout}, running at {occ}% occupancy across the "
                       f"house. Name your exact dates and I will place a provisional hold "
                       f"the same day.")
                return Availability(True, checkin, checkout, r1.price, occ, line)
        d += timedelta(days=1)
    return Availability(False, line=(
        "I do not have a comparable open block in our published rate calendar right now - "
        "let me check with the team directly and come back to you within one business day."))


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------
def clamp_discount(requested_pct: float, floor_pct: float) -> tuple[float, bool]:
    """Clamp a discount ask to the floor. Returns (applied_pct, flagged_for_signoff)."""
    if requested_pct > floor_pct:
        return floor_pct, True
    return max(0.0, requested_pct), False


def price_conference(nightly_rate: float, headcount: int, nights: int, *, day_delegate_fee: float,
                     dinner_fee: float, round_step: int) -> tuple[float, float, str]:
    delegate_rate = round_to(nightly_rate + day_delegate_fee + dinner_fee, round_step)
    total = delegate_rate * max(1, headcount) * max(1, nights)
    line = (f"€{delegate_rate:,.0f} per delegate per night (room + day delegate rate + "
           f"dinner), €{total:,.0f} for {headcount} delegates over {nights} night(s), "
           f"subject to final dates.")
    return delegate_rate, total, line


def price_discounted(rack: float, applied_pct: float, *, round_step: int) -> tuple[float, str]:
    net = round_to(rack * (1 - applied_pct / 100), round_step)
    if applied_pct > 0:
        line = (f"€{net:,.0f} per night for the group - that is {applied_pct:.0f}% off our "
               f"€{rack:,.0f} rack rate, held for you.")
    else:
        line = f"€{rack:,.0f} per night, our best available rate for those dates."
    return net, line


# --------------------------------------------------------------------------
# queue ordering - "the €19,500 enquiry never waits behind a smaller one"
# --------------------------------------------------------------------------
def order_queue(rows: list[dict], *, value_priority: bool) -> list[dict]:
    """``rows`` are dicts with ``id``, ``est_value``, ``received_at``.

    ``value_priority`` on: highest est_value first, tie-break id. Off: oldest
    received_at first - see docs/how-it-works.md.
    """
    if value_priority:
        return sorted(rows, key=lambda r: (-float(r.get("est_value") or 0), r["id"]))
    return sorted(rows, key=lambda r: (r.get("received_at") or "", r["id"]))


# --------------------------------------------------------------------------
# needs-human gate - a plain rule, not a model decision (docs/safety.md)
# --------------------------------------------------------------------------
def needs_human_for(kind: str, headcount: int, confidence: float, discount_flagged: bool,
                    draft_needs_human: bool, settings: Settings,
                    language_unsupported: bool = False) -> bool:
    """A plain rule, not a model decision. Every enquiry this desk is built for
    (group, multi-room, VIP) is high-value enough that a human checks it before
    it sends - see docs/safety.md. ``confidence_threshold`` and
    ``always_needs_human_kinds`` exist for the day a hotel wants to relax that
    for its smallest, most routine group size. ``language_unsupported`` is set
    when the enquiry arrived in a language not in ``hotel.languages`` - the
    draft goes out in the hotel's default language instead, so a human should
    always check it."""
    threshold = float(settings.agent_get("pipeline.confidence_threshold", 0.65))
    large_group = int(settings.agent_get("pipeline.large_group_headcount", 6))
    always_kinds = set(settings.agent_get("pipeline.always_needs_human_kinds", []) or [])
    if language_unsupported:
        return True
    if kind in OUT_OF_SCOPE_KINDS:
        return True
    if kind in always_kinds:
        return True
    if discount_flagged:
        return True
    if headcount >= large_group:
        return True
    if bool(draft_needs_human):
        return True
    return confidence < threshold


# --------------------------------------------------------------------------
# the two LLM calls - `item` is None in --dry-run: compute, write nothing
# (docs/how-it-works.md - "dry-run never writes", build-repo.md section 5).
# --------------------------------------------------------------------------
def classify_lead(settings: Settings, store: Store, item: Item | None, msg: EmailMessage,
                  *, provider: str | None = None) -> dict:
    prompt = build_prompt("classify", settings=settings, item=email_to_dict(msg),
                          fixture_id=msg.id)
    result: LLMResult = complete(
        "classify", prompt, CLASSIFY_SCHEMA, settings=settings, provider=provider,
        store=None if item is None else store, item_id=None if item is None else item.id,
        fixture_id=msg.id)
    data = result.data or {}
    if item is not None:
        store.set_fields(item.id, intent=data.get("kind"),
                         confidence=float(data.get("confidence", 0.0)))
    return data


def draft_lead_reply(settings: Settings, store: Store, item: Item | None, *, reply_language: str,
                     kind: str, headcount: int, nights_wanted: int, availability_line: str,
                     price_line: str, discount_flagged: bool, enquiry_body: str,
                     fixture_id: str, provider: str | None = None) -> dict:
    prompt = build_prompt(
        "draft", settings=settings, fixture_id=fixture_id, reply_language=reply_language,
        kind=kind, headcount=headcount, nights_wanted=nights_wanted,
        availability_line=availability_line, price_line=price_line,
        discount_flagged=("yes" if discount_flagged else "no"), enquiry_body=enquiry_body)
    result: LLMResult = complete(
        "draft", prompt, DRAFT_SCHEMA, settings=settings, provider=provider,
        store=None if item is None else store, item_id=None if item is None else item.id,
        fixture_id=fixture_id)
    data = result.data or {}
    if item is not None:
        store.set_fields(item.id, draft=data)
    return data


# --------------------------------------------------------------------------
# orchestration - shared by tools/run.py and tools/demo.py
# --------------------------------------------------------------------------
def process_lead_email(settings: Settings, store: Store, pms: PMS, msg: EmailMessage,
                       *, provider: str | None = None) -> tuple[Item, bool]:
    """Classify, price and draft one inbound lead. Idempotent on re-run.

    ``settings.dry_run`` computes every one of these steps - including the two
    model calls, so you can preview a prompt change - but never touches the
    store: no item row, no event row, no follow-up task. Two ``--dry-run``
    passes over the same fixtures are both complete no-ops, every time.
    """
    dry = settings.dry_run
    item: Item | None = None
    classification: dict | None = None
    if not dry:
        # Keep the classification cached across passes: `upsert_item` refreshes
        # the payload from the mailbox, which would otherwise wipe it. Without
        # this, a retry after `draft` pends (interactive) or fails schema
        # validation would see `item.intent` already set and skip the item
        # forever instead of resuming at the draft stage.
        existing = store.get_by_external("email", msg.id)
        payload_in = email_to_dict(msg)
        if existing is not None and "_classify_cache" in (existing.payload or {}):
            payload_in["_classify_cache"] = existing.payload["_classify_cache"]
        item = store.upsert_item("email", msg.id, kind="lead_reply", payload=payload_in)
        if item.intent and item.draft is not None:
            return item, False
        classification = (item.payload or {}).get("_classify_cache")

    if not classification:
        try:
            classification = classify_lead(settings, store, item, msg, provider=provider)
        except LLMSchemaError as exc:
            if item is not None:
                store.set_fields(item.id, error=str(exc))
                updated = store.transition(item.id, "needs_human", actor="agent",
                                           detail={"error": "classify_schema_error"})
                return updated, True
            raise
        if item is not None:
            item = store.set_fields(
                item.id, payload={**(item.payload or {}), "_classify_cache": classification}) or item
    kind = classification.get("kind", "other")
    headcount = int(classification.get("headcount") or 0)
    nights = int(classification.get("nights_wanted") or 2) or 2
    requested_pct = float(classification.get("discount_pct_requested") or 0)

    detected_lang = classification.get("language") or settings.hotel.default_language
    language_unsupported = detected_lang not in settings.hotel.languages
    if language_unsupported:
        language = settings.hotel.default_language
    elif settings.agent_get("pipeline.language_match", True):
        language = detected_lang
    else:
        language = settings.hotel.default_language

    floor_pct = float(settings.agent_get("pipeline.discount_floor_pct", 15))
    applied_pct, flagged = clamp_discount(requested_pct, floor_pct)

    availability_line, price_line = "", ""
    est_value = 0.0
    if kind not in OUT_OF_SCOPE_KINDS and settings.agent_get("pipeline.availability_check", True):
        today = date.today()
        rates = pms.get_rates(today.isoformat(),
                              (today + timedelta(days=int(settings.agent_get(
                                  "pipeline.availability_window_days", 21)))).isoformat())
        avail = nearest_midweek_block(
            rates, today=today,
            window_days=int(settings.agent_get("pipeline.availability_window_days", 21)),
            event_room_type=str(settings.agent_get("pipeline.event_room_type", "classic")),
            total_rooms=settings.hotel.rooms)
        availability_line = avail.line
        if avail.found:
            round_step = int(settings.agent_get("pipeline.price_round_to", 5))
            if kind == "conference":
                _, total, price_line = price_conference(
                    avail.nightly_rate, headcount, nights,
                    day_delegate_fee=float(settings.agent_get("pipeline.day_delegate_fee", 60)),
                    dinner_fee=float(settings.agent_get("pipeline.dinner_fee", 95)),
                    round_step=round_step)
                est_value = total
            else:
                net, price_line = price_discounted(avail.nightly_rate, applied_pct,
                                                    round_step=round_step)
                est_value = net * max(1, headcount) * nights

    payload = dict((item.payload if item else None) or email_to_dict(msg))
    payload.update({"stage": "inquiry", "kind": kind, "headcount": headcount,
                    "nights_wanted": nights, "est_value": round(est_value, 2),
                    "discount_requested_pct": requested_pct, "discount_applied_pct": applied_pct,
                    "discount_flagged": flagged, "language": language,
                    "language_unsupported": language_unsupported})
    if not dry:
        store.set_fields(item.id, payload=payload)

    try:
        draft = draft_lead_reply(
            settings, store, item, reply_language=language, kind=kind, headcount=headcount,
            nights_wanted=nights, availability_line=availability_line, price_line=price_line,
            discount_flagged=flagged, enquiry_body=msg.body_text, fixture_id=msg.id,
            provider=provider)
    except LLMSchemaError as exc:
        if item is not None:
            store.set_fields(item.id, error=str(exc))
            updated = store.transition(item.id, "needs_human", actor="agent",
                                       detail={"error": "draft_schema_error", "kind": kind})
            return updated, True
        raise

    human = needs_human_for(kind, headcount, float(classification.get("confidence") or 0),
                            flagged, bool(draft.get("needs_human")), settings,
                            language_unsupported=language_unsupported)
    status = "needs_human" if human else "pending_review"
    reason = (f"guest wrote in {detected_lang}, not in hotel.languages"
             if language_unsupported else "")

    if dry:
        preview = Item(id=f"dry-run:{msg.id}", kind="lead_reply", source="email",
                       external_id=msg.id,
                       payload=payload, intent=kind,
                       confidence=float(classification.get("confidence") or 0), draft=draft,
                       review_status=status, created_at="", updated_at="")
        return preview, True

    updated = store.transition(item.id, status, actor="agent",
                               detail={"kind": kind, **({"reason": reason} if reason else {})})

    if kind not in OUT_OF_SCOPE_KINDS:
        # No point chasing a reply to "this desk doesn't handle single rooms" -
        # the follow-up loop only exists for enquiries this desk actually works.
        gap_days = int(settings.agent_get("pipeline.follow_up_gap_days", 4))
        max_follow_ups = int(settings.agent_get("pipeline.max_follow_ups", 3))
        store.upsert_task(
            "lead_followup", item.id, max_follow_ups=max_follow_ups,
            next_action_due=(datetime.now().astimezone()
                             + timedelta(days=gap_days)).isoformat(timespec="seconds"))
    return updated, True


def build_followup_nudge(*, hotel_name: str, subject: str, follow_up_count: int) -> dict:
    """A short, deterministic check-in - no LLM needed for a one-line bump."""
    return {
        "subject": f"Re: {subject} - checking in",
        "body": (f"Hello,\n\nJust checking this hasn't slipped through the cracks on your "
                f"side - the quote below is still open and I would rather hold the dates "
                f"for you than release them.\n\nHappy to jump on a quick call if that is "
                f"easier.\n\nWarm regards,\n{hotel_name} Sales & Events"),
        "needs_human": False,
    }
