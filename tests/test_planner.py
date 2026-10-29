"""Tests for the battery planner."""

import importlib.util
import math
import pathlib
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from dataclasses import replace

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
    # Real elapsed time, as Home Assistant provides it (correct across DST).
    return [pl._later(start, timedelta(minutes=period * i)) for i in range(int(hours * 60 / period))]


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
        params=params or pl.BatteryParams(load_margin_pct=0, fill_cheap_windows=False), charge_windows=COSY, discharge_windows=NIGHT,
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
    # Mild sunny spring morning: little heating, the solar wants room.
    start = datetime(2026, 4, 1, 0, tzinfo=LON)
    params = pl.BatteryParams(discharge_threshold_soc=0)  # no discharge, isolate the cap
    plan, *_ = plan_for(start, heat_kw=0.2, pv_peak=11.0, soc=20, params=params)
    m = find(plan, "charge", datetime(2026, 4, 1, 4, tzinfo=LON))
    assert m.room_kwh > 0
    assert m.target_soc <= 100 - (m.room_kwh + params.solar_headroom_kwh) / 32 * 100 + 1


def test_spill_day_charges_only_to_carry_the_house_to_the_solar():
    """Sunny spring day on the expected forecast (the battery will fill and
    spill), dull on the pessimistic one. Holding the reserve would buy power the
    solar would replace for free, so the window charges only enough for the
    battery to reach its minimum no earlier than the (pessimistic) solar takes
    over the house."""
    start = datetime(2026, 4, 1, 0, tzinfo=LON)
    starts = series(start)
    load = [(2.0 + 0.4) * 0.5 for _ in starts]
    pv = [bell(t, 14.0) * 0.5 for t in starts]
    pv_low = [x * 0.3 for x in pv]
    params = pl.BatteryParams(discharge_threshold_soc=0)
    plan = pl.make_plan(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv,
                        pv_low_kwh=pv_low, soc_now=20, params=params,
                        charge_windows=COSY, discharge_windows=NIGHT)
    m = find(plan, "charge", datetime(2026, 4, 1, 4, tzinfo=LON))
    assert m.enabled and "until the solar takes over" in m.reason, m.reason
    # Replay on the cautious figures: never below the minimum before 13:00.
    load_c = [x * 1.1 for x in load]
    i0 = starts.index(m.start)
    s = m.projected_soc
    for i in range(i0, len(starts)):
        t = starts[i]
        if t >= datetime(2026, 4, 1, 13, tzinfo=LON):
            break
        act = ("charge", m.target_soc) if t < m.end else None
        s = pl._step(s, act, pv_low[i], load_c[i], 0.5, replace(params, min_soc=0))
        assert s >= params.min_soc - 0.5, (t, s)
    # And it doesn't hold the reserve: the cautious path dips below it.
    assert m.target_soc < 40


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
    plain = pl.make_plan(**base, params=pl.BatteryParams(load_margin_pct=0, fill_cheap_windows=False))
    cautious = pl.make_plan(**base, pv_low_kwh=pv_low,
                            params=pl.BatteryParams(load_margin_pct=10, fill_cheap_windows=False))
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


def _feed_in_first_modes(starts, load, pv, soc0, p):
    """The old rule: Feed-in priority until Self-Use from here can't clip."""
    modes, s = [], soc0
    for i, t in enumerate(starts):
        end = next((j for j in range(i, len(starts)) if starts[j].date() != t.date()), len(starts))
        safe = pl._self_use_clipping(pv, load, i, end, s, 0.5, p) < pl.CLIP_TOLERANCE_KWH
        feed_in = pv[i] > load[i] and not safe
        modes.append((t, "Feed-in priority" if feed_in else "Self-Use"))
        s = pl._step(s, None, pv[i], load[i], 0.5, p, feed_in)
    return pl.Plan(modes=modes)


