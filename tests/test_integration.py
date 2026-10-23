"""End-to-end tests against a real Home Assistant core."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.heat_load_predictor.const import DOMAIN
from custom_components.heat_load_predictor.model import default_schedule

OPTIONS = {
    "weather_entity": "weather.home",
    "heat_loss_w_per_k": 190,
    "thermal_capacity_kwh_per_k": 8,
    "internal_gains_w": 600,
    "standby_w": 15,
    "cop_curve": "-10:2.2, -7:2.4, -2:2.7, 2:2.9, 7:3.6, 12:4.3, 15:4.6",
    "max_output_curve": "-10:5.5, -7:6, 2:6.7, 7:8",
    "period_minutes": 30,
    "horizon_hours": 48,
    "preheat": True,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def fake_weather(hass: HomeAssistant):
    """A weather entity state plus a get_forecasts service: 2 °C all day."""
    hass.states.async_set("weather.home", "cloudy", {"temperature": 2.0})
    calls = []

    async def get_forecasts(call):
        calls.append(call.data)
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": 2.0}
                    for h in range(60)
                ]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY
    )
    return calls


async def _setup(hass, options=OPTIONS):
    await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Heat pump", data={}, options=dict(options))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_sensors(hass: HomeAssistant, fake_weather) -> None:
    await _setup(hass)
    assert fake_weather and fake_weather[0]["type"] == "hourly"

    cur = hass.states.get("sensor.heat_pump_heating_electricity_this_period")
    assert cur is not None
    assert cur.attributes["unit_of_measurement"] == "kWh"
    fc = cur.attributes["forecast"]
    assert fc and fc[0]["t_out"] == 2.0
    assert {"start", "kwh", "heat_kwh", "t_out", "t_in", "t_target", "cop"} <= set(fc[0])

    day = float(hass.states.get("sensor.heat_pump_heating_electricity_next_24h").state)
    # 2 °C, ~20 °C average: ~0.19*18-0.6 = 2.8 kW heat, COP 2.9 -> ~23 kWh/day.
    # Starting inside the overnight setback adds a recovery, so allow for it.
    assert 18 < day < 36
    tomorrow = hass.states.get("sensor.heat_pump_heating_electricity_tomorrow")
    assert float(tomorrow.state) > 15
    assert tomorrow.attributes["complete"] is True
    peak = float(hass.states.get("sensor.heat_pump_heating_peak_power_next_24h").state)
    assert 0.5 < peak < 3.5


async def test_service_and_websocket(hass: HomeAssistant, fake_weather, hass_ws_client) -> None:
    entry = await _setup(hass)
    resp = await hass.services.async_call(
        DOMAIN, "get_forecast", {}, blocking=True, return_response=True
    )
    assert resp["entry_id"] == entry.entry_id
    assert len(resp["periods"]) >= 90

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/schedule/get"})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["schedule"] == default_schedule()

    before = float(hass.states.get("sensor.heat_pump_heating_electricity_tomorrow").state)
    warmer = default_schedule()
    for pts in warmer["days"].values():
        for p in pts:
            p["temp"] += 1.0
    await client.send_json({"id": 2, "type": f"{DOMAIN}/schedule/set", "schedule": warmer})
    msg = await client.receive_json()
    assert msg["success"], msg
    await hass.async_block_till_done()
    after = float(hass.states.get("sensor.heat_pump_heating_electricity_tomorrow").state)
    assert after > before  # 1 K warmer costs more

    bad = default_schedule()
    bad["days"]["mon"][0]["temp"] = 99
    await client.send_json({"id": 3, "type": f"{DOMAIN}/schedule/set", "schedule": bad})
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "invalid_schedule"

    await client.send_json({"id": 4, "type": f"{DOMAIN}/forecast"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["periods"]


async def test_schedule_persists_across_reload(hass: HomeAssistant, fake_weather) -> None:
    entry = await _setup(hass)
    s = default_schedule()
    s["interpolation"] = "linear"
    await hass.services.async_call(DOMAIN, "set_schedule", {"schedule": s}, blocking=True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.schedule["interpolation"] == "linear"


async def test_indoor_sensor_used(hass: HomeAssistant, fake_weather) -> None:
    hass.states.async_set(
        "sensor.lounge", "16.0", {"unit_of_measurement": "°C", "device_class": "temperature"}
    )
    await _setup(hass, {**OPTIONS, "indoor_temp_entity": "sensor.lounge"})
    cur = hass.states.get("sensor.heat_pump_heating_electricity_this_period")
    assert cur.attributes["start_temp_measured"] is True


async def test_config_and_options_flow(hass: HomeAssistant, fake_weather) -> None:
    await async_setup_component(hass, "http", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    user = {"name": "Heat pump", **OPTIONS, "period_minutes": "30", "cop_curve": "nonsense"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["errors"] == {"cop_curve": "bad_curve"}
    user["cop_curve"] = "7:3.6,-10:2.2"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["period_minutes"] == 30
    assert result["options"]["cop_curve"] == "-10:2.2, 7:3.6"
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "heating"})
    assert result["type"] is FlowResultType.FORM
    new = {**entry.options, "period_minutes": "10"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], new)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    cur = hass.states.get("sensor.heat_pump_heating_electricity_this_period")
    assert cur.attributes["period_minutes"] == 10


async def test_weather_unavailable_retries(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "http", {})
    hass.states.async_set("weather.home", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, title="Heat pump", data={}, options=dict(OPTIONS))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY



async def test_card_registered_when_frontend_loads_later(hass: HomeAssistant, fake_weather) -> None:
    """On a real start-up the frontend may load after this integration."""
    from unittest.mock import patch

    from homeassistant.const import EVENT_COMPONENT_LOADED
    from homeassistant.core import Event
    from homeassistant.setup import ATTR_COMPONENT

    assert "frontend" not in hass.config.components
    from homeassistant.core import EventBus

    listeners = []
    real_listen = EventBus.async_listen

    def spy(bus, event_type, cb, *args, **kwargs):
        unsub = real_listen(bus, event_type, cb, *args, **kwargs)
        if event_type == EVENT_COMPONENT_LOADED and getattr(cb, "__name__", "") == "_on_loaded":
            listeners.append(cb)
        return unsub

    with (
        patch.object(EventBus, "async_listen", autospec=True, side_effect=spy),
        patch("custom_components.heat_load_predictor._add_card_js") as add,
    ):
        await _setup(hass)
        add.assert_not_called()
        assert len(listeners) == 1
        on_loaded = listeners[0]
        on_loaded(Event(EVENT_COMPONENT_LOADED, {ATTR_COMPONENT: "sensor"}))
        add.assert_not_called()
        on_loaded(Event(EVENT_COMPONENT_LOADED, {ATTR_COMPONENT: "frontend"}))
        add.assert_called_once_with(hass)


async def test_card_registered_when_frontend_already_loaded(hass: HomeAssistant, fake_weather) -> None:
    from unittest.mock import patch

    hass.config.components.add("frontend")
    with patch("custom_components.heat_load_predictor._add_card_js") as add:
        await _setup(hass)
        add.assert_called_once_with(hass)


async def test_card_message_while_waiting_for_weather(hass: HomeAssistant, hass_ws_client) -> None:
    await async_setup_component(hass, "http", {})
    hass.states.async_set("weather.home", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, title="Heat pump", data={}, options=dict(OPTIONS))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/schedule/get"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert "waiting to start" in msg["error"]["message"]
    assert "weather.home" in msg["error"]["message"]



# --------------------------------------------------------------------------
# Battery planner
# --------------------------------------------------------------------------

BATTERY = {
    "battery_enabled": True,
    "battery_soc_entity": "sensor.battery_soc",
    "solar_today_entity": "sensor.solcast_today",
    "solar_tomorrow_entity": "sensor.solcast_tomorrow",
    "battery_capacity_kwh": 32,
    "battery_min_soc": 15,
    "battery_reserve_soc": 20,
    "baseload_kw": 0.4,
    "export_limit_kw": 5,
    "charge_power_kw": 8,
    "discharge_power_kw": 5,
    "cheap_windows": "04:00-07:00, 13:00-16:00, 22:00-00:00",
    "discharge_windows": "00:30-05:30",
    "discharge_threshold_soc": 80,
    "solar_headroom_kwh": 2.6,
}


def _solcast(hass, peak_kw):
    import math as _m

    today = dt_util.start_of_local_day()
    for name, day in (("sensor.solcast_today", today), ("sensor.solcast_tomorrow", today + timedelta(days=1))):
        slots = []
        for i in range(48):
            t = day + timedelta(minutes=30 * i)
            h = t.hour + t.minute / 60
            kw = peak_kw * _m.sin(_m.pi * (h - 8) / 8) if 8 <= h <= 16 else 0.0
            slots.append({"period_start": t, "pv_estimate": round(kw, 3)})
        hass.states.async_set(name, "1", {"detailedForecast": slots})


async def test_battery_plan_sensor(hass: HomeAssistant, fake_weather) -> None:
    hass.states.async_set("sensor.battery_soc", "40", {"unit_of_measurement": "%"})
    _solcast(hass, 1.5)
    await _setup(hass, {**OPTIONS, **BATTERY})
    st = hass.states.get("sensor.heat_pump_battery_plan")
    assert st is not None and st.state != "unavailable", st
    a = st.attributes
    assert a["soc_now"] == 40 and a["solar_forecast"] is True
    # Every cheap window and the discharge window has a next occurrence.
    assert {"charge_1", "charge_2", "charge_3", "discharge_1"} <= set(a["slots"])
    for key in ("charge_1", "charge_2", "charge_3"):
        w = a["slots"][key]
        assert {"start", "end", "enabled", "target_soc", "reason"} <= set(w)
        assert 15 <= w["target_soc"] <= 100
    # A cold day with little sun: discharge isn't worth it, charging is.
    assert a["slots"]["discharge_1"]["enabled"] is False
    assert any(a["slots"][k]["enabled"] for k in ("charge_1", "charge_2", "charge_3"))
    assert st.state.startswith("Charge to ")
    low = hass.states.get("sensor.heat_pump_battery_lowest_soc_next_24h")
    assert 15 <= float(low.state) <= 100


async def test_battery_plan_unavailable_without_soc(hass: HomeAssistant, fake_weather) -> None:
    await _setup(hass, {**OPTIONS, **BATTERY})
    assert hass.states.get("sensor.heat_pump_battery_plan").state == "unavailable"
    # Heating sensors are unaffected.
    assert hass.states.get("sensor.heat_pump_heating_electricity_next_24h").state != "unavailable"


async def test_battery_sensors_absent_when_disabled(hass: HomeAssistant, fake_weather) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.heat_pump_battery_plan") is None


async def test_battery_options_step(hass: HomeAssistant, fake_weather) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "battery"
    bad = {**BATTERY, "cheap_windows": "4am to 7am"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"cheap_windows": "bad_windows"}
    good = {**BATTERY, "cheap_windows": "04:00-07:00,13:00-16:00, 22:00-24:00"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], good)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cheap_windows"] == "04:00-07:00, 13:00-16:00, 22:00-00:00"
    assert entry.options["heat_loss_w_per_k"] == 190   # heating settings kept


async def test_forecast_consistent_between_runs_without_indoor_sensor(hass: HomeAssistant, fake_weather) -> None:
    """Later runs start from the previous run's modelled indoor temperature."""
    entry = await _setup(hass)
    coord = entry.runtime_data
    first = {r["start"]: r["kwh"] for r in coord.data["periods"]}
    await coord.async_refresh()
    second = {r["start"]: r["kwh"] for r in coord.data["periods"]}
    common = sorted(set(first) & set(second))
    assert common
    assert all(abs(first[k] - second[k]) < 1e-6 for k in common)


