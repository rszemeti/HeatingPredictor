"""Battery planner, free of Home Assistant imports so it can be tested alone.

Given per-period forecasts of house load (heat pump + baseload) and solar, the
battery state of charge now, and a set of daily windows, decide for each
upcoming window:

* Cheap (charge) windows: fill as far as the next day's solar allows (or, if
  fill_cheap_windows is off, to the lowest SOC that still reaches the next
  cheap window at the reserve SOC, found by simulation).
* Discharge windows: make room for solar the inverter can neither use nor
  export. The limit is the highest SOC at window end from which no solar is
  lost, counting what the house draws before the surplus arrives, less a
  fixed headroom. The export stops at the highest target from which the
  house's own use still brings the battery to that limit by window end. If
  the house gets there on its own, or the target is above the threshold, the
  window is skipped.
* Solar room wins: a charge window is capped at the room the solar needs, and a
  charge window that overlaps an active discharge window is switched off.

Windows are processed in time order with a forward SOC simulation, so each
decision knows the SOC the battery is projected to have when the window opens.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace, dataclass, field
from datetime import datetime, time, timedelta, timezone

# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


def parse_windows(text: str) -> list[tuple[time, time]]:
    """Parse "04:00-07:00, 13:00-16:00, 22:00-00:00" into (start, end) times.

    An end at or before the start runs past midnight. Empty text gives [].
    """
    windows: list[tuple[time, time]] = []
    for chunk in (text or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise ValueError(f"'{chunk}' should look like 04:00-07:00")
        a, b = (part.strip() for part in chunk.split("-", 1))
        start, end = _hhmm(a), _hhmm(b)
        if start == end:
            raise ValueError(f"window '{chunk}' has no length")
        windows.append((start, end))
    return windows


def format_windows(windows: Sequence[tuple[time, time]]) -> str:
    return ", ".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in windows)


def _hhmm(text: str) -> time:
    h, m = text.split(":")
    h, m = int(h), int(m)
    if h == 24 and m == 0:
        return time(0, 0)
    return time(h, m)


@dataclass
class Occurrence:
    kind: str           # "charge" or "discharge"
    slot: int           # 1-based, in the order the windows were configured
    start: datetime
    end: datetime


def _later(t: datetime, d: timedelta) -> datetime:
    """`t` plus real elapsed time `d`, in t's zone (correct across clock changes)."""
    return (t.astimezone(timezone.utc) + d).astimezone(t.tzinfo)


def occurrences(
    windows: Sequence[tuple[time, time]], kind: str, first: datetime, last: datetime
) -> list[Occurrence]:
    """Every occurrence of the daily windows that overlaps [first, last).

    `first` and `last` must be timezone-aware local datetimes; occurrences are
    built in that zone, so DST changes keep wall-clock times.
    """
    tz = first.tzinfo
    out: list[Occurrence] = []
    day = first.date() - timedelta(days=1)
    while day <= last.date():
        for i, (s, e) in enumerate(windows, start=1):
            start = datetime.combine(day, s, tz)
            end = datetime.combine(day, e, tz)
            if end <= start:
                end += timedelta(days=1)
            if end > first and start < last:
                out.append(Occurrence(kind, i, start, end))
        day += timedelta(days=1)
    out.sort(key=lambda o: o.start)
    return out


def ranges_to_occurrences(
    ranges: Sequence[tuple[datetime, datetime]], kind: str, first: datetime, last: datetime
) -> list[Occurrence]:
    """Explicit (start, end) ranges as occurrences, numbered per local day.

    The first range starting on a given day is slot 1, the second slot 2, and
    so on, so a tariff's daily windows map onto the same inverter slots each day.
    """
    out: list[Occurrence] = []
    count: dict = {}
    for start, end in sorted(ranges):
        day = start.date()
        count[day] = count.get(day, 0) + 1
        if end > first and start < last:
            out.append(Occurrence(kind, count[day], start, end))
    return out


