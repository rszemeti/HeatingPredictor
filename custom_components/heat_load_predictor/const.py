"""Constants for Heat Load Predictor."""

DOMAIN = "heat_load_predictor"
VERSION = "0.2.2"

CONF_WEATHER = "weather_entity"
CONF_INDOOR = "indoor_temp_entity"
CONF_HEAT_LOSS = "heat_loss_w_per_k"
CONF_CAPACITY = "thermal_capacity_kwh_per_k"
CONF_GAINS = "internal_gains_w"
CONF_STANDBY = "standby_w"
CONF_COP_CURVE = "cop_curve"
CONF_MAX_OUTPUT_CURVE = "max_output_curve"
CONF_PERIOD = "period_minutes"
CONF_HORIZON = "horizon_hours"
CONF_PREHEAT = "preheat"

DEFAULT_HEAT_LOSS = 200.0
DEFAULT_CAPACITY = 10.0
DEFAULT_GAINS = 400.0
DEFAULT_STANDBY = 15.0
DEFAULT_PERIOD = 30
DEFAULT_HORIZON = 48
PERIOD_CHOICES = [5, 10, 15, 30, 60]

CARD_URL = f"/{DOMAIN}/heat-load-schedule-card.js"
STORAGE_VERSION = 1

# Battery planner
CONF_BATTERY_ENABLED = "battery_enabled"
CONF_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_KWH = "battery_capacity_kwh"
CONF_MIN_SOC = "battery_min_soc"
CONF_RESERVE_SOC = "battery_reserve_soc"
CONF_BASELOAD = "baseload_kw"
CONF_SOLAR_TODAY = "solar_today_entity"
CONF_SOLAR_TOMORROW = "solar_tomorrow_entity"
CONF_EXPORT_LIMIT = "export_limit_kw"
CONF_CHARGE_KW = "charge_power_kw"
CONF_DISCHARGE_KW = "discharge_power_kw"
CONF_CHEAP_WINDOWS = "cheap_windows"
CONF_DISCHARGE_WINDOWS = "discharge_windows"
CONF_DISCHARGE_THRESHOLD = "discharge_threshold_soc"
CONF_SOLAR_HEADROOM = "solar_headroom_kwh"

BATTERY_DEFAULTS = {
    CONF_BATTERY_ENABLED: False,
    CONF_BATTERY_KWH: 10.0,
    CONF_MIN_SOC: 15,
    CONF_RESERVE_SOC: 20,
    CONF_BASELOAD: 0.4,
    CONF_EXPORT_LIMIT: 3.68,
    CONF_CHARGE_KW: 5.0,
    CONF_DISCHARGE_KW: 5.0,
    CONF_CHEAP_WINDOWS: "04:00-07:00, 13:00-16:00, 22:00-00:00",
    CONF_DISCHARGE_WINDOWS: "",
    CONF_DISCHARGE_THRESHOLD: 80,
    CONF_SOLAR_HEADROOM: 2.6,
}
