"""Tests for the pure-Python model."""

import importlib.util
import math
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

_spec = importlib.util.spec_from_file_location(
    "hlp_model",
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "heat_load_predictor" / "model.py",
)
m = importlib.util.module_from_spec(_spec)
sys.modules["hlp_model"] = m
_spec.loader.exec_module(m)

UTC = timezone.utc
MON = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday


def flat_schedule(temp):
    return m.validate_schedule(
        {"interpolation": "step", "days": {d: [{"time": "00:00", "temp": temp}] for d in m.DAYS}}
    )


def test_parse_curve():
    assert m.parse_curve("7:3.6, -10:2.2\n2:2.9") == [(-10, 2.2), (2, 2.9), (7, 3.6)]
    assert m.parse_curve([[1, 2]]) == [(1.0, 2.0)]
    with pytest.raises(ValueError):
        m.parse_curve("")
    with pytest.raises(ValueError):
        m.parse_curve("1:2, 1:3")
    with pytest.raises(ValueError):
        m.parse_curve("abc")


def test_interp_clamps():
    c = [(-10, 2.0), (10, 4.0)]
    assert m.interp(c, -20) == 2.0
    assert m.interp(c, 0) == pytest.approx(3.0)
    assert m.interp(c, 30) == 4.0


def test_schedule_step_and_wrap():
    s = m.default_schedule()
    assert m.schedule_temp(s, MON.replace(hour=3)) == 18.0
    assert m.schedule_temp(s, MON.replace(hour=6, minute=30)) == 20.5
    assert m.schedule_temp(s, MON.replace(hour=23)) == 18.0


def test_schedule_wraps_from_previous_day():
    s = m.default_schedule()
    s["days"]["mon"] = [{"time": "06:00", "temp": 21.0}]
    s["days"]["sun"] = [{"time": "08:00", "temp": 20.0}, {"time": "22:00", "temp": 16.0}]
    s = m.validate_schedule(s)
    # Before Monday's first point, Sunday's last point still applies.
    assert m.schedule_temp(s, MON.replace(hour=2)) == 16.0


def test_schedule_linear():
    s = m.validate_schedule(
        {
            "interpolation": "linear",
            "days": {d: [{"time": "00:00", "temp": 18}, {"time": "12:00", "temp": 22}] for d in m.DAYS},
        }
    )
    assert m.schedule_temp(s, MON.replace(hour=6)) == pytest.approx(20.0)
    # From 12:00 back down to tomorrow's 00:00 point.
    assert m.schedule_temp(s, MON.replace(hour=18)) == pytest.approx(20.0)


def test_validate_rejects_bad():
    with pytest.raises(ValueError):
        m.validate_schedule({"days": {}})
    s = m.default_schedule()
    s["days"]["mon"].append({"time": "06:30", "temp": 19})
    with pytest.raises(ValueError):
        m.validate_schedule(s)
    s = m.default_schedule()
    s["days"]["tue"][0]["temp"] = 40
    with pytest.raises(ValueError):
        m.validate_schedule(s)


def run(t_out, target, tin0, hours=24, period=30, **kw):
    params = m.HouseParams(**kw)
    sched = flat_schedule(target) if isinstance(target, (int, float)) else target
    return m.simulate(
        start=MON,
        periods=int(hours * 60 / period),
        period_minutes=period,
        outdoor_temp=lambda _: t_out,
        target_temp=lambda t: m.schedule_temp(sched, t),
        t_in_start=tin0,
        params=params,
    )


def test_steady_state_matches_heat_loss():
    # At steady state, heat = H*(Tin-Tout) - gains: 0.19*(20-0) - 0.6 = 3.2 kW
    res = run(0.0, 20.0, 20.0, heat_loss_w_per_k=190, internal_gains_w=600, standby_w=0)
    for r in res:
        assert r.heat_kw == pytest.approx(3.2, abs=1e-6)
        assert r.t_in_end == pytest.approx(20.0, abs=1e-6)
    cop = m.interp(m.DEFAULT_COP_CURVE, 0.0)
    assert sum(r.elec_kwh for r in res) == pytest.approx(3.2 * 24 / cop, rel=1e-6)