def cheap_ranges(
    rates: Sequence[tuple[datetime, datetime, float]], tolerance_pct: float = 10.0
) -> list[tuple[datetime, datetime]]:
    """Cheapest band of each day from (start, end, price) rate periods.

    A period is cheap when its price is within `tolerance_pct` of that day's
    lowest price (days are the local dates of the period starts). Adjacent
    cheap periods are merged into one window.
    """
    by_day: dict = {}
    for r in rates:
        by_day.setdefault(r[0].date(), []).append(r)
    cheap: list[tuple[datetime, datetime]] = []
    for day_rates in by_day.values():
        lowest = min(p for _, _, p in day_rates)
        limit = lowest + abs(lowest) * tolerance_pct / 100
        cheap += [(s, e) for s, e, p in day_rates if p <= limit + 1e-9]
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(cheap):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass
class BatteryParams:
    capacity_kwh: float = 32.0
    min_soc: float = 15.0
    reserve_soc: float = 20.0
    export_limit_kw: float = 5.0
    charge_power_kw: float = 5.0
    discharge_power_kw: float = 5.0
    discharge_threshold_soc: float = 80.0
    solar_headroom_kwh: float = 2.6
    load_margin_pct: float = 10.0   # charge sizing: assume load this much higher
    # True: fill every cheap window as far as the next day's solar allows.
    # False: charge only what the forecast says is needed to reach the next one.
    fill_cheap_windows: bool = True

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0:
            raise ValueError("battery capacity must be above zero")
        if not 0 <= self.min_soc < 100:
            raise ValueError("minimum SOC must be 0-99 %")


@dataclass
class WindowPlan:
    kind: str
    slot: int
    start: datetime
    end: datetime
    enabled: bool
    target_soc: int
    projected_soc: float     # SOC expected when the window opens
    need_kwh: float          # charge: worst shortfall across the following gap
    room_kwh: float          # solar energy that must fit in the battery
    reason: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "slot": self.slot,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "enabled": self.enabled,
            "target_soc": self.target_soc,
            "projected_soc": round(self.projected_soc, 1),
            "need_kwh": round(self.need_kwh, 2),
            "room_kwh": round(self.room_kwh, 2),
            "reason": self.reason,
        }


@dataclass
class Plan:
    windows: list[WindowPlan] = field(default_factory=list)
    soc: list[tuple[datetime, float]] = field(default_factory=list)  # at each period end
    # Recommended storage mode for each period: "Self-Use" from the earliest
    # point at which the battery can take all spare solar without clipping,
    # "Feed-in priority" (export first) before that.
    modes: list[tuple[datetime, str]] = field(default_factory=list)


