"""Constants for Heat Load Predictor."""

DOMAIN = "heat_load_predictor"
VERSION = "0.1.2"

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
