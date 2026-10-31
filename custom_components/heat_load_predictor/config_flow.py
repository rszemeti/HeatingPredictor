"""Config and options flow for Heat Load Predictor."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    CONF_ROOM_FORECAST,
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
    CONF_HP_FLOW_TARGET,
    CONF_HP_POWER,
    CONF_HP_RETURN,
    CONF_HP_ROOM,
    FLOW_DEFAULTS,
    FLOW_KEYS,
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
    DEFAULT_CAPACITY,
    DEFAULT_GAINS,
    DEFAULT_HEAT_LOSS,
    DEFAULT_HORIZON,
    DEFAULT_PERIOD,
    DEFAULT_STANDBY,
    DOMAIN,
    PERIOD_CHOICES,
)
from .model import DEFAULT_CAPACITY_CURVE, DEFAULT_COP_CURVE, format_curve, parse_curve
from .planner import format_windows, parse_windows


def _defaults() -> dict[str, Any]:
    return {
        CONF_HEAT_LOSS: DEFAULT_HEAT_LOSS,
        CONF_CAPACITY: DEFAULT_CAPACITY,
        CONF_GAINS: DEFAULT_GAINS,
        CONF_STANDBY: DEFAULT_STANDBY,
        CONF_COP_CURVE: format_curve(DEFAULT_COP_CURVE),
        CONF_MAX_OUTPUT_CURVE: format_curve(DEFAULT_CAPACITY_CURVE),
        CONF_PERIOD: str(DEFAULT_PERIOD),
        CONF_HORIZON: DEFAULT_HORIZON,
        CONF_PREHEAT: True,
    }


def _number(lo: float, hi: float, step: float, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=lo, max=hi, step=step, unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _schema(current: dict[str, Any], include_name: bool) -> vol.Schema:
    d = {**_defaults(), **current}
    fields: dict[Any, Any] = {}
    if include_name:
        fields[vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Heat pump"))] = str
    fields[vol.Required(CONF_WEATHER, default=d.get(CONF_WEATHER, vol.UNDEFINED))] = (
        selector.EntitySelector(selector.EntitySelectorConfig(domain="weather"))
    )
    indoor = d.get(CONF_INDOOR)
    fields[
        vol.Optional(CONF_INDOOR, description={"suggested_value": indoor} if indoor else None)
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
    )
    fields[vol.Required(CONF_HEAT_LOSS, default=d[CONF_HEAT_LOSS])] = _number(20, 2000, 1, "W/K")
    fields[vol.Required(CONF_CAPACITY, default=d[CONF_CAPACITY])] = _number(0.5, 100, 0.1, "kWh/K")
    fields[vol.Required(CONF_GAINS, default=d[CONF_GAINS])] = _number(0, 5000, 10, "W")
    fields[vol.Required(CONF_STANDBY, default=d[CONF_STANDBY])] = _number(0, 500, 1, "W")
    fields[vol.Required(CONF_COP_CURVE, default=d[CONF_COP_CURVE])] = selector.TextSelector(
        selector.TextSelectorConfig(multiline=True)
    )
    fields[vol.Required(CONF_MAX_OUTPUT_CURVE, default=d[CONF_MAX_OUTPUT_CURVE])] = (
        selector.TextSelector(selector.TextSelectorConfig(multiline=True))
    )
    fields[vol.Required(CONF_PERIOD, default=str(d[CONF_PERIOD]))] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(p) for p in PERIOD_CHOICES],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    fields[vol.Required(CONF_HORIZON, default=d[CONF_HORIZON])] = _number(6, 168, 1, "h")
    fields[vol.Required(CONF_PREHEAT, default=d[CONF_PREHEAT])] = selector.BooleanSelector()
    return vol.Schema(fields)


def _optional_entity(key: str, current: dict[str, Any], **cfg: Any) -> tuple[Any, Any]:
    value = current.get(key)
    return (
        vol.Optional(key, description={"suggested_value": value} if value else None),
        selector.EntitySelector(selector.EntitySelectorConfig(**cfg)),
    )


def _battery_schema(current: dict[str, Any]) -> vol.Schema:
    d = {**BATTERY_DEFAULTS, **current}
    fields: dict[Any, Any] = {
        vol.Required(CONF_BATTERY_ENABLED, default=d[CONF_BATTERY_ENABLED]): selector.BooleanSelector(),
    }
    for key, cfg in (
        (CONF_SOC_ENTITY, {"domain": "sensor"}),
        (CONF_SOLAR_TODAY, {"domain": "sensor"}),
        (CONF_SOLAR_TOMORROW, {"domain": "sensor"}),
        (CONF_HOUSE_LOAD, {"domain": "sensor"}),
        (CONF_RATES_TODAY, {"domain": "event"}),
        (CONF_RATES_TOMORROW, {"domain": "event"}),
    ):
        k, v = _optional_entity(key, d, **cfg)
        fields[k] = v
    fields[vol.Required(CONF_BATTERY_KWH, default=d[CONF_BATTERY_KWH])] = _number(1, 500, 0.1, "kWh")
    fields[vol.Required(CONF_MIN_SOC, default=d[CONF_MIN_SOC])] = _number(0, 99, 1, "%")
    fields[vol.Required(CONF_RESERVE_SOC, default=d[CONF_RESERVE_SOC])] = _number(0, 100, 1, "%")
    fields[vol.Required(CONF_BASELOAD, default=d[CONF_BASELOAD])] = _number(0, 20, 0.05, "kW")
    fields[vol.Required(CONF_LEARN_DAYS, default=d[CONF_LEARN_DAYS])] = _number(1, 60, 1, "days")
    since = d.get(CONF_HP_SINCE)
    fields[vol.Optional(CONF_HP_SINCE, description={"suggested_value": since} if since else None)] = (
        selector.DateTimeSelector()
    )
    fields[vol.Required(CONF_EXPORT_LIMIT, default=d[CONF_EXPORT_LIMIT])] = _number(0, 100, 0.01, "kW")
    fields[vol.Required(CONF_CHARGE_KW, default=d[CONF_CHARGE_KW])] = _number(0.1, 100, 0.1, "kW")
    fields[vol.Required(CONF_DISCHARGE_KW, default=d[CONF_DISCHARGE_KW])] = _number(0.1, 100, 0.1, "kW")
    fields[vol.Optional(CONF_CHEAP_WINDOWS, default=d[CONF_CHEAP_WINDOWS])] = selector.TextSelector()
    fields[vol.Required(CONF_CHEAP_TOLERANCE, default=d[CONF_CHEAP_TOLERANCE])] = _number(0, 200, 1, "%")
    fields[vol.Optional(CONF_DISCHARGE_WINDOWS, default=d[CONF_DISCHARGE_WINDOWS])] = selector.TextSelector()
    fields[vol.Required(CONF_DISCHARGE_THRESHOLD, default=d[CONF_DISCHARGE_THRESHOLD])] = _number(0, 100, 1, "%")
    fields[vol.Required(CONF_SOLAR_HEADROOM, default=d[CONF_SOLAR_HEADROOM])] = _number(0, 100, 0.1, "kWh")
    fields[vol.Required(CONF_SOLAR_CONFIDENCE, default=d[CONF_SOLAR_CONFIDENCE])] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["pessimistic", "expected", "optimistic"],
            translation_key="solar_confidence",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    fields[vol.Required(CONF_ROOM_FORECAST, default=d.get(CONF_ROOM_FORECAST, "expected"))] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["expected", "optimistic"],
            translation_key="room_forecast",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    fields[vol.Required(CONF_LOAD_MARGIN, default=d[CONF_LOAD_MARGIN])] = _number(0, 100, 1, "%")
    fields[vol.Required(CONF_CHARGE_STRATEGY, default=d.get(CONF_CHARGE_STRATEGY, "fill"))] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["fill", "minimum"],
            translation_key="charge_strategy",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    return vol.Schema(fields)


def _validate_battery(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (CONF_CHEAP_WINDOWS, CONF_DISCHARGE_WINDOWS):
        try:
            parse_windows(user_input.get(key, ""))
        except (ValueError, TypeError):
            errors[key] = "bad_windows"
    if user_input.get(CONF_BATTERY_ENABLED) and not user_input.get(CONF_SOC_ENTITY):
        errors[CONF_SOC_ENTITY] = "soc_required"
    if user_input.get(CONF_RESERVE_SOC, 0) < user_input.get(CONF_MIN_SOC, 0):
        errors[CONF_RESERVE_SOC] = "reserve_below_min"
    return errors


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (CONF_COP_CURVE, CONF_MAX_OUTPUT_CURVE):
        try:
            pts = parse_curve(user_input[key])
            if any(y <= 0 for _, y in pts):
                errors[key] = "curve_positive"
        except (ValueError, TypeError):
            errors[key] = "bad_curve"
    return errors


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_PERIOD] = int(out[CONF_PERIOD])
    out[CONF_HORIZON] = int(out[CONF_HORIZON])
    for key in (CONF_COP_CURVE, CONF_MAX_OUTPUT_CURVE):
        out[key] = format_curve(parse_curve(out[key]))
    return out


class HeatLoadPredictorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the predictor."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                cleaned = _clean(user_input)
                name = cleaned.pop(CONF_NAME)
                return self.async_create_entry(title=name, data={}, options=cleaned)
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}, include_name=True),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HeatLoadPredictorOptionsFlow()


class HeatLoadPredictorOptionsFlow(OptionsFlow):
    """Change the predictor's settings: heating model or battery planner."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["heating", "battery", "flow"])

    async def async_step_heating(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                keep = {k: v for k, v in self.config_entry.options.items()
                        if k in BATTERY_KEYS or k in FLOW_KEYS}
                return self.async_create_entry(data={**keep, **_clean(user_input)})
        current = user_input or dict(self.config_entry.options)
        return self.async_show_form(
            step_id="heating", data_schema=_schema(current, include_name=False), errors=errors
        )

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_battery(user_input)
            if not errors:
                cleaned = dict(user_input)
                for key in (CONF_CHEAP_WINDOWS, CONF_DISCHARGE_WINDOWS):
                    cleaned[key] = format_windows(parse_windows(cleaned.get(key, "")))
                keep = {k: v for k, v in self.config_entry.options.items() if k not in BATTERY_KEYS}
                return self.async_create_entry(data={**keep, **cleaned})
        current = user_input or dict(self.config_entry.options)
        return self.async_show_form(
            step_id="battery", data_schema=_battery_schema(current), errors=errors
        )


    async def async_step_flow(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Heat pump readings and flow temperature planning (advisory)."""
        if user_input is not None:
            keep = {k: v for k, v in self.config_entry.options.items() if k not in FLOW_KEYS}
            return self.async_create_entry(data={**keep, **user_input})
        current = dict(self.config_entry.options)
        return self.async_show_form(step_id="flow", data_schema=_flow_schema(current))


def _flow_schema(current: dict[str, Any]) -> vol.Schema:
    d = {**FLOW_DEFAULTS, **current}
    fields: dict[Any, Any] = {}
    for key, cfg in (
        (CONF_HP_FLOW, {"domain": "sensor", "device_class": "temperature"}),
        (CONF_HP_RETURN, {"domain": "sensor", "device_class": "temperature"}),
        (CONF_HP_FLOW_RATE, {"domain": "sensor"}),
        (CONF_HP_HEAT, {"domain": "sensor", "device_class": "power"}),
        (CONF_HP_POWER, {"domain": "sensor", "device_class": "power"}),
        (CONF_HP_ROOM, {"domain": "sensor", "device_class": "temperature"}),
        (CONF_HP_OUTDOOR, {"domain": "sensor", "device_class": "temperature"}),
        (CONF_HP_FLOW_TARGET, {"domain": "number"}),
    ):
        k, v = _optional_entity(key, current, **cfg)
        fields[k] = v
    fields[vol.Required(CONF_FLOW_MIN, default=d[CONF_FLOW_MIN])] = _number(15, 60, 0.5, "°C")
    fields[vol.Required(CONF_FLOW_MAX, default=d[CONF_FLOW_MAX])] = _number(25, 75, 0.5, "°C")
    fields[vol.Required(CONF_FLOW_LPM, default=d[CONF_FLOW_LPM])] = _number(1, 100, 0.1, "L/min")
    fields[vol.Required(CONF_EMITTER_K, default=d[CONF_EMITTER_K])] = _number(0.005, 1, 0.001, "kW/K^n")
    fields[vol.Required(CONF_EMITTER_N, default=d[CONF_EMITTER_N])] = _number(1.0, 1.6, 0.05, "")
    fields[vol.Required(CONF_HP_LEARN_DAYS, default=d[CONF_HP_LEARN_DAYS])] = _number(1, 30, 1, "days")
    return vol.Schema(fields)


BATTERY_KEYS = set(BATTERY_DEFAULTS) | {
    CONF_SOC_ENTITY, CONF_SOLAR_TODAY, CONF_SOLAR_TOMORROW, CONF_RATES_TODAY, CONF_RATES_TOMORROW,
    CONF_HOUSE_LOAD, CONF_HP_SINCE,
}