async def test_battery_plan_appears_when_soc_sensor_arrives(hass: HomeAssistant, fake_weather, caplog) -> None:
    """Start-up order: the inverter's SOC sensor turns up after the integration."""
    await _setup(hass, {**OPTIONS, **BATTERY})
    assert hass.states.get("sensor.heat_pump_battery_plan").state == "unavailable"
    assert "Battery plan unavailable: battery SOC from sensor.battery_soc is missing" in caplog.text

    hass.states.async_set("sensor.battery_soc", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.heat_pump_battery_plan").state == "unavailable"

    hass.states.async_set("sensor.battery_soc", "55", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    st = hass.states.get("sensor.heat_pump_battery_plan")
    assert st.state != "unavailable", st
    assert st.attributes["soc_now"] == 55


async def test_pessimistic_solar_used_for_charge_sizing(hass: HomeAssistant, fake_weather) -> None:
    """With Solcast's pv_estimate10 present, charges are sized on it."""
    import math as _m

    hass.states.async_set("sensor.battery_soc", "30", {"unit_of_measurement": "%"})
    today = dt_util.start_of_local_day()

    def slots(day, low_factor):
        out = []
        for i in range(48):
            t = day + timedelta(minutes=30 * i)
            h = t.hour + t.minute / 60
            kw = 4.0 * _m.sin(_m.pi * (h - 8) / 8) if 8 <= h <= 16 else 0.0
            out.append({"period_start": t, "pv_estimate": round(kw, 3),
                        "pv_estimate10": round(kw * low_factor, 3), "pv_estimate90": round(kw * 1.2, 3)})
        return out

    for name, day in (("sensor.solcast_today", today), ("sensor.solcast_tomorrow", today + timedelta(days=1))):
        hass.states.async_set(name, "1", {"detailedForecast": slots(day, 0.3)})

    entry = await _setup(hass, {**OPTIONS, **BATTERY, "solar_confidence": "expected", "load_margin_pct": 0})
    a = hass.states.get("sensor.heat_pump_battery_plan").attributes
    expected_1 = a["slots"]["charge_1"]["target_soc"]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "solar_confidence": "pessimistic"})
    await hass.async_block_till_done()
    a = hass.states.get("sensor.heat_pump_battery_plan").attributes
    cautious_soc = [r["soc"] for r in a["soc_forecast"]]
    # Cautious sizing never lets the battery fall below the arrival reserve...
    assert min(cautious_soc) >= 20 - 0.5, min(cautious_soc)
    # ...and charges more in the morning window, whose gap relies on solar.
    assert a["slots"]["charge_1"]["target_soc"] > expected_1


async def test_plan_recommends_storage_mode(hass: HomeAssistant, fake_weather) -> None:
    hass.states.async_set("sensor.battery_soc", "40", {"unit_of_measurement": "%"})
    _solcast(hass, 1.5)                      # small solar: never above the export limit
    await _setup(hass, {**OPTIONS, **BATTERY})
    a = hass.states.get("sensor.heat_pump_battery_plan").attributes
    assert a["storage_mode"] == "Self-Use"