def make_plan(
    *,
    starts: Sequence[datetime],
    period_minutes: int,
    load_kwh: Sequence[float],
    pv_kwh: Sequence[float],
    pv_low_kwh: Sequence[float] | None = None,
    soc_now: float,
    params: BatteryParams,
    charge_windows: Sequence[tuple[time, time]],
    discharge_windows: Sequence[tuple[time, time]],
    charge_ranges: Sequence[tuple[datetime, datetime]] | None = None,
) -> Plan:
    """Plan charge and discharge windows across the forecast periods.

    `starts` are timezone-aware local period starts; `load_kwh` and `pv_kwh`
    hold each period's expected energy. Charges are sized cautiously: against
    `pv_low_kwh` (a pessimistic solar forecast, if given) and the load raised
    by `params.load_margin_pct`. The projected SOC and the solar room use the
    expected figures, so the projection shows the likely path and the gap
    above the reserve is the safety buffer.
    """
    n = len(starts)
    if n == 0:
        return Plan()
    dt = timedelta(minutes=period_minutes)
    dt_h = period_minutes / 60.0
    cap = params.capacity_kwh
    horizon_end = _later(starts[-1], dt)

    occ = occurrences(discharge_windows, "discharge", starts[0], horizon_end)
    if charge_ranges is not None:
        occ += ranges_to_occurrences(charge_ranges, "charge", starts[0], horizon_end)
    else:
        occ += occurrences(charge_windows, "charge", starts[0], horizon_end)
    occ.sort(key=lambda o: (o.start, 0 if o.kind == "discharge" else 1))
    charge_occ = [o for o in occ if o.kind == "charge"]

    day_periods = int(24 * 60 / period_minutes)
    pv_cautious = list(pv_low_kwh) if pv_low_kwh is not None else list(pv_kwh)
    load_cautious = [x * (1 + params.load_margin_pct / 100) for x in load_kwh]

    def idx(when: datetime) -> int:
        """Index of the first period starting at or after `when` (clamped)."""
        # Real elapsed time (UTC): local wall-clock arithmetic is wrong on the
        # days the clocks change.
        k = math.ceil((when.astimezone(timezone.utc) - starts[0].astimezone(timezone.utc)) / dt)
        return min(max(k, 0), n)

    # Fill in decisions in time order. `actions[i]` is the SOC target to drive
    # towards in period i ("charge"/"discharge"), or None for self-use.
    actions: list[tuple[str, float] | None] = [None] * n
    plans: list[WindowPlan] = []

    # End (exclusive) of each period's local day, for the storage-mode rule.
    day_end = [n] * n
    for i in range(n - 2, -1, -1):
        day_end[i] = day_end[i + 1] if starts[i + 1].date() == starts[i].date() else i + 1

    def self_use_safe(i: int, soc: float) -> bool:
        """Can the battery take all spare solar from period i without clipping?"""
        return _self_use_clipping(
            pv_kwh, load_kwh, i, day_end[i], soc, dt_h, params
        ) < CLIP_TOLERANCE_KWH

    modes: list[str] = ["Self-Use"] * n

    def simulate(upto: int) -> list[float]:
        """SOC at the start of each period 0..upto (inclusive)."""
        soc = [soc_now]
        s = soc_now
        for i in range(upto):
            feed_in = not self_use_safe(i, s)
            modes[i] = "Feed-in priority" if feed_in else "Self-Use"
            s = _step(s, actions[i], pv_kwh[i], load_kwh[i], dt_h, params, feed_in)
            soc.append(s)
        return soc

    discharging: list[tuple[datetime, datetime]] = []

    for o in occ:
        a, b = idx(o.start), idx(o.end)
        if a >= n:
            continue
        projected = simulate(max(a, 0))[-1]

        if o.kind == "discharge":
            room = _room_at(pv_kwh, load_kwh, b, b + day_periods, dt_h, params)
            # Highest SOC the battery may hold when the window ends.
            limit = max(params.min_soc, 100 - (room + params.solar_headroom_kwh) / cap * 100)
            # Never make room by running short: the battery must still carry the
            # house until the solar takes over (or the next cheap window),
            # reaching its minimum no earlier than that, judged on the cautious
            # forecasts. Losing some solar costs less than buying at full price.
            nxt_c = next((c for c in charge_occ if c.start >= o.end), None)
            cover_end = idx(nxt_c.start) if nxt_c else n
            keep = _keep_soc(pv_cautious, load_cautious, b, cover_end, dt_h, params)
            protected = keep > limit
            if protected:
                limit = keep

            def soc_at_end(t: float | None) -> float:
                s_ = projected
                for i in range(max(a, 0), b):
                    s_ = _step(s_, ("discharge", t) if t is not None else None,
                               pv_kwh[i], load_kwh[i], dt_h, params)
                return s_

            # The export stops at the target and the house keeps drawing the
            # battery down after that, so pick the highest target that still
            # ends the window at the limit: export only what the house won't use.
            target = int(math.floor(limit))
            lo_t, hi_t = target, max(target, int(math.ceil(projected)))
            while lo_t < hi_t:
                mid = (lo_t + hi_t + 1) // 2
                if soc_at_end(mid) <= limit + 1e-6:
                    lo_t = mid
                else:
                    hi_t = mid - 1
            target = int(max(params.min_soc, lo_t))
            if room <= 0:
                enabled, reason = False, "not worth it: no solar beyond what the house and export can take"
            elif soc_at_end(None) <= limit + 0.5:
                enabled, reason = False, (
                    f"the house will bring the battery down to {limit:.0f}% by "
                    f"{o.end.strftime('%H:%M')} on its own")
            elif target > params.discharge_threshold_soc:
                enabled, reason = False, (
                    f"not worth it: room for {room:.1f} kWh solar only needs "
                    f"{target}%")
            else:
                enabled, reason = True, (
                    f"export down to {target}% to make room for {room:.1f} kWh of "
                    f"solar; the house takes it to {limit:.0f}% by {o.end.strftime('%H:%M')}")
            if protected:
                reason += (f" (keeping {keep:.0f}% so the house runs on the battery until the "
                           f"solar takes over; some solar may be clipped)")
            plans.append(WindowPlan(o.kind, o.slot, o.start, o.end, enabled, target,
                                    projected, 0.0, room, reason))
            if enabled:
                discharging.append((o.start, o.end))
                for i in range(a, b):
                    actions[i] = ("discharge", target)
            continue

        # Charge window: cover the gap to the next cheap window's start.
        nxt = next((c for c in charge_occ if c.start >= o.end), None)
        gap_end = idx(nxt.start) if nxt else n
        # Decide by simulation, period by period, with the cautious solar and
        # demand: what is the lowest SOC between this window's start and the
        # next cheap window, if we skip the slot or charge to a given target?
        gap_start_soc = projected

        def lowest(target: float | None) -> float:
            # Lowest SOC reached during the window and gap. The level the window
            # opens at doesn't count: no decision now can change it.
            s_ = gap_start_soc
            low = math.inf
            for i in range(a, gap_end):
                act = ("charge", target) if target is not None and i < b else None
                s_ = _step(s_, act, pv_cautious[i], load_cautious[i], dt_h, params)
                low = min(low, s_)
            return low

        need = _worst_shortfall(pv_cautious, load_cautious, b, gap_end)
        room = _room_at(pv_kwh, load_kwh, b, b + day_periods, dt_h, params)
        upper = 100 - (room + (params.solar_headroom_kwh if room > 0 else 0)) / cap * 100
        overlaps = any(s < o.end and o.start < e for s, e in discharging)
        skip_low = lowest(None)
        fill_to = int(max(params.min_soc, min(100, math.ceil(upper))))
        if overlaps:
            target = fill_to
            enabled, reason = False, "overnight discharge is making room for solar"
        elif params.fill_cheap_windows:
            # Cheap power is the cheapest there is: fill as far as tomorrow's
            # solar allows, rather than gambling on just scraping through.
            just_enough = _lowest_target(lowest, projected, params)
            if just_enough is not None and just_enough > fill_to:
                # Solar room never wins over staying off the grid at full price.
                target, enabled = just_enough, True
                reason = (f"charge to {target}%, above the {fill_to}% solar limit: needed "
                          f"to stay above {params.reserve_soc:.0f}% until the next cheap "
                          f"window (some solar may be clipped)")
                if lowest(target) < params.reserve_soc - 1e-6:
                    reason += " (charge rate too low to cover it all)"
            elif fill_to > projected + 0.5:
                target, enabled = fill_to, True
                reason = f"fill to {target}% on cheap power"
                if fill_to < 100:
                    reason += f" (the rest is room for {room:.1f} kWh of solar)"
                if lowest(target) < params.reserve_soc - 1e-6:
                    reason += " (charge rate too low to cover it all)"
            elif just_enough is not None:
                # Already at the solar limit but still short: run the house on
                # cheap grid power during the window and keep what's stored.
                target, enabled = int(max(params.min_soc, math.ceil(projected))), True
                reason = (f"hold at {target}%: run the house on cheap power now, "
                          f"keep the battery for {need:.1f} kWh after the window")
            else:
                target, enabled = fill_to, False
                reason = (f"battery expected at {projected:.0f}%, already as full as "
                          f"tomorrow's solar allows")
        elif skip_low >= params.reserve_soc - 1e-6:
            target = int(max(params.min_soc, math.ceil(projected)))
            enabled = False
            reason = (f"battery expected at {projected:.0f}%, enough to stay above "
                      f"{params.reserve_soc:.0f}% until the next cheap window")
        else:
            # Lowest whole-% target that keeps the battery above the reserve.
            lo_t, hi_t = max(int(math.ceil(projected)), int(params.min_soc)), 100
            if lowest(hi_t) < params.reserve_soc - 1e-6:
                target = hi_t
                short = True
            else:
                while lo_t < hi_t:
                    mid = (lo_t + hi_t) // 2
                    if lowest(mid) >= params.reserve_soc - 1e-6:
                        hi_t = mid
                    else:
                        lo_t = mid + 1
                target, short = hi_t, False
            # Solar room never caps a charge below what's needed.
            capped = False
            enabled = True
            if target > projected + 0.5:
                reason = f"charge to {target}% to cover {need:.1f} kWh until the next cheap window"
            else:
                reason = (f"hold at {target}%: run the house on cheap power now, "
                          f"keep the battery for {need:.1f} kWh after the window")
            if short:
                reason += " (charge rate too low to cover it all)"
            if capped:
                reason += f"; capped at {target}% to leave room for solar"
        if nxt is None:
            reason += " (next window beyond forecast)"
        plans.append(WindowPlan(o.kind, o.slot, o.start, o.end, enabled, target,
                                projected, need, room, reason))
        if enabled:
            for i in range(a, b):
                actions[i] = ("charge", target)

    soc = simulate(n)
    return Plan(
        windows=plans,
        soc=[(_later(starts[i], dt), soc[i + 1]) for i in range(n)],
        modes=list(zip(starts, modes)),
    )