def test_big_day_stores_first_and_feeds_in_only_through_the_peak():
    """Store as much as possible, as early as possible, without clipping: Self-Use
    until the room left is just what the solar above the export limit still
    needs, Feed-in priority through the peak, then Self-Use again."""
    start = datetime(2026, 6, 15, 0, tzinfo=LON)
    params = pl.BatteryParams(discharge_threshold_soc=0,        # isolate day 1: no discharge
                              fill_cheap_windows=False)
    plan, starts, load, pv = plan_for(start, heat_kw=0.0, pv_peak=9.0, soc=40, params=params)
    # Only periods with spare solar: elsewhere the mode makes no difference.
    today = [m for i, (t, m) in enumerate(plan.modes)
             if t.astimezone(LON).date() == start.date() and pv[i] > load[i]]
    runs = [m for k, m in enumerate(today) if k == 0 or today[k - 1] != m]
    assert runs == ["Self-Use", "Feed-in priority", "Self-Use"], runs
    lost, path = _replay_lost(plan, starts, load, pv, 40, params)
    assert lost < 0.2, lost                      # no clipping
    assert sum(c for t, c in plan.clipped if t.astimezone(LON).date() == start.date()) < 0.2
    # Ahead of the old feed-in-first rule all morning; behind it only near the
    # end of the peak, by at most the headroom plus one half-hour's export; full
    # by the same time.
    old = _feed_in_first_modes(starts, load, pv, 40.0, params)
    old_lost, old_path = _replay_lost(old, starts, load, pv, 40, params)
    assert old_lost < 0.2
    margin = (params.solar_headroom_kwh + params.export_limit_kw * 0.5) / params.capacity_kwh * 100
    assert all(a >= b - margin for a, b in zip(path, old_path))
    noon = starts.index(datetime(2026, 6, 15, 12, tzinfo=LON))
    assert all(a >= b - 0.5 for a, b in zip(path[:noon], old_path[:noon]))
    assert path[noon] > old_path[noon] + 5
    full = starts.index(datetime(2026, 6, 15, 14, 30, tzinfo=LON))
    assert path[full] >= 99.5 and old_path[full] >= 99.5


def test_storing_more_before_the_peak_would_clip():
    """With the room sized on a forecast taken as exact (no headroom), the first
    Feed-in period is as late as it can be: Self-Use there clips."""
    start = datetime(2026, 6, 15, 0, tzinfo=LON)
    params = pl.BatteryParams(discharge_threshold_soc=0, fill_cheap_windows=False)
    _, starts, load, pv = plan_for(start, heat_kw=0.0, pv_peak=9.0, soc=40, params=params)
    plan = pl.make_plan(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, pv_high_kwh=pv,
                        soc_now=40, params=params, charge_windows=[], discharge_windows=[])
    first = next(t for t, m in plan.modes if m == "Feed-in priority")
    lost, _ = _replay_lost(plan, starts, load, pv, 40, params)
    assert lost < 0.2, lost
    greedier = pl.Plan(modes=[(t, "Self-Use" if t == first else m) for t, m in plan.modes])
    lost_greedier, _ = _replay_lost(greedier, starts, load, pv, 40, params)
    assert lost_greedier > lost + 0.05, (lost, lost_greedier)


def test_optimistic_forecast_keeps_more_room():
    """Clipping comes from solar beating the forecast: sized on a higher
    forecast, the plan switches to Feed-in priority sooner."""
    start = datetime(2026, 6, 15, 0, tzinfo=LON)
    params = pl.BatteryParams(discharge_threshold_soc=0, fill_cheap_windows=False)
    _, starts, load, pv = plan_for(start, heat_kw=0.0, pv_peak=9.0, soc=40, params=params)
    kw = dict(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=40,
              params=params, charge_windows=[], discharge_windows=[])
    exact = pl.make_plan(**kw, pv_high_kwh=pv)
    high = pl.make_plan(**kw, pv_high_kwh=[x * 1.2 for x in pv])
    first = lambda p: next(t for t, m in p.modes if m == "Feed-in priority")  # noqa: E731
    assert first(high) < first(exact)


