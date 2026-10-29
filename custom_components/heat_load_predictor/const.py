"""Constants for Heat Load Predictor."""

DOMAIN = "heat_load_predictor"
VERSION = "0.4.1"

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

CONF_SOLAR_CONFIDENCE = "solar_confidence"   # forecast used to size charges
CONF_LOAD_MARGIN = "load_margin_pct"
SOLAR_CONFIDENCE_FIELD = {
    "pessimistic": "pv_estimate10",
    "expected": "pv_estimate",
    "optimistic": "pv_estimate90",
}
BATTERY_DEFAULTS[CONF_SOLAR_CONFIDENCE] = "pessimistic"
# Sizes the room the storage mode keeps free for solar above the export limit.
SOLAR_HIGH_FIELD = "pv_estimate90"
BATTERY_DEFAULTS[CONF_LOAD_MARGIN] = 10
CONF_CHARGE_STRATEGY = "charge_strategy"   # "fill" or "minimum"
BATTERY_DEFAULTS[CONF_CHARGE_STRATEGY] = "fill"

CONF_RATES_TODAY = "rates_today_entity"
CONF_RATES_TOMORROW = "rates_tomorrow_entity"
CONF_CHEAP_TOLERANCE = "cheap_tolerance_pct"
BATTERY_DEFAULTS[CONF_CHEAP_TOLERANCE] = 10
WRITE_LEAD_MINUTES = 10   # update the inverter this long before each window

CONF_HOUSE_LOAD = "house_load_entity"
CONF_LEARN_DAYS = "learn_days"
BATTERY_DEFAULTS[CONF_LEARN_DAYS] = 10
LEARN_EVERY_HOURS = 6
CONF_HP_SINCE = "heat_pump_since"

# Flow temperature planning (advisory): the heat pump's own readings.
CONF_HP_FLOW = "hp_flow_entity"
CONF_HP_RETURN = "hp_return_entity"
CONF_HP_FLOW_RATE = "hp_flow_rate_entity"
CONF_HP_HEAT = "hp_heat_entity"          # heat out, W or kW
CONF_HP_POWER = "hp_power_entity"        # electricity in, W or kW
CONF_HP_ROOM = "hp_room_entity"
CONF_HP_OUTDOOR = "hp_outdoor_entity"
CONF_FLOW_MIN = "flow_min_c"
CONF_FLOW_MAX = "flow_max_c"
CONF_EMITTER_N = "emitter_exponent"
CONF_EMITTER_K = "emitter_k"             # kW/K^n, used until learned
CONF_FLOW_LPM = "flow_rate_lpm"          # used if no flow rate sensor
CONF_HP_LEARN_DAYS = "hp_learn_days"
FLOW_DEFAULTS = {
    CONF_FLOW_MIN: 25.0,
    CONF_FLOW_MAX: 55.0,
    CONF_EMITTER_N: 1.3,
    CONF_EMITTER_K: 0.065,
    CONF_FLOW_LPM: 14.5,
    CONF_HP_LEARN_DAYS: 7,
}
FLOW_ENTITY_KEYS = (CONF_HP_FLOW, CONF_HP_RETURN, CONF_HP_FLOW_RATE, CONF_HP_HEAT,
                    CONF_HP_POWER, CONF_HP_ROOM, CONF_HP_OUTDOOR)
FLOW_KEYS = set(FLOW_DEFAULTS) | set(FLOW_ENTITY_KEYS)

# What the room kept free for the solar peak is sized on: "expected" (median
# forecast plus the solar headroom) or "optimistic" (Solcast pv_estimate90).
CONF_ROOM_FORECAST = "room_forecast"
BATTERY_DEFAULTS[CONF_ROOM_FORECAST] = "expected"
