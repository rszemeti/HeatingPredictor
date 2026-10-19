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