def test_unavoidable_clipping_is_reported():
    """A peak bigger than an empty battery can absorb: the plan says how much
    will be lost rather than pretending."""
    start = datetime(2026, 6, 15, 0, tzinfo=LON)
    params = pl.BatteryParams(capacity_kwh=5, discharge_threshold_soc=0, fill_cheap_windows=False)
    plan, starts, load, pv = plan_for(start, heat_kw=0.0, pv_peak=9.0, soc=40, params=params)
    clipped = sum(c for t, c in plan.clipped if t.astimezone(LON).date() == start.date())
    lost, _ = _replay_lost(plan, starts, load, pv, 40, params)
    assert clipped > 1 and clipped == pytest.approx(lost, rel=0.05)


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
              params=pl.BatteryParams(fill_cheap_windows=False), charge_windows=COSY, discharge_windows=NIGHT)
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
                        params=pl.BatteryParams(fill_cheap_windows=False), charge_windows=COSY, discharge_windows=NIGHT)
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
                        params=pl.BatteryParams(fill_cheap_windows=False), charge_windows=COSY, discharge_windows=NIGHT)
    w = find(plan, "charge", datetime(2026, 10, 23, 13, tzinfo=LON))
    assert not w.enabled and "enough" in w.reason
    assert dict(plan.soc)[datetime(2026, 10, 23, 22, tzinfo=LON)] >= 20


def test_property_never_below_reserve_before_next_window():
    """Whatever the plan decides, replaying its charge slots with the cautious
    inputs must keep the battery at or above the reserve until each next cheap
    window, unless the plan itself says the charge rate can't cover it."""
    import random

    rng = random.Random(1234)
    for case in range(400):
        p = pl.BatteryParams(load_margin_pct=0, fill_cheap_windows=case % 2 == 0)
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
        # Before a day the solar fills the battery, the floor (not the
        # reserve) is the limit: the battery may run down as the solar arrives.
        spill = {w.start for w in plan.windows if "solar will fill" in w.reason}
        s, path = soc0, []
        for i, t in enumerate(starts):
            s = pl._step(s, acts.get(i), pv[i], load[i], 0.5, p)
            path.append((pl._later(t, timedelta(minutes=30)), s))
        # Check each gap from a window's start to the next window's start.
        for w0, w1 in zip(charge_starts, charge_starts[1:]):
            if w0 in shortish or w0 < starts[0]:
                continue
            low = min(v for t, v in path if w0 < t <= w1)
            start_soc = next((v for t, v in path if t == w0), soc0)
            if start_soc < p.reserve_soc:
                continue        # started the window below reserve already; can only recover
            limit = p.min_soc if w0 in spill else p.reserve_soc
            assert low >= limit - 1e-6, (case, start, w0, low)


def test_fills_the_late_window_instead_of_scraping_through():
    """Regression (23-24 Oct): at 21:50 the forecast said the battery would just
    stay above the reserve until 04:00, so the 22:00-00:00 slot was skipped. The
    house used more than forecast and the battery hit the floor at 02:30,
    buying at standard rate. Cheap windows now fill as far as solar allows."""
    start = datetime(2026, 10, 23, 21, 30, tzinfo=LON)
    starts = series(start, hours=30)
    load = [0.45 * 0.5 for _ in starts]        # forecast: 0.45 kW overnight
    pv = [bell(t, 1.5) * 0.5 for t in starts]  # dull Saturday
    kw = dict(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=32,
              charge_windows=COSY, discharge_windows=NIGHT)
    late = datetime(2026, 10, 23, 22, tzinfo=LON)
    old = find(pl.make_plan(params=pl.BatteryParams(fill_cheap_windows=False), **kw), "charge", late)
    assert not old.enabled                     # what happened last night
    w = find(pl.make_plan(params=pl.BatteryParams(), **kw), "charge", late)
    assert w.enabled and w.target_soc == 100, w.reason
    assert "fill" in w.reason


def test_fill_leaves_room_for_a_big_solar_day():
    start = datetime(2026, 6, 20, 21, 30, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=0.0, pv_peak=12.0, soc=30,
                        params=pl.BatteryParams(load_margin_pct=0), hours=30)
    w = find(plan, "charge", datetime(2026, 6, 20, 22, tzinfo=LON))
    assert w.target_soc < 100 and "room" in w.reason or not w.enabled, w.reason


def test_fill_skips_when_already_at_solar_limit():
    start = datetime(2026, 6, 20, 21, 30, tzinfo=LON)
    plan, *_ = plan_for(start, heat_kw=0.0, pv_peak=12.0, soc=95,
                        params=pl.BatteryParams(load_margin_pct=0), hours=30)
    w = find(plan, "charge", datetime(2026, 6, 20, 22, tzinfo=LON))
    assert not w.enabled, w.reason


