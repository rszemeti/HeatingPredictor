"""Flow temperature planning and learning."""

import importlib.util
import pathlib
import random
import sys
from datetime import datetime, timedelta, timezone

import pytest

_spec = importlib.util.spec_from_file_location(
    "hlp_flowlaw",
    pathlib.Path(__file__).parents[1] / "custom_components" / "heat_load_predictor" / "flowlaw.py",
)
fl = importlib.util.module_from_spec(_spec)
sys.modules["hlp_flowlaw"] = fl
_spec.loader.exec_module(fl)

K, N, LPM = 0.065, 1.3, 14.5


def test_flow_for_inverts_the_emitter_equation():
    for q in (0.5, 2.0, 5.0):
        f = fl.flow_for(q, 21.0, K, N, LPM)
        d_t = q / (LPM / 60 * 4.19)
        mwt = f - d_t / 2
        assert K * (mwt - 21.0) ** N == pytest.approx(q, rel=1e-6)


def test_saturday_numbers():
    """24 Oct: ~2.0 kW to the rooms at 40 °C flow with the house at 23.2 °C."""
    f = fl.flow_for(2.0, 23.2, K, N, LPM)
    assert 37 < f < 41


def test_water_law_rises_as_it_gets_colder():
    pts = fl.water_law(200, 0.4, 21.5, K, N, LPM)
    flows = [f for _, f in pts]
    assert flows == sorted(flows, reverse=True)
    assert 28 <= dict(pts)[15] <= 32 and 44 <= dict(pts)[0] <= 50


def test_plan_flows_idle_limits_and_rounding():
    p = fl.FlowParams(flow_min=25, flow_max=50)
    out = fl.plan_flows([(0.05, 21), (0.3, 21), (2.0, 21), (20.0, 21)], p)
    assert out[0] is None
    assert out[1] == 25                       # raised to the minimum
    assert out[2] * 2 == int(out[2] * 2)      # 0.5 °C steps
    assert out[3] == 50                       # capped
    assert fl.plan_flows([(2.0, 21)], p, correction=1.0)[0] == out[2] + 1.0


def _synthetic(days=4, h=210.0, k=K, cop=(3.4, 0.08, -0.04), seed=1):
    """Half-hour readings from a house that obeys the model exactly (+noise)."""
    rng = random.Random(seed)
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    samples = {}
    room = 21.0
    cap, gains = 10.0, 0.4
    for i in range(days * 48):
        t = t0 + timedelta(minutes=30 * i)
        out = 6 + 4 * __import__("math").sin(i / 48 * 6.283) + rng.uniform(-0.5, 0.5)
        running = (i % 48) not in range(2, 8)       # a few idle half-hours each night
        heat = (h / 1000 * (room - out) - gains + rng.uniform(-0.2, 0.3)) if running else 0.0
        heat = max(0.0, heat)
        flow = fl.flow_for(heat, room, k, N, LPM) if heat > 0 else room + 2
        d_t = heat / (LPM / 60 * 4.19)
        ret = flow - d_t
        elec = heat / (cop[0] + cop[1] * out + cop[2] * flow) if heat > 0 else 0.01
        samples[t] = fl.Sample(heat, elec, flow, ret, room, out)
        # house temperature follows the balance
        room += ((heat + gains) - h / 1000 * (room - out)) * 0.5 / cap
    return samples


def test_learning_recovers_the_house():
    s = _synthetic()
    k, nk = fl.fit_emitter(s.values())
    assert k == pytest.approx(K, rel=0.03) and nk > 100
    h, hours = fl.fit_heat_loss(fl.runs_of(s), 0.4, 10.0)
    assert h == pytest.approx(210, rel=0.1) and hours >= 24
    (a, b, c), nc = fl.fit_cop(s.values())
    assert b > 0 and c < 0
    assert fl.cop_at((a, b, c), 5, 40) == pytest.approx(3.4 + 0.4 - 1.6, rel=0.05)


def test_learning_needs_enough_data():
    s = dict(list(_synthetic(days=1).items())[:10])
    assert fl.fit_emitter(s.values()) is None
    assert fl.fit_heat_loss(fl.runs_of(s), 0.4, 10.0) is None
    assert fl.fit_cop(s.values()) is None


def test_runs_split_on_gaps():
    s = _synthetic(days=1)
    keys = sorted(s)
    del s[keys[10]]
    runs = fl.runs_of(s)
    assert len(runs) == 2 and len(runs[0]) == 10


def test_balance_point():
    bp = fl.balance_point(210, 0.4, 21.5, K, N, LPM, 50)
    assert -12 < bp < 0
    # Lower heat loss holds to a colder day.
    assert fl.balance_point(180, 0.4, 21.5, K, N, LPM, 50) < bp


def _fixed_flow_house(days=5, flow=32.0, a=0.1, b=0.08, c=0.04, seed=3):
    """A house heated at a fixed flow, room following
    d(room)/dt = a(flow-room) - b(room-out) + c  (per hour)."""
    import math
    rng = random.Random(seed)
    t0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
    room, samples = 22.0, {}
    for i in range(days * 48):
        out = 9 + 4 * math.sin(i / 48 * 6.283) + rng.uniform(-0.3, 0.3)
        f = flow if (i % 6) else flow - 6      # an off-cycle now and then
        rooms = []
        for _ in range(6):                     # 5-minute steps
            room += (a * (f - room) - b * (room - out) + c) / 12
            rooms.append(room)
        mean_room = round(sum(rooms) / 6 + rng.uniform(-0.05, 0.05), 2)   # as the recorder keeps it
        samples[t0 + timedelta(minutes=30 * i)] = fl.Sample(1.5, 0.45, f, f - 3, mean_room, out)
    return samples


