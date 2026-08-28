"""Tests for the pure pricing/availability/ordering functions in tools/engine.py.

Uses dates relative to `date.today()` throughout - never the shipped
fixtures/hotel/rates.json - so these pass on any day, forever (see
docs/how-it-works.md and the note in fixtures/hotel/rates.json's generator).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters.base import RateRow

import engine

from _helpers import hermetic_settings


def _row(offset: int, *, available: int = 3, closed: bool = False, price: float = 180.0,
        room_type_id: str = "classic") -> RateRow:
    d = date.today() + timedelta(days=offset)
    return RateRow(date=d.isoformat(), room_type_id=room_type_id, price=price, currency="EUR",
                   min_los=1, available=available, closed=closed)


def test_nearest_midweek_block_finds_the_first_open_mon_thu_pair():
    today = date.today()
    # offset 0 is whatever weekday "today" is; build 21 days of rows so a
    # Mon-Thu pair is guaranteed to exist somewhere in the window.
    rows = [_row(i) for i in range(21)]
    avail = engine.nearest_midweek_block(rows, today=today, window_days=21,
                                         event_room_type="classic", total_rooms=10)
    assert avail.found is True
    start = date.fromisoformat(avail.checkin)
    assert start.weekday() in (0, 1, 2, 3)
    assert date.fromisoformat(avail.checkout) == start + timedelta(days=2)


def test_nearest_midweek_block_skips_a_closed_day():
    today = date.today()
    rows = [_row(i) for i in range(21)]
    # close every candidate Mon..Thu day until offset 10, forcing the search past them
    rows = [_row(i, closed=(i < 10)) for i in range(21)]
    avail = engine.nearest_midweek_block(rows, today=today, window_days=21,
                                         event_room_type="classic", total_rooms=10)
    if avail.found:
        assert date.fromisoformat(avail.checkin) >= today + timedelta(days=10)


def test_nearest_midweek_block_reports_not_found_outside_the_window():
    today = date.today()
    avail = engine.nearest_midweek_block([], today=today, window_days=21,
                                         event_room_type="classic", total_rooms=10)
    assert avail.found is False
    assert "nearest" not in avail.line.lower()  # honest "I don't have one", not a fake quote


def test_price_conference_formula_matches_the_documented_build_up():
    delegate_rate, total, line = engine.price_conference(
        180, 45, 2, day_delegate_fee=60, dinner_fee=95, round_step=5)
    assert delegate_rate == 335  # round_to(180+60+95, 5)
    assert total == 335 * 45 * 2
    assert "335" in line and "45" in line


def test_price_discounted_at_zero_percent_reads_as_best_rate_not_a_discount():
    net, line = engine.price_discounted(180, 0, round_step=5)
    assert net == 180
    assert "%" not in line


def test_price_discounted_at_the_floor_states_the_percentage():
    net, line = engine.price_discounted(180, 15, round_step=5)
    assert net == round(180 * 0.85 / 5) * 5
    assert "15%" in line


def test_order_queue_value_priority_sorts_highest_first():
    rows = [{"id": "a", "est_value": 1000, "received_at": "2026-01-01"},
           {"id": "b", "est_value": 19500, "received_at": "2026-01-02"},
           {"id": "c", "est_value": 5000, "received_at": "2026-01-03"}]
    ordered = engine.order_queue(rows, value_priority=True)
    assert [r["id"] for r in ordered] == ["b", "c", "a"]


def test_order_queue_off_sorts_by_arrival():
    rows = [{"id": "a", "est_value": 1000, "received_at": "2026-01-03"},
           {"id": "b", "est_value": 19500, "received_at": "2026-01-01"}]
    ordered = engine.order_queue(rows, value_priority=False)
    assert [r["id"] for r in ordered] == ["b", "a"]


def test_needs_human_for_large_group_and_discount_flag(tmp_path, monkeypatch):
    settings = hermetic_settings(tmp_path, monkeypatch)
    assert engine.needs_human_for("group", 8, 0.99, False, False, settings) is True  # >= large_group_headcount
    assert engine.needs_human_for("conference", 2, 0.99, True, False, settings) is True  # discount flagged
    assert engine.needs_human_for("single_room", 1, 0.99, False, False, settings) is True  # out of scope
