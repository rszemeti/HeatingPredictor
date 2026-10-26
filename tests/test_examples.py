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


async def _rig(hass, freezer, day: datetime, *, soc, pv_peak, temp, with_soc=True, variables=None):
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
    auto["variables"].update(variables or {})
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


# Tests written for one inverter slot per window.
MULTI = {"use_single_slot": False}


def _slot(hass, key):
    return hass.states.get("sensor.heat_pump_battery_plan").attributes["slots"][key]


async def test_winter_day(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 1, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0, variables=MULTI)

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
    on = "): ON" in log
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
    assert logs and "Battery plan charge_3 ->" in logs[-1] and "): ON" in logs[-1]


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
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0, with_soc=False, variables=MULTI)
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


async def test_storage_mode_follows_plan(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    await _rig(hass, freezer, day, soc=50, pv_peak=12.0, temp=16.0)
    for hh, mm in ((7, 0), (9, 0), (13, 30), (17, 0)):
        # The plan refreshes on its own timer; the automation acts on whichever
        # plan was current when it fired, so accept the plan either side.
        before = hass.states.get("sensor.heat_pump_battery_plan").attributes["storage_mode"]
        await _at(hass, freezer, day, hh, mm)
        after = hass.states.get("sensor.heat_pump_battery_plan").attributes["storage_mode"]
        state = hass.states.get("select.solis8_energy_storage_control_switch").state
        assert state in (before, after), (hh, before, after, state)
        # Ten minutes on the plan has settled; next half-hour it must match.
        await _at(hass, freezer, day, hh, mm + 10)
        planned = hass.states.get("sensor.heat_pump_battery_plan").attributes["storage_mode"]
        await _at(hass, freezer, day, hh, mm + 29)
        await _at(hass, freezer, day, hh + (mm + 30) // 60, (mm + 30) % 60)
        planned = hass.states.get("sensor.heat_pump_battery_plan").attributes["storage_mode"]
        assert hass.states.get("select.solis8_energy_storage_control_switch").state == planned, (hh, planned)


async def test_storage_mode_fallback_without_plan(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    await _rig(hass, freezer, day, soc=50, pv_peak=12.0, temp=16.0, with_soc=False)
    hass.states.async_set("sensor.solis8_battery_soc", "50")
    hass.states.async_set("sensor.solcast_pv_forecast_forecast_remaining_today", "30")   # > 16 kWh space
    await _at(hass, freezer, day, 9, 0)
    assert hass.states.get("select.solis8_energy_storage_control_switch").state == "Feed-in priority"
    hass.states.async_set("sensor.solcast_pv_forecast_forecast_remaining_today", "5")
    await _at(hass, freezer, day, 9, 30)
    assert hass.states.get("select.solis8_energy_storage_control_switch").state == "Self-Use"


def _rates_attr(day, cheap):
    out = []
    for i in range(48):
        s = day + timedelta(minutes=30 * i)
        h = s.hour + s.minute / 60
        price = 0.135 if any(a <= h < b for a, b in cheap) else 0.27
        out.append({"start": s, "end": s + timedelta(minutes=30), "value_inc_vat": price,
                    "is_capped": False, "is_intelligent_adjusted": False})
    return out


async def test_windows_follow_octopus_rates(hass: HomeAssistant, freezer) -> None:
    """A tariff with moved cheap windows: plan, update time and inverter slots follow."""
    from homeassistant.config_entries import ConfigEntryState  # noqa: F401

    day = datetime(2026, 1, 15, tzinfo=LON)
    moved = ((3, 6), (12, 14.5), (21, 23))
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0, variables=MULTI)
    hass.states.async_set("event.octopus_today", "2026-01-15", {"rates": _rates_attr(day, moved)})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "rates_today_entity": "event.octopus_today"})
    await hass.async_block_till_done()

    plan = hass.states.get("sensor.heat_pump_battery_plan").attributes
    assert plan["charge_windows_from"] == "tariff"
    c1 = plan["slots"]["charge_1"]
    assert (c1["start"][11:16], c1["end"][11:16]) == ("03:00", "06:00")
    assert plan["slots"]["charge_2"]["end"][11:16] == "14:30"
    nxt = hass.states.get("sensor.heat_pump_battery_next_inverter_update")
    assert nxt.state.startswith("2026-01-15T00:20")            # discharge window first (00:30)

    await _at(hass, freezer, day, 0, 25)
    assert hass.states.get("sensor.heat_pump_battery_next_inverter_update").state.startswith("2026-01-15T02:50")
    calls.clear()
    await _at(hass, freezer, day, 2, 50)
    w = _written(calls)
    assert (w["number.solis8_timed_charge_start_hours"], w["number.solis8_timed_charge_end_hours"]) == (3, 6)
    calls.clear()
    await _at(hass, freezer, day, 11, 50)
    w = _written(calls)
    assert (w["number.solis8_timed_charge_start_hours_2"], w["number.solis8_timed_charge_end_hours_2"],
            w["number.solis8_timed_charge_end_minutes_2"]) == (12, 14, 30)
    # Tomorrow has no published rates yet: the fixed (Cosy) windows are used.
    tomorrow = [x for x in hass.states.get("sensor.heat_pump_battery_plan").attributes["windows"]
                if x["kind"] == "charge" and x["start"].startswith("2026-01-16")]
    assert tomorrow and tomorrow[0]["start"][11:16] == "04:00"


