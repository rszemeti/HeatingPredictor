"""Flow temperature planning and learning (no Home Assistant imports).

Emitters (radiators) deliver  Q = K * (MWT - T_room) ** n   (n ~ 1.3),
where MWT is the mean water temperature, (flow + return) / 2. The water
loop's temperature drop is  dT = Q / (m_dot * c_p).  So the flow temperature
needed to deliver Q into a room at T_room is

    flow = T_room + (Q / K) ** (1 / n) + dT / 2.

K is learned from the heat pump's own readings (heat out against mean water
temperature above the room, while it runs steadily). The heat loss H is
learned from a heat balance over whole days, and COP as a function of outdoor
and flow temperature from measured heat out / electricity in.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

WATER_KJ_PER_L_K = 4.19          # specific heat of water, per litre


@dataclass
class Sample:
    """Half-hour means from the heat pump's readings."""

    heat_kw: float          # heat out
    elec_kw: float          # electricity in
    flow: float             # flow (leaving water) °C
    ret: float              # return °C
    room: float             # room °C
    out: float              # outdoor °C


@dataclass
class Learned:
    k: float | None = None              # emitter constant, kW / K^n
    k_samples: int = 0
    heat_loss_w_per_k: float | None = None
    heat_loss_hours: float = 0.0
    cop: tuple[float, float, float] | None = None   # a + b*out + c*flow
    cop_samples: int = 0

    def as_dict(self) -> dict:
        return {
            "emitter_k": None if self.k is None else round(self.k, 4),
            "emitter_samples": self.k_samples,
            "heat_loss_w_per_k": None if self.heat_loss_w_per_k is None
            else round(self.heat_loss_w_per_k),
            "heat_loss_hours": round(self.heat_loss_hours, 1),
            "cop_model": None if self.cop is None else [round(x, 4) for x in self.cop],
            "cop_samples": self.cop_samples,
        }


def steady(s: Sample, min_heat_kw: float = 0.5) -> bool:
    """Compressor clearly running and delivering heat (no defrost)."""
    return (s.heat_kw >= min_heat_kw and s.elec_kw >= 0.15 and s.flow > s.ret
            and s.flow - s.room > 5)