def _sunday(peak):
    start = datetime(2026, 10, 24, 23, 30, tzinfo=LON)
    starts = series(start, hours=30)
    load = [0.6 * 0.5 for _ in starts]
    pv = [bell(t, peak, rise=8, fall=17) * 0.5 for t in starts]
    plan = pl.make_plan(starts=starts, period_minutes=30, load_kwh=load, pv_kwh=pv, soc_now=52,
                        params=pl.BatteryParams(), charge_windows=COSY, discharge_windows=NIGHT)
    return plan, starts, load, pv


def test_discharge_counts_what_the_house_uses_afterwards():
    """Regression (24 Oct): a big Sunday needed room, and the plan exported
    from 00:30 straight to the 15% floor by 02:00, leaving the house and heat
    pump on the grid until the solar arrived. The export now stops higher and
    the house's own use takes the battery down to the limit by window end."""
    plan, starts, load, pv = _sunday(12.0)
    d = find(plan, "discharge", datetime(2026, 10, 25, 0, 30, tzinfo=LON))
    assert d.enabled and d.target_soc > 15, d.reason
    assert "keeping" in d.reason
    # The battery may reach its minimum, but only once the solar covers the
    # house: no grid import for the house before then.
    # plan.soc[i] is the level at the end of period i; once at the minimum,
    # every following period must be covered by the solar.
    for i in range(1, len(plan.soc)):
        if plan.soc[i - 1][1] <= 15 + 1e-6 and starts[i].date() == datetime(2026, 10, 25).date():
            assert pv[i] >= load[i], (starts[i], plan.soc[i - 1][1])


def test_no_discharge_when_the_house_makes_the_room():
    plan, *_ = _sunday(9.5)
    d = find(plan, "discharge", datetime(2026, 10, 25, 0, 30, tzinfo=LON))
    assert not d.enabled and "on its own" in d.reason, d.reason


def test_room_at_credits_overnight_use():
    p = pl.BatteryParams()
    # 4 h of 1 kW load, then 4 h of 3 kW beyond house + export.
    pv = [0.0] * 8 + [(1 + 5 + 3) * 0.5] * 8
    load = [0.5] * 16
    assert pl._room_needed(pv, load, 0, 16, 0.5, p) == pytest.approx(12.0)
    assert pl._room_at(pv, load, 0, 16, 0.5, p) == pytest.approx(12.0 - 4.0, abs=0.15)   # within the clipping tolerance


def _replay(name):
    import json

    d = json.loads((pathlib.Path(__file__).parent / "data" / name).read_text())
    starts = [datetime.fromisoformat(t).astimezone(LON) for t in d["starts"]]
    ranges = [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in d["charge_ranges"]] \
        if d.get("charge_ranges") else None
    return d, starts, pl.make_plan(
        starts=starts, period_minutes=d["period_minutes"], load_kwh=d["load_kwh"], pv_kwh=d["pv_kwh"],
        pv_low_kwh=d["pv_low_kwh"], pv_high_kwh=d.get("pv_high_kwh"), soc_now=d["soc_now"],
        params=pl.BatteryParams(**d["params"]),
        charge_windows=pl.parse_windows(d["cheap_windows"]),
        discharge_windows=pl.parse_windows(d["discharge_windows"]), charge_ranges=ranges)


def test_real_plan_across_the_clocks_going_back():
    """Regression (24-25 Oct, real data): Sunday has 25 hours. Wall-clock
    arithmetic put every Sunday window an hour early against the solar and
    load, and the projected SOC repeated 01:30 and 02:00."""
    d, starts, plan = _replay("plan_2026-10-24_dst.json")
    times = [t.astimezone(timezone.utc) for t, _ in plan.soc]
    assert all(a < b for a, b in zip(times, times[1:]))          # real time, no repeats
    assert len(set(t.isoformat() for t in times)) == len(times)
    # Sunday's 04:00-07:00 (GMT) window acts from 04:00, not 03:00: the
    # battery is still draining over 03:00-04:00.
    soc = {t.astimezone(timezone.utc): v for t, v in plan.soc}
    utc = lambda h, m=0: datetime(2026, 10, 25, h, m, tzinfo=timezone.utc)
    assert soc[utc(3, 30)] < soc[utc(3, 0)] and soc[utc(4, 0)] < soc[utc(3, 30)]
    # Never below the reserve, and no export to the floor overnight.
    assert min(v for _, v in plan.soc) >= d["params"]["reserve_soc"] - 0.5
    sun = find(plan, "discharge", datetime(2026, 10, 25, 0, 30, tzinfo=LON))
    assert not sun.enabled, sun.reason