def test_house_fit_from_a_fixed_flow():
    """Fixed 32 °C flow holding ~22 °C: the learned law gives about 32 °C at
    the conditions it ran in, and less for a lower target."""
    s = _fixed_flow_house()
    hf = fl.fit_house(s, s_fallback=1.0)
    assert hf is not None
    assert hf.mode == "fit" and hf.s == pytest.approx(0.08 / 0.1, rel=0.2), hf
    pts = list(s.values())
    room = sum(p.room for p in pts) / len(pts)
    out = sum(p.out for p in pts) / len(pts)
    mean_flow = sum(p.flow for p in pts) / len(pts)
    assert hf.flow(room, out) == pytest.approx(mean_flow, abs=0.7)
    assert hf.flow(room - 1, out) < mean_flow - 0.5
    assert hf.holds_down_to(21, 55) < -20


def test_house_fit_matches_the_physics_house():
    s = _synthetic(days=5)
    hf = fl.fit_house(s, s_fallback=1.0)
    assert hf is not None and hf.s > 0
    for out in (2, 6, 10):
        need = 210 / 1000 * (21 - out) - 0.4
        assert hf.flow(21, out) == pytest.approx(fl.flow_for(need, 21, K, N, LPM), abs=2.5)


def test_house_fit_anchors_when_the_fit_is_nonsense():
    s = _fixed_flow_house()
    # Room readings frozen (a sensor stuck at one value): no slope to learn.
    for t, smp in s.items():
        s[t] = fl.Sample(smp.heat_kw, smp.elec_kw, 32.0, 29.0, 22.0, smp.out)
    hf = fl.fit_house(s, s_fallback=1.2)
    assert hf.mode == "anchored" and hf.s == 1.2
    out = sum(p.out for p in s.values()) / len(s)
    assert hf.flow(22.0, out) == pytest.approx(32.0, abs=0.3)


def test_house_fit_needs_a_day():
    s = dict(list(_fixed_flow_house(days=1).items())[:30])
    assert fl.fit_house(s, s_fallback=1.0) is None


def test_coarse_room_sensor_keeps_the_heat_meter_slope():
    """A room sensor in 0.5 °C steps with the flow fixed for days drags the
    free slope towards zero (2 Nov: 0.19, "holds down to -107 °C"). The slope
    then comes from the radiator / heat loss figures; the level from history."""
    s = _fixed_flow_house(days=6)
    for t, smp in s.items():
        s[t] = fl.Sample(smp.heat_kw, smp.elec_kw, smp.flow, smp.ret, round(smp.room * 2) / 2, smp.out)
    pts = fl.house_points(s)
    free = fl._lsq([(x, 1.0, d, y) for x, d, y in pts])
    hf = fl.fit_house(s, s_fallback=0.8)
    print("free fit", free, "->", hf)
    assert hf.mode == "anchored" and hf.s == 0.8
    assert hf.holds_down_to(21, 55) > -40
    pts_ = list(s.values())
    room = sum(p.room for p in pts_) / len(pts_)
    out = sum(p.out for p in pts_) / len(pts_)
    mean_flow = sum(p.flow for p in pts_) / len(pts_)
    assert hf.flow(room, out) == pytest.approx(mean_flow, abs=1.0)


def test_house_flows_run_steadily_not_on_off():
    """7 Nov: the room read 0.2-0.4 °C over target, the heat model coasted,
    25 °C was written for two hours, then 42-45 °C to recover."""
    house = fl.HouseFit(s=1.0, i=0.24, tau=6.75, hours=165, rmse=2.5, mode="anchored")
    p = fl.FlowParams()
    steady = house.flow(22.0, 5.0)
    rows = [
        (22.0, 22.4, 22.35, 5.0, 0.0),    # a bit warm, model coasting: keep going
        (22.0, 21.7, 22.0, 5.0, 6.0),     # a bit cold, model recovering hard: no boost
        (22.0, 22.0, 22.0, 5.0, 2.0),
    ]
    out = fl.plan_house_flows(rows, house, p)
    assert out == [round(steady * 2) / 2] * 3, (out, steady)
    # The correction does the adjusting.
    assert fl.plan_house_flows(rows[:1], house, p, correction=-0.8)[0] < out[0]


def test_house_flows_idle_after_a_setback_and_boost_a_warm_up():
    house = fl.HouseFit(s=1.0, i=0.0, tau=6.0, hours=100, rmse=2, mode="fit")
    p = fl.FlowParams()
    out = fl.plan_house_flows([
        (18.0, 21.5, 21.3, 5.0, 0.0),     # night setback, coasting: idle
        (21.0, 18.5, 19.5, 5.0, 6.0),     # morning warm-up: extra flow
    ], house, p)
    assert out[0] is None
    assert out[1] > house.flow(21.0, 5.0) + 5