def _keep_soc(pv, load, a: int, b: int, dt_h: float, p: BatteryParams) -> float:
    """Lowest SOC at period a from which self-use carries the house through
    [a, b) without the battery going below its minimum: it may reach the
    minimum, but only as the solar (or the next cheap window) takes over."""
    free = replace(p, min_soc=0.0)          # let the simulation go below the floor

    def low(s0: float) -> float:
        s_, m = s0, s0
        for i in range(a, min(b, len(pv))):
            s_ = _step(s_, None, pv[i], load[i], dt_h, free)
            m = min(m, s_)
        return m

    if low(p.min_soc) >= p.min_soc - 1e-6:
        return p.min_soc
    lo, hi = p.min_soc, 100.0
    if low(hi) < p.min_soc - 1e-6:
        return 100.0
    for _ in range(30):
        mid = (lo + hi) / 2
        if low(mid) >= p.min_soc - 1e-6:
            hi = mid
        else:
            lo = mid
    return hi


def _lowest_target(lowest, projected: float, p: BatteryParams) -> int | None:
    """Smallest whole-% charge target that keeps the battery above the reserve
    until the next cheap window, or None if skipping the window already does."""
    if lowest(None) >= p.reserve_soc - 1e-6:
        return None
    lo, hi = max(int(math.ceil(projected)), int(p.min_soc)), 100
    if lowest(hi) < p.reserve_soc - 1e-6:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        if lowest(mid) >= p.reserve_soc - 1e-6:
            hi = mid
        else:
            lo = mid + 1
    return hi


