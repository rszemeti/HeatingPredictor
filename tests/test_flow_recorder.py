"""Flow planning against a real recorder: learns from heat pump statistics."""

import math
import random
from datetime import datetime, timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .test_integration import OPTIONS, _setup, fake_weather  # noqa: F401
from .test_flowlaw import _synthetic, fl


@pytest.fixture(autouse=True)
def recorder_first(recorder_mock, enable_custom_integrations):
    yield


async def test_learns_house_from_heat_pump_statistics(hass: HomeAssistant, fake_weather, freezer) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    await hass.config.async_set_time_zone("Europe/London")
    tz = dt_util.get_default_time_zone()
    now = datetime(2026, 11, 8, 12, 5, tzinfo=tz)
    synth = _synthetic(days=6, h=210.0)
    first = min(synth)
    shift = now.replace(minute=0) - timedelta(days=6) - first

    def series(attr, scale=1.0):
        hours = {}
        for t, smp in synth.items():
            h = (t + shift).replace(minute=0)
            hours.setdefault(h, []).append(getattr(smp, attr) * scale)
        return [{"start": h, "mean": sum(v) / len(v), "min": min(v), "max": max(v)}
                for h, v in sorted(hours.items())]

    for eid, attr, unit, cls, scale in (
        ("sensor.hp_heat", "heat_kw", "W", "power", 1000.0),
        ("sensor.hp_elec", "elec_kw", "W", "power", 1000.0),
        ("sensor.hp_flow", "flow", "°C", "temperature", 1.0),
        ("sensor.hp_return", "ret", "°C", "temperature", 1.0),
        ("sensor.hp_room", "room", "°C", "temperature", 1.0),
        ("sensor.hp_outdoor", "out", "°C", "temperature", 1.0),
    ):
        async_import_statistics(hass, {
            "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None,
            "source": "recorder", "statistic_id": eid, "unit_class": cls,
            "unit_of_measurement": unit}, series(attr, scale))
    await async_wait_recording_done(hass)
    freezer.move_to(now)

    calls = []
    hass.services.async_register("number", "set_value", lambda call: calls.append(call))
    await _setup(hass, {**OPTIONS,
                        "hp_heat_entity": "sensor.hp_heat", "hp_power_entity": "sensor.hp_elec",
                        "hp_flow_entity": "sensor.hp_flow", "hp_return_entity": "sensor.hp_return",
                        "hp_room_entity": "sensor.hp_room", "hp_outdoor_entity": "sensor.hp_outdoor"})
    st = hass.states.get("sensor.heat_pump_flow_target")
    assert st is not None, [s.entity_id for s in hass.states.async_all("sensor")]
    a = st.attributes
    learned = a["learned"]
    # Hourly means blur the emitter fit a little; still close.
    assert learned["emitter_k"] == pytest.approx(0.065, rel=0.1), learned
    assert learned["heat_loss_w_per_k"] == pytest.approx(210, rel=0.15), learned
    assert a["emitter_k_from"] == "learned" and a["heat_loss_from"] == "learned"
    assert len(a["plan"]) > 40
    assert all(p["flow"] is None or 25 <= p["flow"] <= 55 for p in a["plan"])
    # The curve learned straight from flow / room / outdoor history.
    assert a["curve_from"] == "history", a["learn_status"]
    assert a["learn_status"].startswith("learned from"), a["learn_status"]
    law = {p["outdoor"]: p["flow"] for p in a["water_law"]}
    assert law[0] > law[10] > law[15]
    # "Download planning data" carries the flow plan and recent history.
    from custom_components.heat_load_predictor.const import DOMAIN as _D
    coord = hass.config_entries.async_entries(_D)[0].runtime_data
    await async_wait_recording_done(hass)
    fh = await coord.async_flow_history()
    assert fh["step_minutes"] == 5 and len(fh["series"]["room"]) == 48 * 12
    assert fh["entities"]["planned_flow"] == "sensor.heat_pump_flow_target"
    assert any(v is not None for v in fh["series"]["planned_flow"]), fh["series"]["planned_flow"][-5:]

    # Daily COP from the same statistics.
    cop = hass.states.get("sensor.heat_pump_cop_yesterday")
    assert cop is not None and cop.state not in ("unknown", "unavailable"), cop
    days = cop.attributes["days"]
    assert 1 <= len(days) <= 7 and all(not d["today"] for d in days)
    full = [d for d in days if d["hours"] == 24]
    assert full, days
    for d in full:
        assert d["cop"] == pytest.approx(d["heat_kwh"] / d["elec_kwh"], rel=0.03)
        assert 1.5 < d["cop"] < 6
    assert float(cop.state) == days[-1]["cop"]
    assert not calls                                   # advisory: nothing written


async def test_download_has_what_the_battery_actually_did(hass: HomeAssistant, fake_weather, freezer) -> None:
    """13 Nov: the download had only the plan forward, nothing of what the
    battery, solar and storage mode actually did that day."""
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
    from .test_integration import BATTERY, _solcast

    start = dt_util.utcnow().replace(second=0, microsecond=0)
    freezer.move_to(start)
    hass.states.async_set("sensor.battery_soc", "40", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.house_load", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.pv_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("select.storage_mode", "Self-Use")
    _solcast(hass, 4.0)
    entry = await _setup(hass, {**OPTIONS, **BATTERY, "house_load_entity": "sensor.house_load",
                                "solar_power_entity": "sensor.pv_power",
                                "storage_mode_entity": "select.storage_mode"})
    coord = entry.runtime_data
    for k, (soc, pv, mode) in enumerate([(45, 2100, "Self-Use"), (55, 3900, "Feed-in priority"),
                                         (60, 4200, "Feed-in priority"), (66, 2500, "Self-Use")]):
        freezer.tick(timedelta(minutes=30))
        hass.states.async_set("sensor.battery_soc", str(soc), {"unit_of_measurement": "%"})
        hass.states.async_set("sensor.pv_power", str(pv), {"unit_of_measurement": "W"})
        hass.states.async_set("select.storage_mode", mode)
        await coord.async_refresh()
    await async_wait_recording_done(hass)
    bh = await coord.async_battery_history()
    s = bh["series"]
    assert bh["step_minutes"] == 5 and len(s["battery_soc"]) == 48 * 12
    assert s["battery_soc"][-1] == 66 and 55 in s["battery_soc"]
    assert s["solar_power"][-1] == 2500
    assert "Feed-in priority" in s["inverter_mode"] and s["inverter_mode"][-1] == "Self-Use"
    assert s["house_load"][-1] == 0.5
    assert set(v for v in s["planned_mode"] if v) <= {"Self-Use", "Feed-in priority"}
    assert s["planned_mode"][-1] in ("Self-Use", "Feed-in priority")
    assert bh["entities"]["planned_mode"].endswith("(storage_mode)")


async def test_plan_data_download_includes_battery_history(hass: HomeAssistant, fake_weather, hass_ws_client) -> None:
    from .test_integration import BATTERY, _solcast

    hass.states.async_set("sensor.battery_soc", "40", {"unit_of_measurement": "%"})
    _solcast(hass, 4.0)
    await _setup(hass, {**OPTIONS, **BATTERY})
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "heat_load_predictor/plan_data"})
    msg = await client.receive_json()
    assert msg["success"], msg
    bh = msg["result"]["battery_history"]
    assert "error" not in bh, bh
    assert set(bh["series"]) == {"battery_soc", "planned_mode"}
    assert bh["series"]["battery_soc"][-1] == 40
