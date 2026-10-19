"""Coordinator: fetch the forecast, run the model, hold the results."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CAPACITY,
    CONF_COP_CURVE,
    CONF_GAINS,
    CONF_HEAT_LOSS,
    CONF_HORIZON,
    CONF_INDOOR,
    CONF_MAX_OUTPUT_CURVE,
    CONF_PERIOD,
    CONF_PREHEAT,
    CONF_STANDBY,
    CONF_WEATHER,
    DEFAULT_HORIZON,
    DEFAULT_PERIOD,
    DOMAIN,
    STORAGE_VERSION,
)
from .model import (
    HouseParams,
    align_down,
    default_schedule,
    forecast_interpolator,
    parse_curve,
    schedule_temp,
    simulate,
    validate_schedule,
)

_LOGGER = logging.getLogger(__name__)


def _to_c(value: float, unit: str | None) -> float:
    if unit and unit != UnitOfTemperature.CELSIUS:
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
    return value


class HeatLoadCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Runs the heating load model on a schedule."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.period = int(entry.options.get(CONF_PERIOD, DEFAULT_PERIOD))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(minutes=min(15, self.period)),
        )
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.schedule: dict = default_schedule()

    # ---------------------------------------------------------------- schedule

    async def async_load_schedule(self) -> None:
        stored = await self._store.async_load()
        if stored:
            try:
                self.schedule = validate_schedule(stored)
            except (ValueError, KeyError, TypeError) as err:
                _LOGGER.warning("Stored schedule invalid (%s); using default", err)

    async def async_set_schedule(self, schedule: dict) -> dict:
        cleaned = validate_schedule(schedule)  # raises ValueError
        self.schedule = cleaned
        await self._store.async_save(cleaned)
        await self.async_refresh()
        return cleaned

    # ---------------------------------------------------------------- forecast

    async def _async_forecast_points(self) -> list[tuple[datetime, float]]:
        entity_id = self.config_entry.options[CONF_WEATHER]
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise UpdateFailed(f"{entity_id} is unavailable")
        unit = self.hass.config.units.temperature_unit
        points: list[tuple[datetime, float]] = []

        current = state.attributes.get("temperature")
        if current is not None:
            current_unit = state.attributes.get("temperature_unit", unit)
            points.append((dt_util.utcnow(), _to_c(float(current), current_unit)))

        forecast: list[dict] = []
        for kind in ("hourly", "twice_daily", "daily"):
            try:
                resp = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": entity_id, "type": kind},
                    blocking=True,
                    return_response=True,
                )
            except HomeAssistantError as err:
                _LOGGER.debug("%s forecast not available from %s: %s", kind, entity_id, err)
                continue
            forecast = (resp or {}).get(entity_id, {}).get("forecast") or []
            if forecast:
                if kind != "hourly":
                    _LOGGER.info("%s has no hourly forecast; using %s", entity_id, kind)
                break

        for item in forecast:
            when = dt_util.parse_datetime(str(item.get("datetime")))
            temp = item.get("temperature")
            if when is None or temp is None:
                continue
            points.append((dt_util.as_utc(when), _to_c(float(temp), unit)))
        if len(points) < 2:
            raise UpdateFailed(f"No usable forecast from {entity_id}")
        return points

    def _indoor_temp(self) -> float | None:
        entity_id = self.config_entry.options.get(CONF_INDOOR)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return _to_c(value, state.attributes.get("unit_of_measurement"))

    # ---------------------------------------------------------------- update

    def _params(self) -> HouseParams:
        o = self.config_entry.options
        return HouseParams(
            heat_loss_w_per_k=float(o[CONF_HEAT_LOSS]),
            thermal_capacity_kwh_per_k=float(o[CONF_CAPACITY]),
            internal_gains_w=float(o[CONF_GAINS]),
            standby_w=float(o[CONF_STANDBY]),
            cop_curve=parse_curve(o[CONF_COP_CURVE]),
            capacity_curve=parse_curve(o[CONF_MAX_OUTPUT_CURVE]),
            preheat=bool(o.get(CONF_PREHEAT, True)),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        points = await self._async_forecast_points()
        outdoor = forecast_interpolator(points)
        last_forecast = max(p[0] for p in points)

        now_local = dt_util.now()
        start = dt_util.as_utc(align_down(now_local, self.period))
        horizon_h = int(self.config_entry.options.get(CONF_HORIZON, DEFAULT_HORIZON))
        end = min(start + timedelta(hours=horizon_h), max(last_forecast, start) + timedelta(hours=1))
        periods = max(1, int((end - start).total_seconds() // (self.period * 60)))

        schedule = self.schedule

        def target(when: datetime) -> float:
            return schedule_temp(schedule, dt_util.as_local(when))

        indoor = self._indoor_temp()
        t_in0 = indoor if indoor is not None else target(start)

        results = simulate(
            start=start,
            periods=periods,
            period_minutes=self.period,
            outdoor_temp=outdoor,
            target_temp=target,
            t_in_start=t_in0,
            params=self._params(),
        )
        return summarise(results, now_local, self.period, indoor is not None)


def summarise(results: list, now_local: datetime, period: int, measured_start: bool) -> dict:
    """Totals the sensors expose, from a list of PeriodResult."""
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    in_24h = dt_util.as_utc(now_local) + timedelta(hours=24)
    rows = []
    rest_today = tomorrow_kwh = next_24h = 0.0
    peak_kw = 0.0
    tomorrow_periods = 0
    for r in results:
        day = dt_util.as_local(r.start).date()
        if day == today:
            rest_today += r.elec_kwh
        elif day == tomorrow:
            tomorrow_kwh += r.elec_kwh
            tomorrow_periods += 1
        if r.start < in_24h:
            next_24h += r.elec_kwh
            peak_kw = max(peak_kw, r.elec_kwh * 60 / period)
        row = r.as_dict()
        row["start"] = dt_util.as_local(r.start).isoformat()
        row.pop("end")
        rows.append(row)
    return {
        "periods": rows,
        "period_minutes": period,
        "current_kwh": results[0].elec_kwh if results else None,
        "rest_of_today_kwh": rest_today,
        "tomorrow_kwh": tomorrow_kwh,
        "tomorrow_complete": tomorrow_periods >= (24 * 60) // period - 1,
        "next_24h_kwh": next_24h,
        "peak_kw_24h": peak_kw,
        "start_temp_measured": measured_start,
        "updated": dt_util.utcnow().isoformat(),
    }
