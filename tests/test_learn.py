"""Tests for learning the house baseload."""

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location(
    "hlp_learn", pathlib.Path(__file__).parents[1] / "custom_components" / "heat_load_predictor" / "learn.py")
lr = importlib.util.module_from_spec(_spec)
sys.modules["hlp_learn"] = lr
_spec.loader.exec_module(lr)

LON = ZoneInfo("Europe/London")
DAY = datetime(2026, 10, 13, tzinfo=LON)


def true_base(t):
    """Rest of house: 0.5 kW, 1.8 kW at teatime 17:00-19:00."""
    return 1.8 if 17 <= t.hour < 19 else 0.5


def test_half_hour_means():
    s = [(DAY + timedelta(minutes=5 * i), float(i % 6)) for i in range(12)]
    m = lr.half_hour_means(s, DAY, DAY + timedelta(hours=1))
    assert m == {DAY: 2.5, DAY + timedelta(minutes=30): 2.5}


def test_profile_before_and_after_heat_pump():
    since = DAY + timedelta(days=8, hours=6)        # heat pump started day 9, 06:00
    house, heat = {}, {}
    for i in range(10 * 48):
        t = DAY + timedelta(minutes=30 * i)
        hp = 1.2 if t >= since else 0.0             # real heat pump use (only after start)
        house[t] = true_base(t) + hp
        heat[t] = 1.2                               # the model assumes heating throughout
    prof, days = lr.base_profile(house, heat, heat_pump_since=since)
    assert abs(days - 10) < 0.01
    assert abs(prof[lr.slot_of(DAY.replace(hour=3))] - 0.5) < 1e-9
    assert abs(prof[lr.slot_of(DAY.replace(hour=17, minute=30))] - 1.8) < 1e-9


def test_without_since_the_gas_days_would_be_wrong():
    """Guard: subtracting heating from pre-heat-pump days gives a too-low base."""
    since = DAY + timedelta(days=8, hours=6)
    house, heat = {}, {}
    for i in range(10 * 48):
        t = DAY + timedelta(minutes=30 * i)
        house[t] = true_base(t) + (1.2 if t >= since else 0.0)
        heat[t] = 1.2
    wrong, _ = lr.base_profile(house, heat)            # no since given
    right, _ = lr.base_profile(house, heat, heat_pump_since=since)
    assert sum(wrong) < sum(right) * 0.5


def test_not_enough_data():
    house = {DAY + timedelta(minutes=30 * i): 0.5 for i in range(20)}
    assert lr.base_profile(house, {}, heat_pump_since=DAY + timedelta(days=5), min_days=1) is None


def test_missing_slots_take_overall_mean():
    house = {DAY + timedelta(minutes=30 * i): (2.0 if i % 48 < 24 else 1.0) for i in range(48 * 3) if i % 48 != 40}
    prof, _ = lr.base_profile(house, {}, heat_pump_since=DAY + timedelta(days=30), min_days=1)
    assert abs(prof[40] - (2.0 * 24 + 1.0 * 23) / 47) < 1e-9