def test_charge_window_uses_solar_before_grid():
    """A Solis timed charge takes spare solar first; the grid only tops up."""
    p = pl.BatteryParams()
    # 3 kWh spare solar this half-hour, target 100 from 50%: solar covers 3 kWh,
    # the grid the rest up to the 2.5 kWh charge rate.
    solar, grid = pl._charge_split(50, 100, 3.3, 0.3, 0.5, p)
    assert solar == pytest.approx(3.0) and grid == 0.0
    solar, grid = pl._charge_split(50, 100, 1.3, 0.3, 0.5, p)
    assert solar == pytest.approx(1.0) and grid == pytest.approx(1.5)
    # Already at target: the grid adds nothing, the solar still goes in.
    solar, grid = pl._charge_split(80, 60, 1.3, 0.3, 0.5, p)
    assert solar == pytest.approx(1.0) and grid == 0.0


def test_real_spill_day_buys_only_what_the_house_needs():
    """Regression (24 Oct evening, real data): Solcast's median fills the
    battery by 12:30 on Sunday and clips. 0.3.11 charged to 45% at 22:00 and
    held 30% from 04:00 to keep the 25% reserve: power the solar would have
    replaced for free. Now the windows only stop the battery reaching 15%
    before the solar takes over, and the 13:00 window buys nothing while
    solar is spilling."""
    d, starts, plan = _replay("plan_2026-10-24_evening.json")
    p = pl.BatteryParams(**d["params"])
    sat = find(plan, "charge", datetime(2026, 10, 24, 22, tzinfo=LON))
    assert sat.target_soc <= math.ceil(sat.projected_soc), sat.reason      # no charge above now
    sun = find(plan, "charge", datetime(2026, 10, 25, 4, tzinfo=timezone.utc))
    assert sun.target_soc < p.reserve_soc, sun.reason
    assert "until the solar takes over" in sun.reason
    # Expected path never touches the minimum.
    assert min(v for _, v in plan.soc) > p.min_soc + 1
    # Grid bought in the 13:00 window: none, the solar fills it.
    aft = find(plan, "charge", datetime(2026, 10, 25, 13, tzinfo=timezone.utc))
    soc = d["soc_now"]
    grid = 0.0
    socs = [d["soc_now"]] + [v for _, v in plan.soc]
    for i, t in enumerate(starts):
        if aft.enabled and aft.start <= t < aft.end:
            grid += pl._charge_split(socs[i], aft.target_soc, d["pv_kwh"][i], d["load_kwh"][i], 0.5, p)[1]
    assert grid < 0.1, grid


def test_real_plan_with_learned_baseload():
    """24 Oct 22:09, real data with the learned baseload (~0.1-0.15 kW at
    night): on a day the solar will spill, tonight's window doesn't buy, the
    battery runs the house, and nothing reaches the minimum."""
    d, starts, plan = _replay("plan_2026-10-24_learned.json")
    sat = find(plan, "charge", datetime(2026, 10, 24, 22, tzinfo=LON))
    assert not sat.enabled, sat.reason
    assert min(v for _, v in plan.soc) > d["params"]["min_soc"] + 1


def test_real_no_grid_charge_while_exporting():
    """Regression (26 Oct, real data): Tuesday's plan had Feed-in priority
    until 15:30 *and* a 13:00 charge window to 100%, so the inverter would
    export solar while buying from the grid. The solar fills the battery on
    its own: the window is off, charge windows are always Self-Use, and the
    mode is only Feed-in priority when there's spare solar."""
    d, starts, plan = _replay("plan_2026-10-26_feedin.json")
    aft = find(plan, "charge", datetime(2026, 10, 27, 13, tzinfo=timezone.utc))
    assert not aft.enabled and ("on its own" in aft.reason or "solar will fill" in aft.reason), aft.reason
    for i, (t, m) in enumerate(plan.modes):
        if m != "Self-Use":
            assert d["pv_kwh"][i] > d["load_kwh"][i], (t, m)                 # no feed-in at night
        for w in plan.windows:
            if w.kind == "charge" and w.enabled and w.start <= t < w.end:
                assert m == "Self-Use", (t, m)