def test_standby_only_when_warm_outside():
    res = run(20.0, 18.0, 18.0, standby_w=20)
    assert all(r.heat_kwh == 0 for r in res)
    assert sum(r.elec_kwh for r in res) == pytest.approx(0.02 * 24)


def test_setback_coasts_then_recovers():
    sched = m.default_schedule()  # 18 overnight, 20.5 from 06:30
    res = run(5.0, sched, 20.5, hours=24, preheat=False)
    by_time = {r.start.strftime("%H:%M"): r for r in res}
    # Overnight starting warm: the house coasts, no heat.
    assert by_time["00:00"].heat_kwh == 0
    # The period ending at 06:30 works to reach 20.5 at the boundary.
    assert by_time["06:00"].heat_kwh > by_time["12:00"].heat_kwh


def test_preheat_starts_earlier_and_meets_target():
    sched = m.default_schedule()
    # Small pump and light house: holding 20.5 at 5 °C needs 2.35 kW, so a
    # 4 kW pump can recover, but only at ~0.5 K/h.
    kw = dict(capacity_curve=[(-20, 4.0), (20, 4.0)], thermal_capacity_kwh_per_k=3.0)
    no = run(5.0, sched, 18.0, preheat=False, **kw)
    yes = run(5.0, sched, 18.0, preheat=True, **kw)
    idx_0630 = 13  # boundary at 06:30 is end of period index 12
    assert no[12].shortfall_k > 0.1           # without pre-heat, late
    assert yes[12].shortfall_k < 1e-6         # with pre-heat, on time
    # Pre-heat moves energy earlier: more heat before 06:00.
    early_yes = sum(r.heat_kwh for r in yes[:12])
    early_no = sum(r.heat_kwh for r in no[:12])
    assert early_yes > early_no
    assert idx_0630 == 13


def test_capacity_limit_respected():
    res = run(-10.0, 21.0, 21.0, capacity_curve=[(-20, 3.0), (20, 3.0)])
    assert max(r.heat_kw for r in res) <= 3.0 + 1e-9
    assert res[-1].shortfall_k > 0


def test_energy_balance_exact():
    # Heat in + gains - losses == C * dT, using the analytic mean temperature.
    p = m.HouseParams()
    res = run(3.0, m.default_schedule(), 19.0)
    H, C, G = p.heat_loss_w_per_k / 1000, p.thermal_capacity_kwh_per_k, p.internal_gains_w / 1000
    for r in res:
        dt = (r.end - r.start).total_seconds() / 3600
        tau = C / H
        teq = r.t_out + (r.heat_kw + G) / H
        mean_tin = teq + (r.t_in_start - teq) * tau / dt * (1 - math.exp(-dt / tau))
        balance = r.heat_kwh + G * dt - H * (mean_tin - r.t_out) * dt
        assert balance == pytest.approx(C * (r.t_in_end - r.t_in_start), abs=1e-9)


def test_forecast_interpolator_and_align():
    pts = [(MON, 0.0), (MON + timedelta(hours=2), 4.0)]
    f = m.forecast_interpolator(pts)
    assert f(MON + timedelta(hours=1)) == pytest.approx(2.0)
    assert f(MON - timedelta(hours=5)) == 0.0
    t = MON.replace(hour=7, minute=44, second=12)
    assert m.align_down(t, 30) == MON.replace(hour=7, minute=30)
    assert m.align_down(t, 10) == MON.replace(hour=7, minute=40)


def test_realistic_cold_day():
    """A 190 W/K house on a -2 °C day: ~85-90 kWh heat, ~33 kWh electricity at COP 2.7.
    (Cross-checked against a real house's half-hourly gas data.)"""
    res = run(-2.0, m.default_schedule(), 18.0, hours=24, heat_loss_w_per_k=190,
              thermal_capacity_kwh_per_k=8, internal_gains_w=600,
              cop_curve=[(-10, 2.2), (-7, 2.4), (-2, 2.7), (2, 2.9), (7, 3.6)])
    heat = sum(r.heat_kwh for r in res)
    elec = sum(r.elec_kwh for r in res)
    assert 75 < heat < 100
    assert 28 < elec < 40
