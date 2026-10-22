"""Run examples/solis/battery_control.yaml against the real integration."""

import math
import pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.setup import async_setup_component

from custom_components.heat_load_predictor.const import DOMAIN

from .test_integration import BATTERY, OPTIONS

LON = ZoneInfo("Europe/London")
ROOT = pathlib.Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _rig(hass, freezer, day: datetime, *, soc, pv_peak, temp, with_soc=True):
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(day.replace(hour=0, minute=10))
    calls = []

    async def set_value(call):
        for e in call.data["entity_id"]:
            calls.append((e, call.data["value"]))
            hass.states.async_set(e, str(call.data["value"]))

    async def noop(call):
        calls.append(("push", None))

    async def log(call):
        calls.append(("log", call.data["message"]))

    async def forecasts(call):
        now = datetime.now(LON).replace(minute=0, second=0, microsecond=0)
        return {"weather.home": {"forecast": [
            {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": temp} for h in range(60)]}}

    async def select_option(call):
        for e in call.data["entity_id"]:
            calls.append((e, call.data["option"]))
            hass.states.async_set(e, call.data["option"])

    hass.services.async_register("number", "set_value", set_value)
    hass.services.async_register("select", "select_option", select_option)
    hass.services.async_register("script", "solis8_update_charge_discharge_times", noop)
    hass.services.async_register("system_log", "write", log)
    hass.services.async_register("weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY)
    hass.states.async_set("weather.home", "cloudy", {"temperature": temp})
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", "0")
    if with_soc:
        hass.states.async_set("sensor.battery_soc", str(soc), {"unit_of_measurement": "%"})
        hass.states.async_set("sensor.solis8_battery_soc", str(soc), {"unit_of_measurement": "%"})
    hass.states.async_set("select.solis8_energy_storage_control_switch", "Self-Use")
    hass.states.async_set("sensor.solcast_pv_forecast_forecast_remaining_today", "5")
    for name, d in (("sensor.solcast_today", day), ("sensor.solcast_tomorrow", day + timedelta(days=1))):
        slots = []
        for i in range(48):
            t = d + timedelta(minutes=30 * i)
            h = t.hour + t.minute / 60
            kw = pv_peak * math.sin(math.pi * (h - 6) / 14) if 6 <= h <= 20 else 0.0
            slots.append({"period_start": t, "pv_estimate": round(kw, 3)})
        hass.states.async_set(name, "1", {"detailedForecast": slots})

    await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Heat pump", data={}, options={**OPTIONS, **BATTERY})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    auto = yaml.safe_load((ROOT / "examples/solis/battery_control.yaml").read_text())
    assert await async_setup_component(hass, "automation", {"automation": [auto]})
    await hass.async_block_till_done()
    return calls


async def _at(hass, freezer, day, hh, mm):
    t = day.replace(hour=hh, minute=mm)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()


def _written(calls):
    out = {}
    for e, v in calls:
        if e.startswith("number."):
            out[e] = v
    return out


def _slot(hass, key):
    return hass.states.get("sensor.heat_pump_battery_plan").attributes["slots"][key]


async def test_winter_day(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 1, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0)

    await _at(hass, freezer, day, 0, 25)
    w = _written(calls)
    assert (w["number.solis8_timed_discharge_start_hours"], w["number.solis8_timed_discharge_start_minutes"]) == (0, 30)
    assert (w["number.solis8_timed_discharge_end_hours"], w["number.solis8_timed_discharge_end_minutes"]) == (5, 30)
    assert w["number.solis8_timed_charge_discharge_on_off"] & 64 == 0      # not worth discharging

    calls.clear()
    await _at(hass, freezer, day, 3, 50)
    w = _written(calls)
    assert (w["number.solis8_timed_charge_start_hours"], w["number.solis8_timed_charge_end_hours"]) == (4, 7)
    # What was written must match the decision the automation logged.
    log = next(m for e, m in calls if e == "log" and "charge_1" in m)
    on = "charge_1: ON" in log
    assert bool(w["number.solis8_timed_charge_discharge_on_off"] & 1) == on
    if on:
        assert f"to {w['number.solis8_timed_charge_soc']}%" in log

    # By evening the real battery would be low; refresh the plan with that.
    hass.states.async_set("sensor.battery_soc", "21", {"unit_of_measurement": "%"})
    await _at(hass, freezer, day, 21, 30)
    calls.clear()
    await _at(hass, freezer, day, 21, 50)
    w = _written(calls)
    assert (w["number.solis8_timed_charge_start_hours_3"], w["number.solis8_timed_charge_end_hours_3"],
            w["number.solis8_timed_charge_end_minutes_3"]) == (22, 23, 59)
    assert w["number.solis8_timed_charge_discharge_on_off"] & 4      # a cold night needs charge
    assert 20 < w["number.solis8_timed_charge_soc_3"] <= 100
    logs = [m for e, m in calls if e == "log"]
    assert logs and "Battery plan charge_3: ON" in logs[-1]


async def test_big_solar_day(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=95, pv_peak=12.0, temp=16.0)

    await _at(hass, freezer, day, 0, 25)
    w = _written(calls)
    assert w["number.solis8_timed_charge_discharge_on_off"] & 64       # discharge on
    assert w["number.solis8_timed_discharge_soc"] < 80

    calls.clear()
    await _at(hass, freezer, day, 3, 50)
    w = _written(calls)
    assert w["number.solis8_timed_charge_discharge_on_off"] & 1 == 0    # morning charge off: solar wins
    assert w["number.solis8_timed_charge_discharge_on_off"] & 64        # discharge left alone


async def test_plan_unavailable_falls_back(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 1, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0, with_soc=False)
    assert hass.states.get("sensor.heat_pump_battery_plan").state == "unavailable"
    await _at(hass, freezer, day, 0, 25)
    assert _written(calls)["number.solis8_timed_charge_discharge_on_off"] & 64 == 0
    calls.clear()
    await _at(hass, freezer, day, 3, 50)
    w = _written(calls)
    assert w["number.solis8_timed_charge_discharge_on_off"] & 1
    assert w["number.solis8_timed_charge_soc"] == 80
    assert "number.solis8_timed_charge_start_hours" not in w   # times left as they were



async def test_discharge_stops_at_target(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=95, pv_peak=12.0, temp=16.0)
    await _at(hass, freezer, day, 0, 25)
    target = _written(calls)["number.solis8_timed_discharge_soc"]
    assert int(hass.states.get("number.solis8_timed_charge_discharge_on_off").state) & 64

    # Still well above target: discharge continues.
    hass.states.async_set("sensor.solis8_battery_soc", str(target + 20))
    await _at(hass, freezer, day, 1, 0)
    assert int(hass.states.get("number.solis8_timed_charge_discharge_on_off").state) & 64

    # Reaches target inside the window: slot 1 switched off, other bits kept.
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", str(64 | 4))
    hass.states.async_set("sensor.solis8_battery_soc", str(target + 0.5))
    calls.clear()
    await _at(hass, freezer, day, 3, 2)
    assert int(hass.states.get("number.solis8_timed_charge_discharge_on_off").state) == 4
    assert any(e == "log" and "reached target" in m for e, m in calls)


async def test_discharge_check_ignored_outside_window(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    await _rig(hass, freezer, day, soc=95, pv_peak=12.0, temp=16.0)
    await _at(hass, freezer, day, 0, 25)
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", "64")
    hass.states.async_set("sensor.solis8_battery_soc", "10")
    await _at(hass, freezer, day, 9, 2)          # after 05:30: not this job's business
    assert hass.states.get("number.solis8_timed_charge_discharge_on_off").state == "64"


async def test_storage_mode(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    await _rig(hass, freezer, day, soc=50, pv_peak=12.0, temp=16.0)
    hass.states.async_set("sensor.solcast_pv_forecast_forecast_remaining_today", "30")   # > 16 kWh space
    await _at(hass, freezer, day, 9, 0)
    assert hass.states.get("select.solis8_energy_storage_control_switch").state == "Feed-in priority"
    hass.states.async_set("sensor.solcast_pv_forecast_forecast_remaining_today", "5")
    await _at(hass, freezer, day, 9, 30)
    assert hass.states.get("select.solis8_energy_storage_control_switch").state == "Self-Use"
