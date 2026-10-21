"""Tests for the battery planner."""

import importlib.util
import math
import pathlib
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

_spec = importlib.util.spec_from_file_location(
    "hlp_planner",
    pathlib.Path(__file__).parents[1] / "custom_components" / "heat_load_predictor" / "planner.py",
)
pl = importlib.util.module_from_spec(_spec)
sys.modules["hlp_planner"] = pl
_spec.loader.exec_module(pl)

LON = ZoneInfo("Europe/London")
COSY = pl.parse_windows("04:00-07:00, 13:00-16:00, 22:00-00:00")
NIGHT = pl.parse_windows("00:30-05:30")


def series(start, hours=48, period=30):
    return [start + timedelta(minutes=period * i) for i in range(int(hours * 60 / period))]


def bell(t, peak_kw, rise=8, fall=16):
    h = t.hour + t.minute / 60
    return max(0.0, peak_kw * math.sin(math.pi * (h - rise) / (fall - rise))) if rise <= h <= fall else 0.0


def plan_for(start, *, heat_kw, base_kw=0.4, pv_peak=0.0, soc=50.0, params=None, hours=48):
    starts = series(start, hours)
    load = [(heat_kw + base_kw) * 0.5 for _ in starts]
    pv = [bell(t, pv_peak) * 0.5 for t in starts]
    return pl.make_plan(
        starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=soc,
        params=params or pl.BatteryParams(), charge_windows=COSY, discharge_windows=NIGHT,
    ), starts, load, pv


def find(plan, kind, start):
    return next(w for w in plan.windows if w.kind == kind and w.start == start)


def test_parse_windows():
    assert pl.parse_windows("04:00-07:00, 22:00-24:00") == [(time(4), time(7)), (time(22), time(0))]
    assert pl.parse_windows("") == []
    assert pl.format_windows(COSY) == "04:00-07:00, 13:00-16:00, 22:00-00:00"
    with pytest.raises(ValueError):
        pl.parse_windows("04:00")
    with pytest.raises(ValueError):
        pl.parse_windows("04:00-04:00")


def test_occurrences_cross_midnight_and_dst():
    # 25 Oct 2026 is the UK clocks-back day.
    first = datetime(2026, 10, 24, 12, tzinfo=LON)
    occ = pl.occurrences(COSY, "charge", first, first + timedelta(hours=36))
    eve = [o for o in occ if o.slot == 3]
    assert eve[0].start == datetime(2026, 10, 24, 22, tzinfo=LON)
    assert eve[0].end == datetime(2026, 10, 25, 0, tzinfo=LON)
    morning = next(o for o in occ if o.slot == 1)
    assert morning.start.hour == 4 and morning.start.utcoffset() == timedelta(0)  # GMT after change


def test_winter_charges_to_cover_each_gap():
    start = datetime(2026, 1, 15, 0, tzinfo=LON)
    plan, starts, load, pv = plan_for(start, heat_kw=1.8, pv_peak=2.0, soc=60)
    p = pl.BatteryParams()
    # Morning window covers 07:00-13:00: manual running balance.
    bal = low = 0.0
    for i, t in enumerate(starts):
        if datetime(2026, 1, 15, 7, tzinfo=LON) <= t < datetime(2026, 1, 15, 13, tzinfo=LON):
            bal += pv[i] - load[i]
            low = min(low, bal)
    want = math.ceil(p.reserve_soc + (-low) / p.capacity_kwh * 100)
    m = find(plan, "charge", datetime(2026, 1, 15, 4, tzinfo=LON))
    assert m.target_soc == want
    assert m.enabled
    d = find(plan, "discharge", datetime(2026, 1, 15, 0, 30, tzinfo=LON))
    assert not d.enabled and "not worth it" in d.reason
    # Evening window covers 00:00-04:00: 8 x 2.2 kW x 0.5 h = 8.8 kWh -> 27.5 % + 20 %.
    e = find(plan, "charge", datetime(2026, 1, 15, 22, tzinfo=LON))
    assert e.target_soc == 48 and e.enabled
    # The projected SOC never drops below the minimum.
    assert min(s for _, s in plan.soc) >= p.min_soc - 1e-9


def test_battery_reaches_each_window_near_reserve():
    start = datetime(2026, 1, 15, 0, tzinfo=LON)
    plan, starts, *_ = plan_for(start, heat_kw=1.8, pv_peak=2.0, soc=60)
    soc_at = dict(plan.soc)
    for w in plan.windows:
        if w.kind == "charge" and w.enabled and w.start.date() == start.date():
            # SOC just before the *next* window opens should be about the reserve.
            nxt = min((o.start for o in plan.windows if o.kind == "charge" and o.start >= w.end), default=None)
            if nxt and nxt in soc_at:
                assert 19 <= soc_at[nxt] <= 22, (w.start, soc_at[nxt])


def test_big_solar_day_discharges_and_skips_morning_charge():
    start = datetime(2026, 6, 15, 0, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=0.0, pv_peak=12.0, soc=95)
    d = find(plan, "discharge", datetime(2026, 6, 15, 0, 30, tzinfo=LON))
    assert d.enabled and d.target_soc < 80
    assert d.room_kwh > 10
    m = find(plan, "charge", datetime(2026, 6, 15, 4, tzinfo=LON))
    assert not m.enabled and "overnight discharge" in m.reason
    a = find(plan, "charge", datetime(2026, 6, 15, 13, tzinfo=LON))
    assert not a.enabled
    # Battery must not overflow: projected SOC stays at or under 100 %.
    assert max(s for _, s in plan.soc) <= 100 + 1e-9


def test_solar_room_caps_charge():
    # Cold but sunny spring morning: heating wants charge, the solar wants room.
    start = datetime(2026, 4, 1, 0, tzinfo=LON)
    params = pl.BatteryParams(discharge_threshold_soc=0)  # no discharge, isolate the cap
    plan, *_ = plan_for(start, heat_kw=1.0, pv_peak=11.0, soc=20, params=params)
    m = find(plan, "charge", datetime(2026, 4, 1, 4, tzinfo=LON))
    assert m.room_kwh > 0
    assert m.target_soc <= 100 - (m.room_kwh + params.solar_headroom_kwh) / 32 * 100 + 1


def test_full_battery_needs_no_charge():
    start = datetime(2026, 1, 15, 3, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=0.5, soc=100)
    m = find(plan, "charge", datetime(2026, 1, 15, 4, tzinfo=LON))
    assert not m.enabled and "enough" in m.reason


def test_window_in_progress_uses_current_soc():
    start = datetime(2026, 1, 15, 4, 30, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=1.5, soc=30)
    m = find(plan, "charge", datetime(2026, 1, 15, 4, tzinfo=LON))
    assert m.projected_soc == 30
    assert m.enabled