async def test_running_charge_window_is_rewritten_when_plan_changes(hass: HomeAssistant, freezer) -> None:
    """Regression (23-24 Oct): the 22:00 slot was written 'off' at 21:50 and
    nothing looked at it again. If the plan changes while a charge window is
    running, the automation writes the new decision within 5 minutes."""
    day = datetime(2026, 10, 23, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=60, pv_peak=1.0, temp=10.0, variables=MULTI)
    await _at(hass, freezer, day, 21, 50)
    # Pretend the inverter holds a stale 'off' for slot 3 (as 0.3.2 wrote it).
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", "0")
    calls.clear()
    await _at(hass, freezer, day, 22, 5)
    w = _slot(hass, "charge_3")
    assert w["enabled"], w["reason"]
    written = _written(calls)
    assert written["number.solis8_timed_charge_discharge_on_off"] & 4
    assert written["number.solis8_timed_charge_soc_3"] == w["target_soc"]
    # Once it matches, the recheck leaves the inverter alone.
    calls.clear()
    await _at(hass, freezer, day, 22, 10)
    assert not _written(calls)


async def test_card_gets_storage_mode_runs(hass: HomeAssistant, freezer) -> None:
    """The card's forecast carries the plan's storage modes as runs, matching
    the plan sensor's current mode, and they aren't sensor attributes."""
    import json
    import os

    day = datetime(2026, 6, 15, tzinfo=LON)
    await _rig(hass, freezer, day, soc=50, pv_peak=12.0, temp=16.0)
    await _at(hass, freezer, day, 6, 0)
    from custom_components.heat_load_predictor import _coordinator, _response

    # What the card's forecast websocket call returns.
    result = json.loads(json.dumps(_response(_coordinator(hass, None)), default=str))
    runs = result["battery"]["mode_runs"]
    assert runs and {r["mode"] for r in runs} <= {"Self-Use", "Feed-in priority"}
    assert any(r["mode"] == "Feed-in priority" for r in runs)       # a 12 kW day
    for a, b in zip(runs, runs[1:]):
        assert a["end"] == b["start"] and a["mode"] != b["mode"]
    plan = hass.states.get("sensor.heat_pump_battery_plan").attributes
    assert runs[0]["mode"] == plan["storage_mode"]
    assert "mode_runs" not in plan
    if out := os.environ.get("DUMP_CARD_DATA"):
        pathlib.Path(out).write_text(json.dumps(result))


async def test_inverter_on_utc_in_summer(hass: HomeAssistant, freezer) -> None:
    """An inverter left on GMT/UTC all year: in summer (BST) the 22:00-00:00
    window is written as 21:00-23:00 inverter time."""
    day = datetime(2026, 6, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=30, pv_peak=1.0, temp=12.0,
                       variables={**MULTI, "inverter_uses_local_time": False})
    await _at(hass, freezer, day, 21, 50)
    w = _written(calls)
    assert (w["number.solis8_timed_charge_start_hours_3"], w["number.solis8_timed_charge_start_minutes_3"]) == (21, 0)
    assert (w["number.solis8_timed_charge_end_hours_3"], w["number.solis8_timed_charge_end_minutes_3"]) == (23, 0)


async def test_inverter_on_local_time_in_summer(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 6, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=30, pv_peak=1.0, temp=12.0, variables=MULTI)
    await _at(hass, freezer, day, 21, 50)
    w = _written(calls)
    assert (w["number.solis8_timed_charge_start_hours_3"], w["number.solis8_timed_charge_end_hours_3"],
            w["number.solis8_timed_charge_end_minutes_3"]) == (22, 23, 59)