def _step(
    soc: float, action, pv: float, load: float, dt_h: float, p: BatteryParams,
    feed_in: bool = False,
) -> float:
    """Advance SOC by one period.

    `feed_in` is Feed-in priority: export comes first (up to the export limit)
    and only the excess charges the battery.
    """
    cap = p.capacity_kwh
    if action and action[0] == "charge":
        # Timed charge slot: the house runs from the grid for the whole window,
        # the battery charges to target and then holds; spare solar still goes in.
        if soc < action[1]:
            add = min(p.charge_power_kw * dt_h, (action[1] - soc) / 100 * cap)
            soc += add / cap * 100
        spare = max(0.0, pv - load)
        return min(100.0, soc + spare / cap * 100)
    if action and action[0] == "discharge" and soc > action[1]:
        take = min(p.discharge_power_kw * dt_h, (soc - action[1]) / 100 * cap)
        return soc - take / cap * 100
    # Self-use: surplus solar charges the battery, shortfalls drain it to min SOC.
    net = pv - load
    if net >= 0:
        if feed_in:
            net = max(0.0, net - p.export_limit_kw * dt_h)
        return min(100.0, soc + net / cap * 100)
    return max(p.min_soc, soc + net / cap * 100) if soc > p.min_soc else soc


CLIP_TOLERANCE_KWH = 0.1


