"""Coordinator: fetch the forecast, run the model, hold the results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    BATTERY_DEFAULTS,
    CONF_BASELOAD,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_KWH,
    CONF_CHARGE_KW,
    CONF_CHEAP_WINDOWS,
    CONF_DISCHARGE_KW,
    CONF_DISCHARGE_THRESHOLD,
    CONF_DISCHARGE_WINDOWS,
    CONF_EXPORT_LIMIT,
    CONF_MIN_SOC,
    CONF_RESERVE_SOC,
    CONF_SOC_ENTITY,
    CONF_CHEAP_TOLERANCE,
    CONF_LOAD_MARGIN,
    CONF_RATES_TODAY,
    CONF_RATES_TOMORROW,
    CONF_SOLAR_CONFIDENCE,
    CONF_SOLAR_HEADROOM,
    CONF_SOLAR_TODAY,
    CONF_SOLAR_TOMORROW,
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
    SOLAR_CONFIDENCE_FIELD,
    WRITE_LEAD_MINUTES,
    STORAGE_VERSION,
)
from .planner import BatteryParams, cheap_ranges, make_plan, occurrences, parse_windows
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
        # Modelled indoor temperature from the last run, (UTC time, °C), used
        # as the starting point when there's no indoor sensor.
        self._last_tin: list[tuple[datetime, float]] = []
        self._battery_error: str | None = None

    # ---------------------------------------------------------------- schedule

    @callback
    def async_watch_battery_inputs(self) -> Callable[[], None]:
        """Rebuild a failed battery plan as soon as its inputs come back.

        At start-up the inverter's SOC sensor (or the solar forecast) may not
        exist yet; rather than wait for the next scheduled update, refresh when
        one of them changes while the plan is unavailable.
        """
        o = self.config_entry.options
        ids = [o.get(k) for k in (CONF_SOC_ENTITY, CONF_SOLAR_TODAY, CONF_SOLAR_TOMORROW)]
        ids = [i for i in ids if i]
        if not o.get(CONF_BATTERY_ENABLED) or not ids:
            return lambda: None

        soc_id = o.get(CONF_SOC_ENTITY)

        @callback
        def _changed(event: Event) -> None:
            plan = (self.data or {}).get("battery")
            if plan is not None and "error" not in plan:
                return
            new = event.data.get("new_state")
            if event.data.get("entity_id") == soc_id:
                try:
                    float(new.state if new else "x")
                except ValueError:
                    return  # still not a number; wait for one
            self.hass.async_create_task(self.async_request_refresh())

        return async_track_state_change_event(self.hass, ids, _changed)

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
        if indoor is not None:
            t_in0 = indoor
        else:
            t_in0 = self._carried_tin(start)
            if t_in0 is None:
                t_in0 = target(start)

        results = simulate(
            start=start,
            periods=periods,
            period_minutes=self.period,
            outdoor_temp=outdoor,
            target_temp=target,
            t_in_start=t_in0,
            params=self._params(),
        )
        self._last_tin = [(results[0].start, results[0].t_in_start)] + [
            (r.end, r.t_in_end) for r in results
        ]
        data = summarise(results, now_local, self.period, indoor is not None)
        o = self.config_entry.options
        if o.get(CONF_BATTERY_ENABLED):
            data["battery"] = self._plan_battery(results, now_local)
        return data

    def _carried_tin(self, when: datetime) -> float | None:
        """Previous run's modelled indoor temperature at `when`, if it covers it."""
        pts = self._last_tin
        if len(pts) < 2 or not pts[0][0] <= when <= pts[-1][0]:
            return None
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= when <= t1:
                span = (t1 - t0).total_seconds()
                return v0 if span <= 0 else v0 + (v1 - v0) * (when - t0).total_seconds() / span
        return None

    # ---------------------------------------------------------------- battery

    def _solar_slots(self, field: str = "pv_estimate") -> tuple[list[tuple[datetime, float]], bool]:
        """Solcast-style half-hour slots (start, kW) from today/tomorrow sensors.

        `field` picks the estimate; if a slot lacks it, the median is used.
        """
        slots: list[tuple[datetime, float]] = []
        found = False
        for key in (CONF_SOLAR_TODAY, CONF_SOLAR_TOMORROW):
            entity_id = self.config_entry.options.get(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            items = (state.attributes.get("detailedForecast") if state else None) or []
            for item in items:
                start = item.get("period_start")
                when = start if isinstance(start, datetime) else dt_util.parse_datetime(str(start))
                if when is None:
                    continue
                try:
                    kw = float(item.get(field, item.get("pv_estimate", 0)) or 0)
                except (TypeError, ValueError):
                    continue
                slots.append((dt_util.as_utc(when), kw))
                found = True
        return slots, found

    def _rates(self) -> list[tuple[datetime, datetime, float]]:
        """(start, end, price) periods from Octopus Energy rate event entities."""
        out: list[tuple[datetime, datetime, float]] = []
        for key in (CONF_RATES_TODAY, CONF_RATES_TOMORROW):
            entity_id = self.config_entry.options.get(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            for r in (state.attributes.get("rates") if state else None) or []:
                try:
                    start, end = (
                        v if isinstance(v, datetime) else dt_util.parse_datetime(str(v))
                        for v in (r["start"], r["end"])
                    )
                    price = float(r["value_inc_vat"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start and end:
                    out.append((dt_util.as_local(start), dt_util.as_local(end), price))
        return sorted(set(out))

    def _charge_ranges(
        self, o: dict, first: datetime, last: datetime
    ) -> tuple[list[tuple[datetime, datetime]] | None, str]:
        """Cheap windows from the tariff, with the fixed windows beyond its rates."""
        rates = self._rates()
        if not rates:
            return None, "fixed"
        ranges = cheap_ranges(rates, float(o[CONF_CHEAP_TOLERANCE]))
        covered_until = max(e for _, e, _ in rates)
        if covered_until < last:
            fixed = occurrences(parse_windows(o[CONF_CHEAP_WINDOWS]), "charge", first, last)
            ranges += [(f.start, f.end) for f in fixed if f.start >= covered_until]
        return ranges, "tariff"

    def _plan_battery(self, results: list, now_local: datetime) -> dict[str, Any]:
        o = {**BATTERY_DEFAULTS, **self.config_entry.options}
        soc_state = self.hass.states.get(o.get(CONF_SOC_ENTITY) or "")
        try:
            soc_now = float(soc_state.state) if soc_state else None
        except ValueError:
            soc_now = None
        if soc_now is None:
            error = (
                f"battery SOC from {o.get(CONF_SOC_ENTITY)} is "
                f"{soc_state.state if soc_state else 'missing'}; the plan will "
                "be made as soon as it reports a number"
            )
            if error != self._battery_error:
                _LOGGER.warning("Battery plan unavailable: %s", error)
            self._battery_error = error
            return {"error": error}
        if self._battery_error:
            _LOGGER.info("Battery plan available again")
        self._battery_error = None

        dt_h = self.period / 60
        starts = [dt_util.as_local(r.start) for r in results]
        load = [r.elec_kwh + float(o[CONF_BASELOAD]) * dt_h for r in results]
        utc_starts = [dt_util.as_utc(t) for t in starts]
        slots, solar_ok = self._solar_slots()
        pv = solar_per_period(utc_starts, self.period, slots)
        field = SOLAR_CONFIDENCE_FIELD.get(o[CONF_SOLAR_CONFIDENCE], "pv_estimate")
        low_slots, _ = self._solar_slots(field)
        pv_low = solar_per_period(utc_starts, self.period, low_slots)
        charge_ranges, charge_source = self._charge_ranges(
            o, starts[0], starts[-1] + timedelta(minutes=self.period)
        )
        params = BatteryParams(
            capacity_kwh=float(o[CONF_BATTERY_KWH]),
            min_soc=float(o[CONF_MIN_SOC]),
            reserve_soc=float(o[CONF_RESERVE_SOC]),
            export_limit_kw=float(o[CONF_EXPORT_LIMIT]),
            charge_power_kw=float(o[CONF_CHARGE_KW]),
            discharge_power_kw=float(o[CONF_DISCHARGE_KW]),
            discharge_threshold_soc=float(o[CONF_DISCHARGE_THRESHOLD]),
            solar_headroom_kwh=float(o[CONF_SOLAR_HEADROOM]),
            load_margin_pct=float(o[CONF_LOAD_MARGIN]),
        )
        plan = make_plan(
            starts=starts,
            period_minutes=self.period,
            load_kwh=load,
            pv_kwh=pv,
            pv_low_kwh=pv_low,
            soc_now=soc_now,
            params=params,
            charge_windows=parse_windows(o[CONF_CHEAP_WINDOWS]),
            discharge_windows=parse_windows(o[CONF_DISCHARGE_WINDOWS]),
            charge_ranges=charge_ranges,
        )
        windows = [w.as_dict() for w in plan.windows if w.end > now_local]
        # The next occurrence of each slot, keyed like "charge_1", "discharge_1".
        slots_next: dict[str, dict] = {}
        for w in windows:
            slots_next.setdefault(f"{w['kind']}_{w['slot']}", w)
        upcoming = [w for w in windows if w["enabled"]]
        nxt = upcoming[0] if upcoming else None
        if nxt:
            verb = "Charge" if nxt["kind"] == "charge" else "Discharge"
            when = dt_util.parse_datetime(nxt["start"])
            summary = f"{verb} to {nxt['target_soc']}% at {when:%H:%M}"
        else:
            summary = "No charging needed"
        # When the inverter next needs updating: shortly before the next window.
        lead = timedelta(minutes=WRITE_LEAD_MINUTES)
        writes = sorted(
            (w.start - lead, f"{w.kind}_{w.slot}") for w in plan.windows if w.start - lead > now_local
        )
        next_write = writes[0][0] if writes else None
        next_write_slots = [k for t, k in writes if t == next_write]
        socs = [v for _, v in plan.soc]
        modes = plan.modes
        storage_mode = modes[0][1] if modes else "Self-Use"
        today = now_local.date()
        self_use_from = next(
            (t.isoformat() for t, m in modes if t.date() == today and m == "Self-Use"
             and all(m2 == "Self-Use" for t2, m2 in modes if t <= t2 and t2.date() == today)),
            None,
        )
        return {
            "summary": summary,
            "storage_mode": storage_mode,
            "charge_windows_from": charge_source,
            "next_write": next_write.isoformat() if next_write else None,
            "next_write_slots": next_write_slots,
            "self_use_from": self_use_from,
            "soc_now": soc_now,
            "solar_forecast": solar_ok,
            "windows": windows,
            "slots": slots_next,
            "next": nxt,
            "min_soc_24h": round(min(socs[: int(24 / dt_h)]), 1) if socs else None,
            "soc_forecast": [
                {"time": t.isoformat(), "soc": round(v, 1)} for t, v in plan.soc
            ],
            # For the card only (not exposed as a sensor attribute).
            "solar": [
                {"start": t.isoformat(), "kwh": round(k, 3)} for t, k in zip(starts, pv)
            ] if solar_ok else [],
        }


def solar_per_period(
    starts_utc: list[datetime], period_minutes: int, slots: list[tuple[datetime, float]]
) -> list[float]:
    """Solar energy (kWh) in each period from half-hour (start, kW) slots."""
    out = []
    span = timedelta(minutes=period_minutes)
    half = timedelta(minutes=30)
    for a in starts_utc:
        b = a + span
        kwh = 0.0
        for s, kw in slots:
            overlap = (min(b, s + half) - max(a, s)).total_seconds()
            if overlap > 0:
                kwh += kw * overlap / 3600
        out.append(kwh)
    return out


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