def fit_emitter(samples: Iterable[Sample], n: float = 1.3, min_samples: int = 6
                ) -> tuple[float, int] | None:
    """Median K over steady half-hours (robust to the odd defrost or start)."""
    ks = []
    for s in samples:
        if not steady(s):
            continue
        dt = (s.flow + s.ret) / 2 - s.room
        if dt <= 3:
            continue
        ks.append(s.heat_kw / dt ** n)
    if len(ks) < min_samples:
        return None
    ks.sort()
    return ks[len(ks) // 2], len(ks)


def fit_cop(samples: Iterable[Sample], min_samples: int = 12
            ) -> tuple[tuple[float, float, float], int] | None:
    """Least squares COP = a + b*outdoor + c*flow over steady half-hours.

    Returned only if it's physically sensible: better when it's warmer
    outside (b > 0) and worse at higher flow temperatures (c < 0)."""
    rows = [(1.0, s.out, s.flow, s.heat_kw / s.elec_kw) for s in samples
            if steady(s) and 1.0 < s.heat_kw / s.elec_kw < 8.0]
    if len(rows) < min_samples:
        return None
    # Normal equations, 3x3.
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    atb = [sum(r[i] * r[3] for r in rows) for i in range(3)]
    sol = _solve3(ata, atb)
    if sol is None:
        return None
    a, b, c = sol
    if not (b > 0 and c < 0):
        return None
    return (a, b, c), len(rows)


def _solve3(m: list[list[float]], v: list[float]) -> tuple[float, float, float] | None:
    """Solve a 3x3 linear system (Cramer's rule)."""
    def det(x):
        return (x[0][0] * (x[1][1] * x[2][2] - x[1][2] * x[2][1])
                - x[0][1] * (x[1][0] * x[2][2] - x[1][2] * x[2][0])
                + x[0][2] * (x[1][0] * x[2][1] - x[1][1] * x[2][0]))
    d = det(m)
    if abs(d) < 1e-9:
        return None
    out = []
    for col in range(3):
        mc = [row[:] for row in m]
        for r in range(3):
            mc[r][col] = v[r]
        out.append(det(mc) / d)
    return out[0], out[1], out[2]


def fit_heat_loss(runs: Sequence[Sequence[Sample]], gains_kw: float,
                  capacity_kwh_per_k: float, hours_per_sample: float = 0.5,
                  min_hours: float = 24.0) -> tuple[float, float] | None:
    """Heat balance over contiguous runs of half-hours:

        sum(heat + gains) - C * (room_end - room_start) = H * sum(room - out)

    H in W/K and the hours used, or None with too little data. Includes losses
    on the way to the house (pipework outside), which the planning needs too.
    """
    num = den = hours = 0.0
    for run in runs:
        if len(run) < 2:
            continue
        num += sum(s.heat_kw + gains_kw for s in run) * hours_per_sample
        num -= capacity_kwh_per_k * (run[-1].room - run[0].room)
        den += sum(s.room - s.out for s in run) * hours_per_sample
        hours += len(run) * hours_per_sample
    if hours < min_hours or den <= 0:
        return None
    h = num / den * 1000
    if not 30 <= h <= 2000:
        return None
    return h, hours


def flow_for(heat_kw: float, t_room: float, k: float, n: float, flow_lpm: float) -> float:
    """Flow temperature that delivers `heat_kw` into a room at `t_room`."""
    if heat_kw <= 0:
        return t_room
    mwt = t_room + (heat_kw / k) ** (1 / n)
    d_t = heat_kw / (max(flow_lpm, 1.0) / 60 * WATER_KJ_PER_L_K)
    return mwt + d_t / 2


def cop_at(model: tuple[float, float, float], t_out: float, flow: float) -> float:
    a, b, c = model
    return max(1.0, a + b * t_out + c * flow)


@dataclass
class FlowParams:
    k: float = 0.065            # kW / K^n until learned
    n: float = 1.3
    flow_lpm: float = 14.5
    flow_min: float = 25.0
    flow_max: float = 55.0
    idle_kw: float = 0.15       # below this the pump is effectively off


def plan_flows(rows: Sequence[tuple[float, float]], p: FlowParams,
               correction: float = 0.0) -> list[float | None]:
    """Flow target for each (heat_kw, t_room) period; None where the pump
    should simply idle. Limited to [flow_min, flow_max], to 0.5 °C."""
    out: list[float | None] = []
    for heat_kw, t_room in rows:
        if heat_kw < p.idle_kw:
            out.append(None)
            continue
        f = flow_for(heat_kw, t_room, p.k, p.n, p.flow_lpm) + correction
        f = min(p.flow_max, max(p.flow_min, f))
        out.append(round(f * 2) / 2)
    return out


def balance_point(h_w_per_k: float, gains_kw: float, t_room: float,
                  k: float, n: float, flow_lpm: float, flow_max: float) -> float:
    """Lowest outdoor temperature at which `flow_max` still holds `t_room`."""
    lo, hi = -40.0, t_room
    for _ in range(60):
        mid = (lo + hi) / 2
        need = max(0.0, h_w_per_k / 1000 * (t_room - mid) - gains_kw)
        if flow_for(need, t_room, k, n, flow_lpm) <= flow_max:
            hi = mid
        else:
            lo = mid
    return hi


def water_law(h_w_per_k: float, gains_kw: float, t_room: float, k: float, n: float,
              flow_lpm: float, outdoor: Sequence[float] = (-10, -5, 0, 5, 10, 15)
              ) -> list[tuple[float, float]]:
    """Steady-state flow temperature for each outdoor temperature."""
    pts = []
    for t in outdoor:
        need = max(0.0, h_w_per_k / 1000 * (t_room - t) - gains_kw)
        pts.append((t, round(flow_for(need, t_room, k, n, flow_lpm), 1)))
    return pts


def runs_of(samples: dict, step_s: float = 1800) -> list[list[Sample]]:
    """Split {time: Sample} into contiguous runs (no missing half-hours)."""
    runs: list[list[Sample]] = []
    prev = None
    for t in sorted(samples):
        if prev is None or (t - prev).total_seconds() > step_s + 1:
            runs.append([])
        runs[-1].append(samples[t])
        prev = t
    return runs


def is_finite(*xs: float) -> bool:
    return all(x is not None and math.isfinite(x) for x in xs)


# ---------------------------------------------------------------- house fit
#
# Learned straight from history (flow, room, outdoor), without the radiator
# and heat loss settings. Over an hour or two the house behaves like
#
#     d(room)/dt = a * (flow - room) - b * (room - outdoor) + c
#
# (radiators heating it, losses to outside, sun/people/cooking), which,
# rearranged, is linear in three unknowns:
#
#     flow - room = s * (room - outdoor) + i + tau * d(room)/dt
#
# with s = b/a, i = -c/a, tau = 1/a (hours). Holding a room at R when it's T
# outside then needs  flow = R + s * (R - T) + i. The measured flow includes
# the pump's off-cycles, so this is the flow that, run steadily, delivers the
# same heat: what a water law should be.


@dataclass
class HouseFit:
    s: float                # flow-above-room per K of room-above-outdoor
    i: float                # offset °C (negative: free gains)
    tau: float              # hours: extra flow per °C/h of warm-up
    hours: float
    rmse: float
    mode: str               # "fit": all three learned; "anchored": s from the settings

    def flow(self, room: float, out: float, d_room_per_h: float = 0.0) -> float:
        return room + self.s * (room - out) + self.i + self.tau * max(0.0, d_room_per_h)

    def holds_down_to(self, room: float, flow_max: float) -> float:
        return room - (flow_max - room - self.i) / self.s

    def as_dict(self) -> dict:
        return {"slope": round(self.s, 3), "offset": round(self.i, 2), "warmup_h": round(self.tau, 2),
                "hours": round(self.hours, 1), "rmse": round(self.rmse, 2), "mode": self.mode}


def house_points(samples: dict, step_s: float = 1800, half_window: int = 3
                 ) -> list[tuple[float, float, float]]:
    """(room - out, d(room)/dt per hour, flow - room) over each span of
    2 * half_window half-hours (half-hour means at either end)."""
    pts = []
    hours = 2 * half_window * step_s / 3600
    for run in runs_of(samples, step_s):
        run = [s for s in run]
        for j in range(half_window, len(run) - half_window):
            win = run[j - half_window:j + half_window + 1]
            if not all(is_finite(s.out, s.flow, s.room) for s in win):
                continue
            # Half-hour means, so the room change from the first to the last
            # spans 2 * half_window half-hours; integrate over the same span
            # (trapezoid weights) to keep the equation exact.
            w = [0.5] + [1.0] * (len(win) - 2) + [0.5]
            tw = sum(w)
            room = sum(wi * s.room for wi, s in zip(w, win)) / tw
            out = sum(wi * s.out for wi, s in zip(w, win)) / tw
            flow = sum(wi * s.flow for wi, s in zip(w, win)) / tw
            d_room = (win[-1].room - win[0].room) / hours
            pts.append((room - out, d_room, flow - room))
    return pts


def _lsq(rows: list[tuple[float, ...]]) -> tuple[float, ...] | None:
    """Least squares for y = sum(x_k * p_k) with up to 3 unknowns; the last
    element of each row is y."""
    n = len(rows[0]) - 1
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    atb = [sum(r[i] * r[n] for r in rows) for i in range(n)]
    if n == 3:
        return _solve3(ata, atb)
    if n == 2:
        d = ata[0][0] * ata[1][1] - ata[0][1] * ata[1][0]
        if abs(d) < 1e-9:
            return None
        return ((atb[0] * ata[1][1] - ata[0][1] * atb[1]) / d,
                (ata[0][0] * atb[1] - atb[0] * ata[1][0]) / d)
    return None


def fit_house(samples: dict, s_fallback: float | None = None, min_hours: float = 24.0,
              step_s: float = 1800) -> HouseFit | None:
    """Fit the house from history. If the three-way fit isn't physically
    sensible (too little variety, e.g. a fixed flow for days), keep the slope
    from the settings (`s_fallback`) and learn only the offset and warm-up."""
    pts = house_points(samples, step_s)
    hours = len(pts) * step_s / 3600
    if hours < min_hours:
        return None

    def rmse(s, i, tau):
        return math.sqrt(sum((y - (s * x + i + tau * d)) ** 2 for x, d, y in pts) / len(pts))

    sol = _lsq([(x, 1.0, d, y) for x, d, y in pts])
    if sol is not None:
        s, i, tau = sol
        # Physically sensible, and not wildly off typical houses.
        if 0.1 <= s <= 4.0 and 0 <= tau <= 48 and -15 <= i <= 15:
            return HouseFit(s, i, tau, hours, rmse(s, i, tau), "fit")
    if s_fallback is None:
        return None
    s = s_fallback
    sol2 = _lsq([(1.0, d, y - s * x) for x, d, y in pts])
    if sol2 is None:
        i, tau = sum(y - s * x for x, _, y in pts) / len(pts), 0.0
    else:
        i, tau = sol2
        if not 0 <= tau <= 48:
            tau = 0.0
            i = sum(y - s * x for x, _, y in pts) / len(pts)
    return HouseFit(s, i, tau, hours, rmse(s, i, tau), "anchored")


def model_slope(h_w_per_k: float, gains_kw: float, t_room: float, k: float, n: float,
                flow_lpm: float, around: float = 5.0) -> float:
    """Slope of the settings-based water law (flow per K of room-above-outdoor)."""
    f_cold = water_law(h_w_per_k, gains_kw, t_room, k, n, flow_lpm, (around - 5,))[0][1]
    f_warm = water_law(h_w_per_k, gains_kw, t_room, k, n, flow_lpm, (around + 5,))[0][1]
    return max(0.1, (f_cold - f_warm) / 10)
