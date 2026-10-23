"""Learning the baseload against a real recorder database."""

from datetime import datetime, timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .test_integration import BATTERY, OPTIONS, _setup, fake_weather  # noqa: F401


@pytest.fixture(autouse=True)
def recorder_first(recorder_mock, enable_custom_integrations):
    """The recorder must be set up before Home Assistant starts."""
    yield


async def test_learns_baseload_from_recorder(hass: HomeAssistant, fake_weather, freezer) -> None:
    """Real recorder: 10 days of hourly house-load statistics (heat pump from
    day 9) and outdoor temperature history; the learned profile recovers the
    true baseload, including the teatime peak."""
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    await hass.config.async_set_time_zone("Europe/London")
    tz = dt_util.get_default_time_zone()
    now = datetime(2026, 10, 23, 18, 5, tzinfo=tz)
    start = now.replace(minute=0) - timedelta(days=10)
    since = (now - timedelta(hours=36)).replace(minute=0)

    # Outdoor temperature history: write the weather entity's state over time.
    t = start - timedelta(hours=1)
    while t < now:
        freezer.move_to(t)
        hass.states.async_set("weather.home", "cloudy", {"temperature": 8.0 + (t.hour % 6) * 0.1})
        t += timedelta(hours=3)
    freezer.move_to(now)
    hass.states.async_set("weather.home", "cloudy", {"temperature": 2.0})
    await async_wait_recording_done(hass)

    # House load: true base (0.5 kW, 1.8 kW 17:00-19:00) + heat pump after `since`.
    stats = []
    t = start
    while t < now.replace(minute=0):
        base = 1.8 if 17 <= t.hour < 19 else 0.5
        hp = 1.0 if t >= since else 0.0
        v = base + hp
        stats.append({"start": t, "mean": v, "min": v, "max": v})
        t += timedelta(hours=1)
    async_import_statistics(hass, {
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None, "source": "recorder",
        "statistic_id": "sensor.house_load", "unit_class": "power", "unit_of_measurement": "kW"}, stats)
    await async_wait_recording_done(hass)

    hass.states.async_set("sensor.battery_soc", "50", {"unit_of_measurement": "%"})
    entry = await _setup(hass, {**OPTIONS, **BATTERY, "house_load_entity": "sensor.house_load",
                                "heat_pump_since": since.strftime("%Y-%m-%d %H:%M:%S"), "learn_days": 10})
    a = hass.states.get("sensor.heat_pump_battery_plan").attributes
    assert a["baseload_from"].startswith("learned from"), a["baseload_from"]
    prof = a["baseload_profile"]
    # Pre-heat-pump days dominate and are exact; the last 36 h subtract the
    # model's heating, so allow some slack there.
    assert abs(prof[6] - 0.5) < 0.25, prof[6]        # 03:00
    assert abs(prof[35] - 1.8) < 0.35, prof[35]      # 17:30
    assert prof[35] > prof[6] + 1.0
