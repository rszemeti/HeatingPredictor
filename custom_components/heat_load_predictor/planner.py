"""Battery planner, free of Home Assistant imports so it can be tested alone.

Given per-period forecasts of house load (heat pump + baseload) and solar, the
battery state of charge now, and a set of daily windows, decide for each
upcoming window:

* Cheap (charge) windows: charge from the grid to the lowest SOC that still
  reaches the next cheap window at the reserve SOC. The shortfall is the worst
  point of the running balance (solar in, load out) across the gap.
* Discharge windows: discharge to make room for solar the inverter can neither
  use nor export. Room needed is the peak of the running balance of solar
  beyond (load + export limit), less what the house draws in between, plus a
  fixed headroom. If the resulting target is above the threshold, discharging
  isn't worth it and the window is skipped.
* Solar room wins: a charge window is capped at the room the solar needs, and a
  charge window that overlaps an active discharge window is switched off.

Windows are processed in time order with a forward SOC simulation, so each
decision knows the SOC the battery is projected to have when the window opens.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

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


def make_plan(
    *,
    starts: Sequence[datetime],
    period_minutes: int,
    load_kwh: Sequence[float],
    pv_kwh: Sequence[float],
    soc_now: float,
    params: BatteryParams,
    charge_windows: Sequence[tuple[time, time]],
    discharge_windows: Sequence[tuple[time, time]],
) -> Plan:
    """Plan charge and discharge windows across the forecast periods.

    `starts` are timezone-aware local period starts; `load_kwh` and `pv_kwh`
    hold each period's energy.
    """
    n = len(starts)
    if n == 0:
        return Plan()
    dt = timedelta(minutes=period_minutes)
    dt_h = period_minutes / 60.0
    cap = params.capacity_kwh
    horizon_end = starts[-1] + dt

    occ = occurrences(discharge_windows, "discharge", starts[0], horizon_end)
    occ += occurrences(charge_windows, "charge", starts[0], horizon_end)
    occ.sort(key=lambda o: (o.start, 0 if o.kind == "discharge" else 1))
    charge_occ = [o for o in occ if o.kind == "charge"]

    day_periods = int(24 * 60 / period_minutes)

    def idx(when: datetime) -> int:
        """Index of the first period starting at or after `when` (clamped)."""
        k = math.ceil((when - starts[0]) / dt)
        return min(max(k, 0), n)

    # Fill in decisions in time order. `actions[i]` is the SOC target to drive
    # towards in period i ("charge"/"discharge"), or None for self-use.
    actions: list[tuple[str, float] | None] = [None] * n
    plans: list[WindowPlan] = []

    # Solar still to come today from each period on, for the feed-in rule.
    pv_left = [0.0] * n
    for i in range(n - 1, -1, -1):
        same_day = i + 1 < n and starts[i + 1].date() == starts[i].date()
        pv_left[i] = pv_kwh[i] + (pv_left[i + 1] if same_day else 0.0)

    def simulate(upto: int) -> list[float]:
        """SOC at the start of each period 0..upto (inclusive)."""
        soc = [soc_now]
        s = soc_now
        for i in range(upto):
            feed_in = pv_left[i] > (100 - s) / 100 * cap
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
            room = _room_needed(pv_kwh, load_kwh, b, b + day_periods, dt_h, params)
            raw = 100 - (room + params.solar_headroom_kwh) / cap * 100
            target = int(max(params.min_soc, 5 * round(raw / 5)))
            if target > params.discharge_threshold_soc:
                enabled, reason = False, (
                    f"not worth it: room for {room:.1f} kWh solar only needs "
                    f"{target}%")
            elif projected <= target + 0.5:
                enabled, reason = False, f"battery already at or below {target}%"
            else:
                enabled, reason = True, f"make room for {room:.1f} kWh of solar"
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
        need = _worst_shortfall(pv_kwh, load_kwh, b, gap_end)
        lower = params.reserve_soc + need / cap * 100
        room = _room_needed(pv_kwh, load_kwh, b, b + day_periods, dt_h, params)
        upper = 100 - (room + (params.solar_headroom_kwh if room > 0 else 0)) / cap * 100
        target = int(min(100, max(params.min_soc, math.ceil(min(lower, upper)))))
        overlaps = any(s < o.end and o.start < e for s, e in discharging)
        if overlaps:
            enabled, reason = False, "overnight discharge is making room for solar"
        elif projected >= target - 0.5:
            enabled, reason = False, f"battery expected at {projected:.0f}%, enough"
        else:
            enabled = True
            reason = f"cover {need:.1f} kWh until the next cheap window"
            if upper < lower:
                reason += f"; capped at {target}% to leave room for solar"
        if nxt is None:
            reason += " (next window beyond forecast)"
        plans.append(WindowPlan(o.kind, o.slot, o.start, o.end, enabled, target,
                                projected, need, room, reason))
        if enabled:
            for i in range(a, b):
                actions[i] = ("charge", target)

    soc = simulate(n)
    return Plan(windows=plans, soc=[(starts[i] + dt, soc[i + 1]) for i in range(n)])


def _step(
    soc: float, action, pv: float, load: float, dt_h: float, p: BatteryParams,
    feed_in: bool = False,
) -> float:
    """Advance SOC by one period.

    `feed_in` mirrors the usual storage-mode rule: when today's remaining solar
    won't fit in the battery, export comes first (up to the export limit) and
    only the excess charges the battery.
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


def _worst_shortfall(pv, load, a: int, b: int) -> float:
    """Deepest dip of the running balance (solar - load) over periods [a, b)."""
    bal = low = 0.0
    for i in range(a, b):
        bal += pv[i] - load[i]
        low = min(low, bal)
    return -low


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