async def test_gas_rates_entity_is_ignored(hass: HomeAssistant, freezer, caplog) -> None:
    """A gas rates event set by mistake (gas is far cheaper per kWh) would make
    the whole day look cheap. It's ignored, with a warning."""
    day = datetime(2026, 1, 15, tzinfo=LON)
    await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0)
    elec = ((4, 7), (13, 16), (22, 24))
    gas = [{"start": day + timedelta(minutes=30 * i), "end": day + timedelta(minutes=30 * (i + 1)),
            "value_inc_vat": 0.06, "is_capped": False} for i in range(48)]
    hass.states.async_set("event.octopus_energy_electricity_x_current_day_rates", "1", {"rates": _rates_attr(day, elec)})
    hass.states.async_set("event.octopus_energy_gas_y_next_day_rates", "1", {"rates": gas})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(entry, options={
        **entry.options,
        "rates_today_entity": "event.octopus_energy_electricity_x_current_day_rates",
        "rates_tomorrow_entity": "event.octopus_energy_gas_y_next_day_rates"})
    await hass.async_block_till_done()
    plan = hass.states.get("sensor.heat_pump_battery_plan").attributes
    c1 = plan["slots"]["charge_1"]
    assert (c1["start"][11:16], c1["end"][11:16]) == ("04:00", "07:00")     # not the whole day
    assert "gas tariff" in caplog.text



async def test_single_slot_carries_every_window(hass: HomeAssistant, freezer) -> None:
    """Single-slot mode: each cheap window goes into charge slot 1 just before
    it starts; slots 2 and 3 are never written. (Bits left alone here.)"""
    day = datetime(2026, 1, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0,
                       variables={"clear_unused_slots": False})
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", str(2 | 4))   # stale 2 and 3 bits
    seen = {}
    for hh, mm, start in ((3, 50, 4), (12, 50, 13), (21, 50, 22)):
        calls.clear()
        await _at(hass, freezer, day, hh, mm)
        w = _written(calls)
        assert not any(e.endswith(("_2", "_3")) for e in w), w
        assert w["number.solis8_timed_charge_start_hours"] == start
        mask = w["number.solis8_timed_charge_discharge_on_off"]
        assert mask & 6 == 6                                   # left as they were
        seen[start] = (w.get("number.solis8_timed_charge_soc"), bool(mask & 1))
    assert seen[22][1]                                         # a cold night charges
    assert w["number.solis8_timed_charge_end_hours"] == 23 and w["number.solis8_timed_charge_end_minutes"] == 59


async def test_single_slot_can_clear_unused_slots(hass: HomeAssistant, freezer) -> None:
    day = datetime(2026, 1, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0)   # default: clear
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", str(2 | 4 | 64))
    calls.clear()
    await _at(hass, freezer, day, 3, 50)
    mask = _written(calls)["number.solis8_timed_charge_discharge_on_off"]
    assert mask & 62 == 0 and mask & 64                        # 2-6 off, discharge bit untouched


async def test_single_slot_recheck_rewrites_slot_1(hass: HomeAssistant, freezer) -> None:
    """A running window whose slot 1 holds another window's times is rewritten."""
    day = datetime(2026, 1, 15, tzinfo=LON)
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0)
    await _at(hass, freezer, day, 21, 50)
    hass.states.async_set("number.solis8_timed_charge_start_hours", "13")      # stale
    calls.clear()
    await _at(hass, freezer, day, 22, 5)
    assert _written(calls)["number.solis8_timed_charge_start_hours"] == 22


async def test_discharge_stop_ignores_a_disabled_discharge(hass: HomeAssistant, freezer) -> None:
    """24-26 Oct: a stale discharge bit with the plan's discharge off made the
    discharge-stop job fire every 2 minutes. It now needs the plan to enable it."""
    day = datetime(2026, 1, 15, tzinfo=LON)                    # winter: discharge not worth it
    calls = await _rig(hass, freezer, day, soc=40, pv_peak=1.5, temp=1.0)
    await _at(hass, freezer, day, 0, 25)
    assert not _slot(hass, "discharge_1")["enabled"]
    hass.states.async_set("number.solis8_timed_charge_discharge_on_off", "64")
    calls.clear()
    await _at(hass, freezer, day, 1, 2)
    assert not any(e == "push" for e, _ in calls)
    assert hass.states.get("number.solis8_timed_charge_discharge_on_off").state == "64"
