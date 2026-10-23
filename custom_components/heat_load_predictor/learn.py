"""Learn the house's half-hourly baseload from history (no Home Assistant imports).

The measured house load includes the heat pump. Subtracting what the heat
model says the heat pump used, half-hour by half-hour, leaves the rest of the
house. Averaged by half-hour of the day, that is the baseload profile.

Because the profile is "measured minus modelled heating", modelled heating
plus the profile adds back up to what the house actually used: if the heat
model is off, the difference lands in the profile rather than being lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

SLOTS = 48  # half-hours in a day


def half_hour_means(
    samples: Sequence[tuple[datetime, float]], start: datetime, end: datetime
) -> dict[datetime, float]:
    """Mean of (time, kW) samples in each half-hour from `start` to `end`.

    `start` must be on a half-hour boundary. Half-hours with no samples are
    left out.
    """
    sums: dict[datetime, list[float]] = {}
    step = timedelta(minutes=30)
    for t, v in samples:
        if not start <= t < end:
            continue
        k = start + step * int((t - start) / step)
        acc = sums.setdefault(k, [0.0, 0.0])
        acc[0] += v
        acc[1] += 1
    return {k: s / n for k, (s, n) in sums.items() if n}


def slot_of(t: datetime) -> int:
    """Half-hour of the (local) day, 0..47."""
    return t.hour * 2 + (1 if t.minute >= 30 else 0)


def base_profile(
    house_kw: dict[datetime, float],
    heat_kw: dict[datetime, float],
    *,
    heat_pump_since: datetime | None = None,
    min_days: float = 2.0,
    floor_kw: float = 0.05,
) -> tuple[list[float], float] | None:
    """Baseload (kW) for each half-hour of the day, and days of data used.

    `house_kw` and `heat_kw` map local half-hour starts to mean kW. Before
    `heat_pump_since` the house load *is* the baseload (no heat pump to
    subtract); from then on the modelled heat pump use is subtracted, and only
    half-hours with a heat figure count. Returns None with less than
    `min_days` of usable data. Half-hours of the day with no data take the
    overall mean.
    """
    by_slot: list[list[float]] = [[] for _ in range(SLOTS)]
    n = 0
    for t, kw in house_kw.items():
        if heat_pump_since is not None and t < heat_pump_since:
            base = kw
        elif t in heat_kw:
            base = kw - heat_kw[t]
        else:
            continue
        by_slot[slot_of(t)].append(max(floor_kw, base))
        n += 1
    days = n / SLOTS
    if days < min_days:
        return None
    every = [v for vals in by_slot for v in vals]
    overall = sum(every) / len(every)
    profile = [sum(v) / len(v) if v else overall for v in by_slot]
    return profile, days