def test_real_44kwh_day_stores_before_feeding_in():
    """Regression (27 Oct, real data): 44% at 10:30, ~44 kWh of solar to come,
    ~18 kWh of room. The old rule chose Feed-in priority from 10:30 because the
    surplus wouldn't fit, exporting solar the battery had room for. Only the
    surplus above the 5 kW export limit (~11 kWh) needs room, so the plan now
    stores first, feeds in through the peak, and still clips nothing."""
    d, starts, plan = _replay("plan_2026-10-27_morning.json")
    p = pl.BatteryParams(**d["params"])
    modes = [m for _, m in plan.modes]
    assert modes[0] == "Self-Use"                                       # 10:30: store
    assert "Feed-in priority" in modes[:12]                             # through the peak
    lost, path = _replay_lost(plan, starts, d["load_kwh"], d["pv_kwh"], d["soc_now"], p, periods=13)
    assert lost < 0.2, lost
    assert sum(c for _, c in plan.clipped[:13]) < 0.2
    # Ahead of the old plan until the peak (by about 12% at 12:00, 3.8 kWh),
    # behind at most by the headroom kept for its end, and full by 15:30 as before.
    old_soc = [v for _, v in d["plan"]["soc"]]
    new_soc = [v for _, v in plan.soc]
    hhmm = [t.astimezone(LON).strftime("%H:%M") for t in starts]
    noon, full = hhmm.index("12:00"), hhmm.index("15:30")
    assert all(a >= b - 0.5 for a, b in zip(new_soc[:hhmm.index("14:00")], old_soc))
    assert new_soc[noon] > old_soc[noon] + 10
    margin = p.solar_headroom_kwh / p.capacity_kwh * 100 + 0.5
    assert all(a >= b - margin for a, b in zip(new_soc[:full], old_soc[:full]))
    assert new_soc[full] >= 99.5
    # The 13:00 window still isn't needed: the solar fills the battery.
    aft = find(plan, "charge", datetime(2026, 10, 27, 13, tzinfo=timezone.utc))
    assert not aft.enabled, aft.reason


def test_real_dont_buy_what_the_solar_will_supply():
    """Regression (29 Oct, real data): the 04:00 window bought 83% -> 89%,
    then the morning was Feed-in priority and the solar filled the battery by
    13:00 anyway, so the grid power only displaced exported solar. Windows
    before a day the solar fills the battery now buy nothing beyond carrying
    the house to the solar, and a night window buys only what the next day's
    solar can't top up."""
    d, starts, plan = _replay("plan_2026-10-29_nobuy.json")
    utc = lambda day, h: datetime(2026, 10, day, h, tzinfo=timezone.utc)
    assert not find(plan, "charge", utc(29, 4)).enabled
    assert not find(plan, "charge", utc(29, 13)).enabled
    night = find(plan, "charge", utc(29, 22))
    assert night.target_soc < 100, night.reason
    assert min(v for _, v in plan.soc) > d["params"]["reserve_soc"]


def test_real_self_use_morning_no_afternoon_buy():
    """Regression (29 Oct 02:19, real data): room for the peak sized on the
    optimistic forecast kept Feed-in priority on from 08:30, the battery sat at
    72% all morning, and the 13:00 window bought it back from the grid. Sized
    on the expected forecast: Self-Use through the morning, Feed-in priority
    only around the peak, no grid charge at 13:00, nothing clipped."""
    d, starts, plan = _replay("plan_2026-10-29_feedin_then_buy.json")      # no pv_high: expected
    aft = find(plan, "charge", datetime(2026, 10, 29, 13, tzinfo=timezone.utc))
    assert not aft.enabled, aft.reason
    feed = [t.astimezone(LON) for t, m in plan.modes
            if m != "Self-Use" and t.astimezone(LON).day == 29]
    assert feed and min(feed).hour >= 10 and max(feed).hour <= 13, feed
    soc = {t.astimezone(LON).strftime("%H:%M"): v for t, v in plan.soc if t.astimezone(LON).day == 29}
    assert soc["11:30"] > soc["08:30"] + 5           # the morning solar is stored
