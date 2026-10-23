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
    # Exact-sizing tests: no demand margin unless a test asks for one.
    starts = series(start, hours)
    load = [(heat_kw + base_kw) * 0.5 for _ in starts]
    pv = [bell(t, pv_peak) * 0.5 for t in starts]
    return pl.make_plan(
        starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=soc,
        params=params or pl.BatteryParams(load_margin_pct=0), charge_windows=COSY, discharge_windows=NIGHT,
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
    p = pl.BatteryParams(load_margin_pct=0)
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


def test_battery_holds_at_target_through_charge_window():
    """In a timed charge slot the house runs from the grid; SOC doesn't saw-tooth."""
    start = datetime(2026, 1, 15, 0, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=2.0, pv_peak=0.0, soc=18,
                        params=pl.BatteryParams(charge_power_kw=10))
    m = find(plan, "charge", datetime(2026, 1, 15, 4, tzinfo=LON))
    assert m.enabled
    in_window = [v for t, v in plan.soc
                 if datetime(2026, 1, 15, 4, 30, tzinfo=LON) <= t <= datetime(2026, 1, 15, 7, tzinfo=LON)]
    assert all(b >= a - 1e-9 for a, b in zip(in_window, in_window[1:])), in_window  # never drains
    assert abs(in_window[-1] - m.target_soc) < 0.01, in_window                       # ends on target


def test_cautious_sizing_charges_more_but_projection_stays_expected():
    """Pessimistic solar and a demand margin raise the charge; the projected
    SOC still follows the expected forecast, so the battery is projected to
    arrive above the reserve, which is the buffer."""
    start = datetime(2026, 3, 10, 0, tzinfo=LON)
    starts = series(start)
    load = [(1.2 + 0.4) * 0.5 for _ in starts]
    pv = [bell(t, 4.0) * 0.5 for t in starts]
    pv_low = [x * 0.4 for x in pv]
    base = dict(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=40,
                charge_windows=COSY, discharge_windows=NIGHT)
    plain = pl.make_plan(**base, params=pl.BatteryParams(load_margin_pct=0))
    cautious = pl.make_plan(**base, pv_low_kwh=pv_low, params=pl.BatteryParams(load_margin_pct=10))
    when = datetime(2026, 3, 10, 13, tzinfo=LON)
    a, c = find(plain, "charge", when), find(cautious, "charge", when)
    assert c.target_soc > a.target_soc
    # Expected path: the cautious plan reaches the 22:00 window with a buffer.
    t22 = datetime(2026, 3, 10, 22, tzinfo=LON)
    assert dict(cautious.soc)[t22] > dict(plain.soc)[t22] + 5


def _replay_lost(plan, starts, load, pv, soc0, p, periods=48):
    """Replay the plan's modes over the first day; return (kWh clipped, SOC path)."""
    cap, dt_h, lost, s = p.capacity_kwh, 0.5, 0.0, soc0
    modes = dict(plan.modes)
    path = []
    for i, t in enumerate(starts[:periods]):
        spare = pv[i] - load[i]
        if spare > 0:
            room = (100 - s) / 100 * cap
            if modes[t] == "Feed-in priority":
                to_batt = min(room, max(0.0, spare - p.export_limit_kw * dt_h))
                lost += max(0.0, spare - p.export_limit_kw * dt_h - to_batt)
            else:
                to_batt = min(room, spare)
                lost += max(0.0, spare - to_batt - p.export_limit_kw * dt_h)
            s += to_batt / cap * 100
        else:
            s = max(p.min_soc, s + spare / cap * 100)
        path.append(s)
    return lost, path


def test_self_use_all_day_when_solar_never_exceeds_export_limit():
    start = datetime(2026, 1, 15, 0, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=1.0, pv_peak=3.0, soc=50)
    assert {m for t, m in plan.modes if t.date() == start.date()} == {"Self-Use"}


def test_big_day_flips_to_self_use_as_early_as_possible_without_clipping():
    start = datetime(2026, 6, 15, 0, tzinfo=LON)
    params = pl.BatteryParams(discharge_threshold_soc=0)       # isolate day 1: no discharge
    plan, starts, load, pv = plan_for(start, heat_kw=0.0, pv_peak=9.0, soc=40, params=params)
    today = [(t, m) for t, m in plan.modes if t.date() == start.date()]
    flips = [t for (t0, m0), (t, m) in zip(today, today[1:]) if m0 != m]
    assert len(flips) == 1 and dict(today)[flips[0]] == "Self-Use", flips
    assert 8 <= flips[0].hour <= 14
    lost, path = _replay_lost(plan, starts, load, pv, 40, params)
    assert lost < 0.2, lost                      # no clipping
    # Flipping any earlier would clip.
    early = pl.Plan(windows=plan.windows, soc=plan.soc,
                    modes=[(t, "Self-Use" if t >= flips[0] - timedelta(hours=1) else m) for t, m in plan.modes])
    lost_early, _ = _replay_lost(early, starts, load, pv, 40, params)
    assert lost_early > lost + 0.2
    # And it stores more than the old "fill just in time" rule would have.
    old_modes = []
    s = 40.0
    for i, t in enumerate(starts):
        left = sum(pv[j] for j in range(i, len(starts)) if starts[j].date() == t.date())
        old_modes.append((t, "Feed-in priority" if left > (100 - s) / 100 * 32 else "Self-Use"))
        s = pl._step(s, None, pv[i], load[i], 0.5, params, old_modes[-1][1] != "Self-Use")
    _, old_path = _replay_lost(pl.Plan(modes=old_modes), starts, load, pv, 40, params)
    i17 = starts.index(datetime(2026, 6, 15, 17, tzinfo=LON))
    assert path[i17] >= old_path[i17] - 0.5


def _cosy_rates(day, cheap=((4, 7), (13, 16), (22, 24)), peak=(16, 19)):
    out = []
    for i in range(48):
        s = day + timedelta(minutes=30 * i)
        h = s.hour + s.minute / 60
        price = 0.27
        if any(a <= h < b for a, b in cheap):
            price = 0.135
        elif peak[0] <= h < peak[1]:
            price = 0.40
        out.append((s, s + timedelta(minutes=30), price))
    return out


def test_cheap_ranges_from_cosy_rates():
    day = datetime(2026, 10, 23, tzinfo=LON)
    ranges = pl.cheap_ranges(_cosy_rates(day))
    assert [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in ranges] == [
        ("04:00", "07:00"), ("13:00", "16:00"), ("22:00", "00:00")]


def test_cheap_ranges_follow_a_changed_tariff():
    day = datetime(2026, 10, 23, tzinfo=LON)
    ranges = pl.cheap_ranges(_cosy_rates(day, cheap=((3, 6), (12, 14.5))))
    assert [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in ranges] == [("03:00", "06:00"), ("12:00", "14:30")]
    occ = pl.ranges_to_occurrences(ranges, "charge", day, day + timedelta(days=1))
    assert [o.slot for o in occ] == [1, 2]


def test_plan_from_ranges_matches_plan_from_fixed_windows():
    start = datetime(2026, 1, 15, 0, tzinfo=LON)
    starts = series(start)
    load = [1.1 for _ in starts]
    pv = [bell(t, 2.0) * 0.5 for t in starts]
    rates = _cosy_rates(start) + _cosy_rates(start + timedelta(days=1))
    kw = dict(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=50,
              params=pl.BatteryParams(), charge_windows=COSY, discharge_windows=NIGHT)
    fixed = pl.make_plan(**kw)
    tariff = pl.make_plan(**kw, charge_ranges=pl.cheap_ranges(rates))
    key = lambda p: [(w.kind, w.slot, w.start, w.enabled, w.target_soc) for w in p.windows]
    assert key(fixed) == key(tariff)


def test_skipping_a_window_counts_the_window_itself():
    """Regression (23 Oct): battery just above the 16:00-22:00 target at 13:00,
    dull afternoon. Skipping the slot would leave the battery running the house
    13:00-16:00 and hit the floor before 22:00. The slot must stay on."""
    start = datetime(2026, 10, 23, 12, 30, tzinfo=LON)
    starts = series(start, hours=30)
    load = [(0.6 + 0.4) * 0.5 for _ in starts]
    pv = [bell(t, 0.3) * 0.5 for t in starts]
    plan = pl.make_plan(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=43,
                        params=pl.BatteryParams(), charge_windows=COSY, discharge_windows=NIGHT)
    w = find(plan, "charge", datetime(2026, 10, 23, 13, tzinfo=LON))
    assert w.enabled, w.reason
    assert w.target_soc >= 42                      # holds at least the current level
    soc = dict(plan.soc)
    assert soc[datetime(2026, 10, 23, 22, tzinfo=LON)] >= 20, soc[datetime(2026, 10, 23, 22, tzinfo=LON)]
    assert min(v for t, v in plan.soc if t <= datetime(2026, 10, 23, 22, tzinfo=LON)) > 20


def test_skip_when_battery_covers_window_and_gap():
    start = datetime(2026, 10, 23, 12, 30, tzinfo=LON)
    starts = series(start, hours=30)
    load = [(0.6 + 0.4) * 0.5 for _ in starts]
    pv = [bell(t, 0.3) * 0.5 for t in starts]
    plan = pl.make_plan(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=80,
                        params=pl.BatteryParams(), charge_windows=COSY, discharge_windows=NIGHT)
    w = find(plan, "charge", datetime(2026, 10, 23, 13, tzinfo=LON))
    assert not w.enabled and "enough" in w.reason
    assert dict(plan.soc)[datetime(2026, 10, 23, 22, tzinfo=LON)] >= 20


def test_property_never_below_reserve_before_next_window():
    """Whatever the plan decides, replaying its charge slots with the cautious
    inputs must keep the battery at or above the reserve until each next cheap
    window, unless the plan itself says the charge rate can't cover it."""
    import random

    rng = random.Random(1234)
    p = pl.BatteryParams(load_margin_pct=0)
    for case in range(200):
        start = datetime(2026, rng.choice([1, 3, 6, 10, 11]), rng.randint(1, 28),
                         rng.randint(0, 23), rng.choice([0, 30]), tzinfo=LON)
        starts = series(start, hours=36)
        heat = rng.uniform(0, 2.5)
        load = [(heat * rng.uniform(0.7, 1.3) + 0.4) * 0.5 for _ in starts]
        peak = rng.uniform(0, 9)
        pv = [bell(t, peak) * 0.5 for t in starts]
        soc0 = rng.uniform(15, 95)
        plan = pl.make_plan(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=soc0,
                            params=p, charge_windows=COSY, discharge_windows=pl.parse_windows(""))
        acts = {}
        for w in plan.windows:
            if w.kind == "charge" and w.enabled:
                for i, t in enumerate(starts):
                    if w.start <= t < w.end:
                        acts[i] = ("charge", w.target_soc)
        charge_starts = sorted(w.start for w in plan.windows if w.kind == "charge")
        shortish = {w.start for w in plan.windows if "charge rate too low" in w.reason}
        s, path = soc0, []
        for i, t in enumerate(starts):
            s = pl._step(s, acts.get(i), pv[i], load[i], 0.5, p)
            path.append((t + timedelta(minutes=30), s))
        # Check each gap from a window's start to the next window's start.
        for w0, w1 in zip(charge_starts, charge_starts[1:]):
            if w0 in shortish or w0 < starts[0]:
                continue
            low = min(v for t, v in path if w0 < t <= w1)
            start_soc = next((v for t, v in path if t == w0), soc0)
            if start_soc < p.reserve_soc:
                continue        # started the window below reserve already; can only recover
            assert low >= p.reserve_soc - 1e-6, (case, start, w0, low)
