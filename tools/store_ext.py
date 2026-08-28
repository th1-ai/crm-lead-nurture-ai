"""tools/store_ext.py - CRM / Lead Nurture AI's own tables, layered on core.store.Store.

The generic `items` table (core/store.py) is the one review queue: a lead
reply, an outreach step, an outreach inbox reply and a win-back letter are all
rows there, told apart by `kind`. This module adds the relational state that
does not fit a queue row - the outreach prospect list, its sources and
campaigns, and the win-back cohort - plus the pure helper functions the
engines and the tests share.

Call :func:`ensure_schema` once per `Store` before touching any of these
tables; every tool in this repo does it right after constructing its `Store`.
Nothing here replaces `core.store` - additive, same connection (`store.db`),
same `utcnow()` convention, same JSON-column convention.
"""

from __future__ import annotations

import json
import uuid

from core.store import Store, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach_sources (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  seeded      INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_prospects (
  id             TEXT PRIMARY KEY,
  avatar         TEXT NOT NULL,
  org            TEXT NOT NULL,
  first_name     TEXT NOT NULL,
  last_name      TEXT NOT NULL,
  role           TEXT,
  domain         TEXT,
  city           TEXT,
  email          TEXT,
  email_status   TEXT NOT NULL DEFAULT 'missing',
  enrich_provider TEXT,
  enrich_cost    REAL NOT NULL DEFAULT 0,
  signal         TEXT,
  source_id      TEXT,
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  seeded         INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prospects_avatar ON outreach_prospects (avatar);

CREATE TABLE IF NOT EXISTS outreach_campaigns (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  avatar      TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'mice',
  status      TEXT NOT NULL DEFAULT 'draft',
  steps_json  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_enrollments (
  id            TEXT PRIMARY KEY,
  campaign_id   TEXT NOT NULL,
  prospect_id   TEXT NOT NULL,
  step_idx      INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'queued',
  channel_kind  TEXT,
  history_json  TEXT NOT NULL DEFAULT '[]',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (campaign_id, prospect_id)
);
CREATE INDEX IF NOT EXISTS idx_enrollments_status ON outreach_enrollments (status);

CREATE TABLE IF NOT EXISTS outreach_meetings (
  id          TEXT PRIMARY KEY,
  prospect_id TEXT NOT NULL,
  slug        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'created',
  slot_at     TEXT,
  provider    TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS winback_cohort (
  id                   TEXT PRIMARY KEY,
  guest_email          TEXT,
  guest_name           TEXT NOT NULL,
  last_stay            TEXT,
  stays                INTEGER NOT NULL DEFAULT 0,
  lifetime_spend       REAL NOT NULL DEFAULT 0,
  reason_to_return     TEXT,
  reason_category      TEXT,
  prior_reservation_id TEXT,
  prior_room_type      TEXT,
  item_id              TEXT,
  status               TEXT NOT NULL DEFAULT 'new',
  created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS winback_bookings (
  id                TEXT PRIMARY KEY,
  cohort_id         TEXT NOT NULL,
  guest_name        TEXT NOT NULL,
  checkin           TEXT NOT NULL,
  checkout          TEXT NOT NULL,
  room_type         TEXT NOT NULL,
  total_eur         REAL NOT NULL,
  pms_reservation_id TEXT,
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_proposals (
  id             TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  pattern        TEXT NOT NULL,
  intent         TEXT,
  cluster_size   INTEGER NOT NULL,
  example_before TEXT,
  example_after  TEXT,
  suggested_fix  TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',
  decided_at     TEXT,
  applied_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON coach_proposals (status, created_at);
"""


def ensure_schema(store: Store) -> None:
    """Create every table above if it does not already exist. Idempotent."""
    store.db.executescript(SCHEMA)


def new_id() -> str:
    return uuid.uuid4().hex


# -- sources ----------------------------------------------------------------
def upsert_source(store: Store, name: str, *, status: str = "pending",
                  seeded: bool = False) -> str:
    row = store.db.execute("SELECT id FROM outreach_sources WHERE name=?", (name,)).fetchone()
    if row is not None:
        return row["id"]
    sid = new_id()
    store.db.execute(
        "INSERT INTO outreach_sources (id, name, status, seeded, created_at) "
        "VALUES (?,?,?,?,?)", (sid, name, status, int(seeded), utcnow()))
    return sid


def list_sources(store: Store) -> list[dict]:
    return [dict(r) for r in store.db.execute(
        "SELECT * FROM outreach_sources ORDER BY created_at ASC").fetchall()]


def set_source_status(store: Store, source_id: str, status: str) -> None:
    store.db.execute("UPDATE outreach_sources SET status=? WHERE id=?", (status, source_id))


def source_usable(store: Store, source_id: str | None, *, vetting: bool) -> bool:
    if not source_id or not vetting:
        return True
    row = store.db.execute("SELECT status FROM outreach_sources WHERE id=?",
                           (source_id,)).fetchone()
    return bool(row) and row["status"] in ("approved", "testing")


# -- prospects ----------------------------------------------------------------
def upsert_prospect(store: Store, *, avatar: str, org: str, first_name: str, last_name: str,
                    role: str = "", domain: str = "", city: str = "", email: str = "",
                    signal: str = "", source_id: str = "", do_not_contact: bool = False,
                    seeded: bool = False) -> str:
    row = store.db.execute(
        "SELECT id FROM outreach_prospects WHERE avatar=? AND org=? AND first_name=? "
        "AND last_name=?", (avatar, org, first_name, last_name)).fetchone()
    if row is not None:
        return row["id"]
    pid = new_id()
    store.db.execute(
        "INSERT INTO outreach_prospects (id, avatar, org, first_name, last_name, role, "
        "domain, city, email, email_status, signal, source_id, do_not_contact, seeded, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, avatar, org, first_name, last_name, role, domain, city, email,
         "found" if email else "missing", signal, source_id, int(do_not_contact),
         int(seeded), utcnow()))
    return pid


def get_prospect(store: Store, prospect_id: str) -> dict | None:
    row = store.db.execute("SELECT * FROM outreach_prospects WHERE id=?",
                           (prospect_id,)).fetchone()
    return dict(row) if row else None


def list_prospects(store: Store, *, avatar: str | None = None,
                   revealed_only: bool = False) -> list[dict]:
    sql, params = "SELECT * FROM outreach_prospects", []
    if avatar:
        sql += " WHERE avatar=?"
        params.append(avatar)
    rows = [dict(r) for r in store.db.execute(sql + " ORDER BY created_at ASC", params)]
    return rows


def set_prospect_email(store: Store, prospect_id: str, *, email: str, status: str,
                       provider: str, cost: float) -> None:
    store.db.execute(
        "UPDATE outreach_prospects SET email=?, email_status=?, enrich_provider=?, "
        "enrich_cost=? WHERE id=?", (email, status, provider, cost, prospect_id))


# -- campaigns + enrollments --------------------------------------------------
def create_campaign(store: Store, *, name: str, avatar: str, kind: str,
                    steps: list[dict]) -> str:
    cid = new_id()
    store.db.execute(
        "INSERT INTO outreach_campaigns (id, name, avatar, kind, status, steps_json, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (cid, name, avatar, kind, "draft", json.dumps(steps), utcnow()))
    return cid


def get_campaign(store: Store, campaign_id: str) -> dict | None:
    row = store.db.execute("SELECT * FROM outreach_campaigns WHERE id=?",
                           (campaign_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["steps"] = json.loads(d.pop("steps_json"))
    return d


def list_campaigns(store: Store) -> list[dict]:
    return [get_campaign(store, r["id"]) for r in
           store.db.execute("SELECT id FROM outreach_campaigns ORDER BY created_at ASC")]


def launch_campaign(store: Store, campaign_id: str) -> None:
    store.db.execute("UPDATE outreach_campaigns SET status='active' WHERE id=?", (campaign_id,))


def enroll(store: Store, campaign_id: str, prospect_id: str) -> tuple[str, bool]:
    row = store.db.execute(
        "SELECT id FROM outreach_enrollments WHERE campaign_id=? AND prospect_id=?",
        (campaign_id, prospect_id)).fetchone()
    if row is not None:
        return row["id"], False
    eid = new_id()
    now = utcnow()
    store.db.execute(
        "INSERT INTO outreach_enrollments (id, campaign_id, prospect_id, step_idx, status, "
        "history_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (eid, campaign_id, prospect_id, 0, "queued", "[]", now, now))
    return eid, True


def get_enrollment(store: Store, enrollment_id: str) -> dict | None:
    row = store.db.execute("SELECT * FROM outreach_enrollments WHERE id=?",
                           (enrollment_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["history"] = json.loads(d.pop("history_json") or "[]")
    return d


def list_enrollments(store: Store, *, campaign_id: str | None = None,
                     status: str | None = None) -> list[dict]:
    sql, params = "SELECT id FROM outreach_enrollments", []
    where = []
    if campaign_id:
        where.append("campaign_id=?")
        params.append(campaign_id)
    if status:
        where.append("status=?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [get_enrollment(store, r["id"]) for r in store.db.execute(sql, params)]


def advance_enrollment(store: Store, enrollment_id: str, *, step_idx: int, status: str,
                       channel_kind: str = "", note: str = "") -> None:
    enr = get_enrollment(store, enrollment_id)
    history = (enr or {}).get("history", [])
    history.append({"ts": utcnow(), "step_idx": step_idx, "status": status, "note": note})
    store.db.execute(
        "UPDATE outreach_enrollments SET step_idx=?, status=?, channel_kind=?, "
        "history_json=?, updated_at=? WHERE id=?",
        (step_idx, status, channel_kind, json.dumps(history), utcnow(), enrollment_id))


def record_meeting(store: Store, prospect_id: str, *, slug: str, provider: str,
                   slot_at: str = "", status: str = "created") -> str:
    mid = new_id()
    store.db.execute(
        "INSERT INTO outreach_meetings (id, prospect_id, slug, status, slot_at, provider, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (mid, prospect_id, slug, status, slot_at, provider, utcnow()))
    return mid


def set_meeting_status(store: Store, meeting_id: str, status: str, slot_at: str = "") -> None:
    if slot_at:
        store.db.execute("UPDATE outreach_meetings SET status=?, slot_at=? WHERE id=?",
                         (status, slot_at, meeting_id))
    else:
        store.db.execute("UPDATE outreach_meetings SET status=? WHERE id=?", (status, meeting_id))


def get_meeting(store: Store, meeting_id: str) -> dict | None:
    row = store.db.execute("SELECT * FROM outreach_meetings WHERE id=?",
                           (meeting_id,)).fetchone()
    return dict(row) if row else None


# -- win-back cohort ----------------------------------------------------------
def upsert_cohort_row(store: Store, *, guest_email: str, guest_name: str, last_stay: str,
                      stays: int, lifetime_spend: float, reason_to_return: str,
                      reason_category: str, prior_reservation_id: str = "",
                      prior_room_type: str = "") -> str:
    row = store.db.execute("SELECT id FROM winback_cohort WHERE guest_name=?",
                           (guest_name,)).fetchone()
    if row is not None:
        return row["id"]
    cid = new_id()
    store.db.execute(
        "INSERT INTO winback_cohort (id, guest_email, guest_name, last_stay, stays, "
        "lifetime_spend, reason_to_return, reason_category, prior_reservation_id, "
        "prior_room_type, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, guest_email, guest_name, last_stay, stays, lifetime_spend, reason_to_return,
         reason_category, prior_reservation_id, prior_room_type, "new", utcnow()))
    return cid


def get_cohort_row(store: Store, cohort_id: str) -> dict | None:
    row = store.db.execute("SELECT * FROM winback_cohort WHERE id=?", (cohort_id,)).fetchone()
    return dict(row) if row else None


def list_cohort(store: Store, *, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM winback_cohort"
    params: list[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY (lifetime_spend * stays) DESC, id ASC"
    return [dict(r) for r in store.db.execute(sql, params)]


def set_cohort_status(store: Store, cohort_id: str, status: str, *, item_id: str = "") -> None:
    if item_id:
        store.db.execute("UPDATE winback_cohort SET status=?, item_id=? WHERE id=?",
                         (status, item_id, cohort_id))
    else:
        store.db.execute("UPDATE winback_cohort SET status=? WHERE id=?", (status, cohort_id))


def record_winback_booking(store: Store, *, cohort_id: str, guest_name: str, checkin: str,
                           checkout: str, room_type: str, total_eur: float,
                           pms_reservation_id: str = "") -> str:
    bid = new_id()
    store.db.execute(
        "INSERT INTO winback_bookings (id, cohort_id, guest_name, checkin, checkout, "
        "room_type, total_eur, pms_reservation_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (bid, cohort_id, guest_name, checkin, checkout, room_type, total_eur,
         pms_reservation_id, utcnow()))
    return bid


def list_winback_bookings(store: Store) -> list[dict]:
    return [dict(r) for r in store.db.execute(
        "SELECT * FROM winback_bookings ORDER BY created_at ASC")]
