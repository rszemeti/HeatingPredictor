"""Heating load model, free of Home Assistant imports so it can be tested alone.

The house is a single thermal mass (a first-order RC model):

    C * dTin/dt = P_heat + G - H * (Tin - Tout)

    C  thermal capacity (kWh/K)      H  heat loss coefficient (kW/K)
    G  internal gains (kW)           P  heat pump heat output (kW)

Within each period Tout and P are held constant, so the solution is exact:

    Tin(t) = Teq + (Tin0 - Teq) * exp(-t / tau),   Teq = Tout + (P + G) / H,
    tau = C / H

For each period the model chooses the heat output P that brings Tin to the
required temperature at the end of the period, clamped to 0 (the house coasts
down after a setback) and to the heat pump's maximum output at that outdoor
temperature. Electricity is heat / COP(Tout) plus a fixed standby draw.

With pre-heat on, a backward pass first raises the required temperatures ahead
of each step up in the schedule, so the house reaches the target on time even
when the heat pump can't make the jump in a single period.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Generic starting points for a modern air-to-water heat pump on weather
# compensation (roughly 45 °C flow when cold, 35 °C when mild). Replace them
# with your heat pump's figures at the flow temperatures it actually runs at.
DEFAULT_COP_CURVE: list[tuple[float, float]] = [
    (-10.0, 2.3),
    (-5.0, 2.6),
    (0.0, 3.0),
    (5.0, 3.6),
    (10.0, 4.3),
    (15.0, 4.9),
]
# Maximum heat output (kW) against outdoor temperature, defrost-derated,
# for a nominal 8 kW unit.
DEFAULT_CAPACITY_CURVE: list[tuple[float, float]] = [
    (-10.0, 6.0),
    (-5.0, 6.6),
    (2.0, 7.0),
    (7.0, 8.0),
]


def default_schedule() -> dict:
    """Steady 20.5 °C by day with a mild 18 °C overnight setback, every day."""
    day = [
        {"time": "00:00", "temp": 18.0},
        {"time": "06:30", "temp": 20.5},
        {"time": "22:30", "temp": 18.0},
    ]
    return {
        "interpolation": "step",
        "days": {d: [dict(p) for p in day] for d in DAYS},
    }


# --------------------------------------------------------------------------
# Curves
# --------------------------------------------------------------------------


def parse_curve(value: str | Iterable) -> list[tuple[float, float]]:
    """Parse a curve such as "-10:2.2, -7:2.4, 7:3.6" or [[-10, 2.2], ...].

    Returns points sorted by x. Raises ValueError on bad input.
    """
    points: list[tuple[float, float]] = []
    if isinstance(value, str):
        for chunk in value.replace(";", ",").replace("\n", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                raise ValueError(f"'{chunk}' should look like temperature:value")
            x, y = chunk.split(":", 1)
            points.append((float(x), float(y)))
    else:
        for item in value:
            x, y = item
            points.append((float(x), float(y)))
    if not points:
        raise ValueError("curve needs at least one point")
    points.sort(key=lambda p: p[0])
    xs = [p[0] for p in points]
    if len(set(xs)) != len(xs):
        raise ValueError("curve has two points at the same temperature")
    return points


def format_curve(points: Sequence[tuple[float, float]]) -> str:
    return ", ".join(f"{_num(x)}:{_num(y)}" for x, y in points)


def _num(v: float) -> str:
    return f"{v:g}"


def interp(points: Sequence[tuple[float, float]], x: float) -> float:
    """Linear interpolation, held flat beyond the end points."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]  # pragma: no cover


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"bad time '{hhmm}'")
    return h * 60 + m


def validate_schedule(schedule: dict) -> dict:
    """Return a cleaned copy of the schedule, raising ValueError if invalid."""
    interpolation = schedule.get("interpolation", "step")
    if interpolation not in ("step", "linear"):
        raise ValueError("interpolation must be 'step' or 'linear'")
    days_in = schedule.get("days") or {}
    days: dict[str, list[dict]] = {}
    for d in DAYS:
        pts = days_in.get(d)
        if not pts:
            raise ValueError(f"no points for {d}")
        cleaned = []
        for p in pts:
            mins = _minutes(str(p["time"]))
            if mins >= 1440:
                raise ValueError("times must be before 24:00")
            temp = float(p["temp"])
            if not 5.0 <= temp <= 30.0:
                raise ValueError(f"temperature {temp} is outside 5-30 °C")
            cleaned.append({"time": f"{mins // 60:02d}:{mins % 60:02d}", "temp": round(temp, 2)})
        cleaned.sort(key=lambda p: _minutes(p["time"]))
        if len({p["time"] for p in cleaned}) != len(cleaned):
            raise ValueError(f"two points at the same time on {d}")
        days[d] = cleaned
    return {"interpolation": interpolation, "days": days}


def schedule_temp(schedule: dict, when: datetime) -> float:
    """Target indoor temperature at a local time."""
    wd = when.weekday()
    today = schedule["days"][DAYS[wd]]
    yesterday = schedule["days"][DAYS[(wd - 1) % 7]]
    tomorrow = schedule["days"][DAYS[(wd + 1) % 7]]
    now = when.hour * 60 + when.minute + when.second / 60

    # Points on a timeline in minutes from today's midnight, with yesterday's
    # last point and tomorrow's first so the curve wraps across midnight.
    line = [(_minutes(yesterday[-1]["time"]) - 1440, yesterday[-1]["temp"])]
    line += [(_minutes(p["time"]), p["temp"]) for p in today]
    line.append((_minutes(tomorrow[0]["time"]) + 1440, tomorrow[0]["temp"]))

    prev = line[0]
    for pt in line[1:]:
        if pt[0] > now:
            if schedule.get("interpolation") == "linear":
                span = pt[0] - prev[0]
                return prev[1] + (pt[1] - prev[1]) * (now - prev[0]) / span
            return prev[1]
        prev = pt
    return prev[1]  # pragma: no cover


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