def _self_use_clipping(
    pv, load, a: int, b: int, soc: float, dt_h: float, p: BatteryParams
) -> float:
    """Solar lost over [a, b) if the battery takes all spare solar from a.

    In Self-Use the battery takes every spare kWh until it's full; after that
    spare solar is exported, and anything above the export limit is lost.
    Shortfalls draw the battery down (to its minimum), making room again.
    """
    cap = p.capacity_kwh
    stored = soc / 100 * cap
    floor = p.min_soc / 100 * cap
    lost = 0.0
    for i in range(a, min(b, len(pv))):
        spare = pv[i] - load[i]
        if spare >= 0:
            take = min(spare, cap - stored)
            stored += take
            lost += max(0.0, spare - take - p.export_limit_kw * dt_h)
        else:
            stored = max(floor, stored + spare) if stored > floor else stored
    return lost


def _worst_shortfall(pv, load, a: int, b: int) -> float:
    """Deepest dip of the running balance (solar - load) over periods [a, b)."""
    bal = low = 0.0
    for i in range(a, b):
        bal += pv[i] - load[i]
        low = min(low, bal)
    return -low


def _room_at(pv, load, a: int, b: int, dt_h: float, p: BatteryParams) -> float:
    """Free space (kWh) the battery needs at the start of period a.

    The highest SOC at a from which no solar is lost over [a, b), with export
    first up to the limit and the battery taking only the excess, while the
    house draws the battery down whenever solar falls short. Unlike
    `_room_needed`, what the house uses before the solar arrives counts: a
    battery left at 60% at 05:30 may be at 50% by the time there's spare solar.
    """
    cap = p.capacity_kwh
    floor = p.min_soc / 100 * cap
    lim = p.export_limit_kw * dt_h

    def lost(stored: float) -> float:
        out = 0.0
        for i in range(a, min(b, len(pv))):
            surplus = pv[i] - load[i]
            if surplus > lim:
                stored += surplus - lim
                if stored > cap:
                    out += stored - cap
                    stored = cap
            elif surplus < 0 and stored > floor:
                stored = max(floor, stored + surplus)
        return out

    if lost(cap) < CLIP_TOLERANCE_KWH:
        return 0.0
    lo, hi = 0.0, cap                 # lost(lo) small, lost(hi) not
    if lost(lo) >= CLIP_TOLERANCE_KWH:
        return cap
    for _ in range(40):
        mid = (lo + hi) / 2
        if lost(mid) < CLIP_TOLERANCE_KWH:
            lo = mid
        else:
            hi = mid
    return cap - lo


def _room_needed(pv, load, a: int, b: int, dt_h: float, p: BatteryParams) -> float:
    """Peak energy the battery must absorb from solar beyond load + export limit.

    Running balance over [a, b): solar the house can't use or export goes in,
    and the house drawing more than the solar takes out. Capped at zero from
    below, since an empty battery can't go negative.
    """
    bal = peak = 0.0
    for i in range(a, min(b, len(pv))):
        surplus = pv[i] - load[i]
        if surplus > p.export_limit_kw * dt_h:
            bal += surplus - p.export_limit_kw * dt_h
        elif surplus < 0:
            bal = max(0.0, bal + surplus)
        peak = max(peak, bal)
    return peak
