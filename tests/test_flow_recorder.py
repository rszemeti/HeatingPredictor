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
    assert not calls                                   # advisory: nothing written