@dataclass
class HouseParams:
    heat_loss_w_per_k: float = 200.0
    thermal_capacity_kwh_per_k: float = 10.0
    internal_gains_w: float = 400.0
    standby_w: float = 15.0
    cop_curve: list[tuple[float, float]] = field(default_factory=lambda: list(DEFAULT_COP_CURVE))
    capacity_curve: list[tuple[float, float]] = field(
        default_factory=lambda: list(DEFAULT_CAPACITY_CURVE)
    )
    preheat: bool = True

    def __post_init__(self) -> None:
        if self.heat_loss_w_per_k <= 0:
            raise ValueError("heat loss must be above zero")
        if self.thermal_capacity_kwh_per_k <= 0:
            raise ValueError("thermal capacity must be above zero")


@dataclass
class PeriodResult:
    start: datetime
    end: datetime
    t_out: float          # mean outdoor temperature, °C
    t_target: float       # required indoor temperature at period end, °C
    t_in_start: float     # modelled indoor temperature, °C
    t_in_end: float
    heat_kw: float        # mean heat output, kW
    heat_kwh: float
    cop: float
    elec_kwh: float       # heat pump electricity incl. standby, kWh
    shortfall_k: float    # how far below target the house ends the period

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kwh": round(self.elec_kwh, 3),
            "heat_kwh": round(self.heat_kwh, 3),
            "t_out": round(self.t_out, 1),
            "t_target": round(self.t_target, 2),
            "t_in": round(self.t_in_end, 2),
            "cop": round(self.cop, 2),
            "shortfall_k": round(self.shortfall_k, 2),
        }


def simulate(
    *,
    start: datetime,
    periods: int,
    period_minutes: int,
    outdoor_temp: Callable[[datetime], float],
    target_temp: Callable[[datetime], float],
    t_in_start: float,
    params: HouseParams,
) -> list[PeriodResult]:
    """Model `periods` consecutive periods from `start`."""
    if periods <= 0:
        return []
    dt_h = period_minutes / 60.0
    H = params.heat_loss_w_per_k / 1000.0           # kW/K
    C = params.thermal_capacity_kwh_per_k            # kWh/K
    G = params.internal_gains_w / 1000.0             # kW
    a = math.exp(-dt_h * H / C)                      # decay over one period

    bounds = [start + timedelta(minutes=period_minutes * k) for k in range(periods + 1)]
    t_out = [
        (outdoor_temp(bounds[i]) + outdoor_temp(bounds[i + 1])) / 2.0 for i in range(periods)
    ]
    p_max = [max(0.0, interp(params.capacity_curve, t)) for t in t_out]

    # Required indoor temperature at each boundary (index 0 is now, already given).
    required = [target_temp(b) for b in bounds]
    if params.preheat:
        for i in range(periods - 1, 0, -1):
            teq_max = t_out[i] + (p_max[i] + G) / H
            # Lowest temperature at boundary i from which full output for one
            # period still reaches required[i + 1].
            need = (required[i + 1] - teq_max * (1.0 - a)) / a
            if need > required[i]:
                required[i] = need

    results: list[PeriodResult] = []
    tin = t_in_start
    for i in range(periods):
        teq_needed = (required[i + 1] - tin * a) / (1.0 - a)
        p = H * (teq_needed - t_out[i]) - G
        p = min(max(p, 0.0), p_max[i])
        teq = t_out[i] + (p + G) / H
        tin_end = teq + (tin - teq) * a
        heat_kwh = p * dt_h
        cop = max(0.5, interp(params.cop_curve, t_out[i]))
        elec = heat_kwh / cop + params.standby_w / 1000.0 * dt_h
        results.append(
            PeriodResult(
                start=bounds[i],
                end=bounds[i + 1],
                t_out=t_out[i],
                t_target=required[i + 1],
                t_in_start=tin,
                t_in_end=tin_end,
                heat_kw=p,
                heat_kwh=heat_kwh,
                cop=cop,
                elec_kwh=elec,
                shortfall_k=max(0.0, target_temp(bounds[i + 1]) - tin_end),
            )
        )
        tin = tin_end
    return results


def forecast_interpolator(
    points: Sequence[tuple[datetime, float]],
) -> Callable[[datetime], float]:
    """Linear interpolation over (time, temperature) points, flat beyond the ends."""
    pts = sorted(points, key=lambda p: p[0])
    if not pts:
        raise ValueError("no forecast points")
    xs = [p[0].timestamp() for p in pts]
    curve = [(x, p[1]) for x, p in zip(xs, pts)]

    def fn(when: datetime) -> float:
        return interp(curve, when.timestamp())

    return fn


def align_down(when: datetime, period_minutes: int) -> datetime:
    """Start of the period containing `when` (periods aligned to local midnight)."""
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    mins = int((when - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=(mins // period_minutes) * period_minutes)
