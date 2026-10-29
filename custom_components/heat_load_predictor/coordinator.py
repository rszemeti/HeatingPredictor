"""Coordinator: fetch the forecast, run the model, hold the results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
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
    CONF_HOUSE_LOAD,
    CONF_HP_SINCE,
    CONF_LEARN_DAYS,
    CONF_LOAD_MARGIN,
    CONF_CHARGE_STRATEGY,
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
    LEARN_EVERY_HOURS,
    SOLAR_CONFIDENCE_FIELD,
    SOLAR_HIGH_FIELD,
    CONF_ROOM_FORECAST,
    WRITE_LEAD_MINUTES,
    STORAGE_VERSION,
    VERSION,
    CONF_EMITTER_K,
    CONF_EMITTER_N,
    CONF_FLOW_LPM,
    CONF_FLOW_MAX,
    CONF_FLOW_MIN,
    CONF_HP_FLOW,
    CONF_HP_FLOW_RATE,
    CONF_HP_HEAT,
    CONF_HP_LEARN_DAYS,
    CONF_HP_OUTDOOR,
    CONF_HP_POWER,
    CONF_HP_RETURN,
    CONF_HP_ROOM,
    FLOW_DEFAULTS,
)
from . import flowlaw
from .learn import base_profile, half_hour_means, slot_of
from .planner import BatteryParams, _later, cheap_ranges, make_plan, occurrences, parse_windows
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
        # Learned baseload: (48 half-hourly kW values, days of data, when).
        self._profile: tuple[list[float], float, datetime] | None = None
        # The battery planner's inputs and output from the last run.
        self.plan_inputs: dict | None = None
        # Flow planning: what's been learned from the heat pump, and when.
        self._flow_learned: flowlaw.Learned | None = None
        self._flow_learned_at: datetime | None = None
        self._flow_house: flowlaw.HouseFit | None = None
        self._flow_status = "not run yet"
        self._flow_correction = 0.0
        self._flow_corr_at: datetime | None = None
        self._warned_gas: set[str] = set()

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
        await self._async_maybe_learn_flow()
        data["flow"] = self._plan_flow(results, data, now_local, target)
        if self.config_entry.options.get(CONF_BATTERY_ENABLED):
            await self._async_maybe_learn()
        o = self.config_entry.options
        if o.get(CONF_BATTERY_ENABLED):
            data["battery"] = self._plan_battery(results, now_local)
        return data

    # ---------------------------------------------------------------- flow

    def _num(self, entity_id: str | None, unit_to: str | None = None) -> float | None:
        """A sensor's numeric value, in kW for power and °C for temperature."""
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        try:
            v = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit_to == "kW" and unit == "W":
            return v / 1000
        if unit_to == "°C":
            return _to_c(v, unit)
        return v

    def _plan_flow(self, results: list, data: dict, now_local: datetime, target) -> dict:
        """Advisory flow temperature for each period (nothing is written)."""
        o = {**FLOW_DEFAULTS, **self.config_entry.options}
        learned = self._flow_learned or flowlaw.Learned()
        lpm = self._num(o.get(CONF_HP_FLOW_RATE))
        lpm_source = "sensor"
        if lpm is None or lpm < 2:
            lpm, lpm_source = float(o[CONF_FLOW_LPM]), "setting"
        k = learned.k or float(o[CONF_EMITTER_K])
        p = flowlaw.FlowParams(
            k=k, n=float(o[CONF_EMITTER_N]), flow_lpm=lpm,
            flow_min=float(o[CONF_FLOW_MIN]), flow_max=float(o[CONF_FLOW_MAX]),
        )
        # Live correction: a slow integral of (target - room). The forecast
        # already starts from the measured room temperature; this only takes
        # out a steady bias (radiators or heat loss a bit off).
        room = self._num(o.get(CONF_HP_ROOM), "°C") or self._indoor_temp()
        now_utc = dt_util.utcnow()
        if room is not None and self._flow_corr_at is not None:
            dt_h = min(1.0, (now_utc - self._flow_corr_at).total_seconds() / 3600)
            err = target(now_utc) - room
            self._flow_correction = max(-5.0, min(5.0, self._flow_correction + 0.5 * err * dt_h))
        self._flow_corr_at = now_utc

        house = self._flow_house
        if house:
            per_h = (results[1].start - results[0].start).total_seconds() / 3600 if len(results) > 1 else 0.5
            flows = []
            for r in results:
                if r.heat_kw < p.idle_kw:
                    flows.append(None)
                    continue
                room_mid = (r.t_in_start + r.t_in_end) / 2
                f = house.flow(room_mid, r.t_out, (r.t_in_end - r.t_in_start) / per_h)
                f = min(p.flow_max, max(p.flow_min, f + self._flow_correction))
                flows.append(round(f * 2) / 2)
        else:
            rows = [(r.heat_kw, (r.t_in_start + r.t_in_end) / 2) for r in results]
            flows = flowlaw.plan_flows(rows, p, self._flow_correction)
        cop_model = learned.cop
        plan = []
        for r, f, row in zip(results, flows, data["periods"]):
            row["flow"] = f
            item = {"start": dt_util.as_local(r.start).isoformat(), "flow": f,
                    "heat_kw": round(r.heat_kw, 2)}
            if cop_model and f is not None:
                cop = flowlaw.cop_at(cop_model, r.t_out, f)
                item["cop_learned"] = round(cop, 2)
                item["elec_kwh_learned"] = round(r.heat_kwh / cop, 3)
            plan.append(item)
        h = learned.heat_loss_w_per_k or float(self.config_entry.options.get(CONF_HEAT_LOSS, 200))
        gains = float(self.config_entry.options.get(CONF_GAINS, 400)) / 1000
        t_room = target(now_utc)
        return {
            "now": flows[0] if flows else None,
            "idle_now": bool(flows) and flows[0] is None,
            "plan": plan,
            "correction": round(self._flow_correction, 2),
            "emitter_k": round(k, 4),
            "emitter_k_from": "learned" if learned.k else "setting",
            "flow_rate_lpm": round(lpm, 1),
            "flow_rate_from": lpm_source,
            "heat_loss_w_per_k": round(h),
            "heat_loss_from": "learned" if learned.heat_loss_w_per_k else "setting",
            "curve_from": "history" if house else "settings",
            "learn_status": self._flow_status,
            "house_fit": house.as_dict() if house else None,
            "water_law": (
                [{"outdoor": t, "flow": round(house.flow(t_room, t), 1)} for t in (-10, -5, 0, 5, 10, 15)]
                if house else
                [{"outdoor": t, "flow": f} for t, f in flowlaw.water_law(h, gains, t_room, k, p.n, lpm)]),
            "holds_target_down_to": round(
                house.holds_down_to(t_room, p.flow_max) if house else
                flowlaw.balance_point(h, gains, t_room, k, p.n, lpm, p.flow_max), 1),
            "learned": learned.as_dict(),
            "learned_at": self._flow_learned_at.isoformat() if self._flow_learned_at else None,
        }

    async def _async_maybe_learn_flow(self) -> None:
        o = self.config_entry.options
        missing = [name for key, name in (
            (CONF_HP_FLOW, "flow temperature"), (CONF_HP_OUTDOOR, "outdoor temperature"),
        ) if not o.get(key)]
        if not (o.get(CONF_HP_ROOM) or o.get(CONF_INDOOR)):
            missing.append("room temperature")
        if missing:
            self._flow_status = ("set the heat pump's " + ", ".join(missing)
                                 + " sensor in Flow temperature planning")
            return
        if "recorder" not in self.hass.config.components:
            self._flow_status = "needs the recorder"
            return
        if self._flow_learned_at and dt_util.utcnow() - self._flow_learned_at < timedelta(
                hours=LEARN_EVERY_HOURS):
            return
        try:
            self._flow_learned = await self._async_learn_flow()
            self._flow_learned_at = dt_util.utcnow()
            if self._flow_learned:
                _LOGGER.info("Learned from the heat pump: %s", self._flow_learned.as_dict())
            if self._flow_house:
                _LOGGER.info("Learned the flow curve from history: %s", self._flow_house.as_dict())
        except Exception as err:  # noqa: BLE001 - never let learning break the forecast
            self._flow_status = f"failed: {err}"
            _LOGGER.warning("Couldn't learn from the heat pump readings: %s", err)

    async def _async_learn_flow(self) -> flowlaw.Learned | None:
        """Half-hourly heat pump readings from statistics, then fit K, H, COP."""
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers.recorder import get_instance

        o = {**FLOW_DEFAULTS, **self.config_entry.options}
        ids = {
            "heat": o.get(CONF_HP_HEAT), "elec": o.get(CONF_HP_POWER),
            "flow": o.get(CONF_HP_FLOW), "ret": o.get(CONF_HP_RETURN),
            "room": o.get(CONF_HP_ROOM) or o.get(CONF_INDOOR),
            "out": o.get(CONF_HP_OUTDOOR),
        }
        ids = {k: v for k, v in ids.items() if v}
        end = align_down(dt_util.now(), 30)
        start = end - timedelta(days=int(o[CONF_HP_LEARN_DAYS]))
        rec = get_instance(self.hass)

        def fetch(period: str) -> dict:
            return statistics_during_period(
                self.hass, dt_util.as_utc(start), dt_util.as_utc(end), set(ids.values()),
                period, {"power": "kW", "temperature": "°C"}, {"mean"},
            )

        five = await rec.async_add_executor_job(fetch, "5minute")
        hourly = await rec.async_add_executor_job(fetch, "hour")
        series = {}
        for key, eid in ids.items():
            series[key] = half_hour_means(
                [(dt_util.as_local(dt_util.utc_from_timestamp(r["start"])), r["mean"])
                 for r in five.get(eid, []) if r.get("mean") is not None],
                start, end,
            )
            # Older half-hours that only have hourly statistics.
            for r in hourly.get(eid, []):
                if r.get("mean") is None:
                    continue
                t = dt_util.as_local(dt_util.utc_from_timestamp(r["start"]))
                for half in (t, t + timedelta(minutes=30)):
                    if start <= half < end:
                        series[key].setdefault(half, r["mean"])
        empty = [ids[k] for k in ("flow", "room", "out") if not series.get(k)]
        if empty:
            self._flow_house = None
            self._flow_status = ("no history for " + ", ".join(empty)
                                 + " (the sensor needs state_class: measurement)")
            return None
        nan = float("nan")
        samples = {}
        for t, flow in series["flow"].items():
            room, out = series["room"].get(t), series["out"].get(t)
            if room is None or out is None:
                continue
            g = lambda k: series.get(k, {}).get(t)  # noqa: E731
            samples[t] = flowlaw.Sample(
                heat_kw=g("heat") if g("heat") is not None else nan,
                elec_kw=g("elec") if g("elec") is not None else 0.0,
                flow=flow, ret=g("ret") if g("ret") is not None else nan, room=room, out=out,
            )

        # The flow curve straight from history (flow, room, outdoor).
        h0 = float(o.get(CONF_HEAT_LOSS, 200))
        gains = float(o.get(CONF_GAINS, 400)) / 1000
        outs = [s.out for s in samples.values()]
        s_settings = flowlaw.model_slope(
            h0, gains, 21.0, float(o[CONF_EMITTER_K]), float(o[CONF_EMITTER_N]),
            float(o[CONF_FLOW_LPM]), sum(outs) / len(outs) if outs else 5.0)
        self._flow_house = flowlaw.fit_house(samples, s_settings)
        hours = len(samples) / 2
        if self._flow_house:
            self._flow_status = (f"learned from {self._flow_house.hours:.0f} h of history"
                                 + (" (slope from the settings: not enough variety yet)"
                                    if self._flow_house.mode == "anchored" else ""))
        else:
            self._flow_status = f"only {hours:.0f} h of usable history so far; needs about a day"

        samples = {t: s for t, s in samples.items()
                   if flowlaw.is_finite(s.heat_kw, s.ret)}
        learned = flowlaw.Learned()
        fit = flowlaw.fit_emitter(samples.values(), float(o[CONF_EMITTER_N]))
        if fit:
            learned.k, learned.k_samples = fit
        with_out = samples
        if with_out:
            hl = flowlaw.fit_heat_loss(
                flowlaw.runs_of(with_out),
                float(self.config_entry.options.get(CONF_GAINS, 400)) / 1000,
                float(self.config_entry.options.get(CONF_CAPACITY, 10)),
            )
            if hl:
                learned.heat_loss_w_per_k, learned.heat_loss_hours = hl
            if "elec" in series:
                cop = flowlaw.fit_cop(with_out.values())
                if cop:
                    learned.cop, learned.cop_samples = cop
        return learned

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
        slots, found, _ = self._solar_slots_with(field)
        return slots, found

    def _solar_slots_with(self, field: str) -> tuple[list[tuple[datetime, float]], bool, bool]:
        """As `_solar_slots`, plus whether any slot actually had `field`."""
        slots: list[tuple[datetime, float]] = []
        found = has_field = False
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
                has_field = has_field or field in item
        return slots, found, has_field

    async def _async_maybe_learn(self) -> None:
        """Relearn the baseload profile if it's missing or stale."""
        o = self.config_entry.options
        if not o.get(CONF_HOUSE_LOAD) or "recorder" not in self.hass.config.components:
            return
        if self._profile and dt_util.utcnow() - self._profile[2] < timedelta(hours=LEARN_EVERY_HOURS):
            return
        try:
            learned = await self._async_learn_profile()
        except Exception as err:  # noqa: BLE001 - never let learning break the plan
            _LOGGER.warning("Couldn't learn the house load profile: %s", err)
            learned = None
        if learned:
            self._profile = (learned[0], learned[1], dt_util.utcnow())
            p = learned[0]
            _LOGGER.info(
                "Learned house baseload from %.1f days of %s: average %.2f kW, "
                "17:00-19:00 %.2f kW, 02:00-05:00 %.2f kW",
                learned[1], o[CONF_HOUSE_LOAD], sum(p) / len(p),
                sum(p[34:38]) / 4, sum(p[4:10]) / 6,
            )

    async def _async_learn_profile(self) -> tuple[list[float], float] | None:
        """House load history minus modelled heat pump use, by half-hour of day."""
        from homeassistant.components.recorder.history import state_changes_during_period
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers.recorder import get_instance

        o = {**BATTERY_DEFAULTS, **self.config_entry.options}
        load_id = o[CONF_HOUSE_LOAD]
        weather_id = o[CONF_WEATHER]
        end = align_down(dt_util.now(), 30)
        start = end - timedelta(days=int(o[CONF_LEARN_DAYS]))
        rec = get_instance(self.hass)

        def fetch(period: str) -> list:
            rows = statistics_during_period(
                self.hass, dt_util.as_utc(start), dt_util.as_utc(end), {load_id},
                period, {"power": "kW"}, {"mean"},
            )
            return rows.get(load_id, [])

        five = await rec.async_add_executor_job(fetch, "5minute")
        hourly = await rec.async_add_executor_job(fetch, "hour")
        house = half_hour_means(
            [(dt_util.as_local(dt_util.utc_from_timestamp(r["start"])), r["mean"])
             for r in five if r.get("mean") is not None],
            start, end,
        )
        # Older half-hours that only have hourly statistics.
        for r in hourly:
            if r.get("mean") is None:
                continue
            t = dt_util.as_local(dt_util.utc_from_timestamp(r["start"]))
            for half in (t, t + timedelta(minutes=30)):
                if start <= half < end:
                    house.setdefault(half, r["mean"])
        if not house:
            return None

        changes = await rec.async_add_executor_job(
            state_changes_during_period, self.hass, dt_util.as_utc(start), dt_util.as_utc(end),
            weather_id, False, False, None, True,
        )
        unit = self.hass.config.units.temperature_unit
        temps = []
        for st in changes.get(weather_id, []):
            t = st.attributes.get("temperature")
            if t is None:
                continue
            try:
                temps.append((st.last_updated, _to_c(float(t), st.attributes.get("temperature_unit", unit))))
            except (TypeError, ValueError):
                continue
        since = None
        if o.get(CONF_HP_SINCE):
            parsed = dt_util.parse_datetime(str(o[CONF_HP_SINCE]))
            if parsed is None:
                d = dt_util.parse_date(str(o[CONF_HP_SINCE]))
                parsed = datetime(d.year, d.month, d.day) if d else None
            if parsed is not None:
                since = dt_util.as_local(
                    parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())
                )

        heat: dict[datetime, float] = {}
        if len(temps) >= 2:
            schedule = self.schedule

            def target(when: datetime) -> float:
                return schedule_temp(schedule, dt_util.as_local(when))

            start_utc = dt_util.as_utc(start)
            past = simulate(
                start=start_utc,
                periods=int((end - start).total_seconds() // 1800),
                period_minutes=30,
                outdoor_temp=forecast_interpolator(temps),
                target_temp=target,
                t_in_start=target(start_utc),
                params=self._params(),
            )
            heat = {dt_util.as_local(r.start): r.elec_kwh * 2 for r in past}
        elif since is None:
            _LOGGER.info("Not enough outdoor temperature history in %s to learn yet", weather_id)
            return None
        return base_profile(house, heat, heat_pump_since=since, min_days=1.0)

    def _baseload_kwh(self, starts: list[datetime], o: dict) -> tuple[list[float], str]:
        dt_h = self.period / 60
        if self._profile:
            prof, days, _ = self._profile
            return [prof[slot_of(t)] * dt_h for t in starts], f"learned from {days:.1f} days"
        return [float(o[CONF_BASELOAD]) * dt_h for _ in starts], f"fixed {float(o[CONF_BASELOAD]):.2f} kW"

    def _rates(self) -> list[tuple[datetime, datetime, float]]:
        """(start, end, price) periods from Octopus Energy rate event entities."""
        out: list[tuple[datetime, datetime, float]] = []
        for key in (CONF_RATES_TODAY, CONF_RATES_TOMORROW):
            entity_id = self.config_entry.options.get(key)
            if entity_id and "_gas_" in entity_id:
                # Gas is priced far below electricity: it would make the whole
                # day look cheap. Ignore it.
                if entity_id not in self._warned_gas:
                    self._warned_gas.add(entity_id)
                    _LOGGER.warning(
                        "Ignoring %s for cheap windows: it's a gas tariff. Set the "
                        "electricity rates event instead.", entity_id)
                continue
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
        return sorted(set(out), key=lambda r: dt_util.as_utc(r[0]))

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
        base, base_source = self._baseload_kwh(starts, o)
        load = [r.elec_kwh + b for r, b in zip(results, base)]
        utc_starts = [dt_util.as_utc(t) for t in starts]
        slots, solar_ok = self._solar_slots()
        pv = solar_per_period(utc_starts, self.period, slots)
        field = SOLAR_CONFIDENCE_FIELD.get(o[CONF_SOLAR_CONFIDENCE], "pv_estimate")
        low_slots, _ = self._solar_slots(field)
        pv_low = solar_per_period(utc_starts, self.period, low_slots)
        # The room the storage mode keeps free for the peak: on the expected
        # forecast plus headroom (default; the plan reruns every 15 minutes, so
        # a sunnier day switches to Feed-in in time), or on Solcast's
        # optimistic estimate, which exports more on an ordinary day.
        pv_high = None
        if o.get(CONF_ROOM_FORECAST, "expected") == "optimistic":
            high_slots, _, has_high = self._solar_slots_with(SOLAR_HIGH_FIELD)
            pv_high = solar_per_period(utc_starts, self.period, high_slots) if has_high else None
        charge_ranges, charge_source = self._charge_ranges(
            o, starts[0], dt_util.as_local(utc_starts[-1] + timedelta(minutes=self.period))
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
            fill_cheap_windows=o.get(CONF_CHARGE_STRATEGY, "fill") != "minimum",
        )
        plan = make_plan(
            starts=starts,
            period_minutes=self.period,
            load_kwh=load,
            pv_kwh=pv,
            pv_low_kwh=pv_low,
            pv_high_kwh=pv_high,
            soc_now=soc_now,
            params=params,
            charge_windows=parse_windows(o[CONF_CHEAP_WINDOWS]),
            discharge_windows=parse_windows(o[CONF_DISCHARGE_WINDOWS]),
            charge_ranges=charge_ranges,
        )
        # Everything the planner saw, for "Download planning data" on the card.
        self.plan_inputs = {
            "version": VERSION,
            "made_at": now_local.isoformat(),
            "period_minutes": self.period,
            "starts": [t.isoformat() for t in starts],
            "heat_kwh": [round(r.elec_kwh, 4) for r in results],
            "base_kwh": [round(b, 4) for b in base],
            "baseload_from": base_source,
            "load_kwh": [round(x, 4) for x in load],
            "pv_kwh": [round(x, 4) for x in pv],
            "pv_low_kwh": [round(x, 4) for x in pv_low],
            "solar_field_low": field,
            "pv_high_kwh": [round(x, 4) for x in pv_high] if pv_high is not None else None,
            "solar_slots": [(t.isoformat(), kw) for t, kw in slots],
            "soc_now": soc_now,
            "params": asdict(params),
            "cheap_windows": o[CONF_CHEAP_WINDOWS],
            "discharge_windows": o[CONF_DISCHARGE_WINDOWS],
            "charge_ranges": [(a.isoformat(), b.isoformat()) for a, b in charge_ranges]
            if charge_ranges is not None else None,
            "charge_windows_from": charge_source,
            "options": {k: v for k, v in o.items() if isinstance(v, (str, int, float, bool)) or v is None},
            "plan": {
                "windows": [w.as_dict() for w in plan.windows],
                "soc": [(t.isoformat(), round(v, 2)) for t, v in plan.soc],
                "modes": [(t.isoformat(), m) for t, m in plan.modes],
                "clipped": [(t.isoformat(), round(c, 3)) for t, c in plan.clipped],
            },
        }
        # The plan is in UTC; from here on, times are for people: local.
        tz = now_local.tzinfo
        windows = [w.as_dict(tz) for w in plan.windows if w.end > now_local]
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
            (dt_util.as_local(w.start - lead), f"{w.kind}_{w.slot}") for w in plan.windows
            if w.start - lead > now_local
        )
        next_write = writes[0][0] if writes else None
        next_write_slots = [k for t, k in writes if t == next_write]
        socs = [v for _, v in plan.soc]
        # Solar expected to be lost above the export limit with the battery
        # full, today and over the next 24 h: zero unless it can't be avoided.
        clip_today = sum(c for t, c in plan.clipped if dt_util.as_local(t).date() == now_local.date())
        clip_24h = sum(c for _, c in plan.clipped[: int(24 / dt_h)])
        modes = [(dt_util.as_local(t), m) for t, m in plan.modes]
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
            "baseload_from": base_source,
            "baseload_profile": [round(v, 3) for v in self._profile[0]] if self._profile else None,
            "next_write": next_write.isoformat() if next_write else None,
            "next_write_slots": next_write_slots,
            "self_use_from": self_use_from,
            "clipping_today_kwh": round(clip_today, 2),
            "clipping_24h_kwh": round(clip_24h, 2),
            "soc_now": soc_now,
            "solar_forecast": solar_ok,
            "windows": windows,
            "slots": slots_next,
            "next": nxt,
            "min_soc_24h": round(min(socs[: int(24 / dt_h)]), 1) if socs else None,
            "soc_forecast": [
                {"time": dt_util.as_local(t).isoformat(), "soc": round(v, 1)} for t, v in plan.soc
            ],
            # For the card only (not exposed as sensor attributes).
            "mode_runs": _mode_runs(modes, timedelta(minutes=self.period)),
            "solar": [
                {"start": t.isoformat(), "kwh": round(k, 3)} for t, k in zip(starts, pv)
            ] if solar_ok else [],
        }


def _mode_runs(modes: list[tuple[datetime, str]], dt: timedelta) -> list[dict]:
    """Collapse per-period storage modes into runs: [{start, end, mode}]."""
    runs: list[dict] = []
    for t, m in modes:
        if runs and runs[-1]["mode"] == m:
            runs[-1]["end"] = _later(t, dt).isoformat()
        else:
            runs.append({"start": t.isoformat(), "end": _later(t, dt).isoformat(), "mode": m})
    return runs


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
