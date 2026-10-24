"""Heat Load Predictor: forecast heat pump electricity per period."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_COMPONENT_LOADED, Platform
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import ATTR_COMPONENT

from .const import CARD_URL, DOMAIN, VERSION
from .coordinator import HeatLoadCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_ENTRY = "config_entry_id"
ATTR_SCHEDULE = "schedule"


def _coordinator(hass: HomeAssistant, entry_id: str | None) -> HeatLoadCoordinator:
    loaded = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    if entry_id:
        loaded = [e for e in loaded if e.entry_id == entry_id]
    if not loaded:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entry"
        )
    return loaded[0].runtime_data


def _not_found_message(hass: HomeAssistant) -> str:
    """Why there's no loaded predictor, for the card to show."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.SETUP_RETRY:
            return (
                "Heat Load Predictor is waiting to start: "
                f"{entry.reason or 'the weather forecast is not available yet'}"
            )
    return "No Heat Load Predictor is set up"


def _response(coord: HeatLoadCoordinator) -> dict[str, Any]:
    return {"entry_id": coord.config_entry.entry_id, **(coord.data or {})}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the card, websocket commands and services (once)."""
    card = Path(__file__).parent / "frontend" / "heat-load-schedule-card.js"
    if hass.http is not None:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(card), True)]
        )
    _async_register_card(hass)

    websocket_api.async_register_command(hass, ws_entries)
    websocket_api.async_register_command(hass, ws_get_schedule)
    websocket_api.async_register_command(hass, ws_set_schedule)
    websocket_api.async_register_command(hass, ws_forecast)
    websocket_api.async_register_command(hass, ws_plan_data)

    async def set_schedule(call: ServiceCall) -> None:
        coord = _coordinator(hass, call.data.get(ATTR_ENTRY))
        try:
            await coord.async_set_schedule(call.data[ATTR_SCHEDULE])
        except (ValueError, KeyError, TypeError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="bad_schedule",
                translation_placeholders={"error": str(err)},
            ) from err

    async def get_forecast(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass, call.data.get(ATTR_ENTRY))
        if call.data.get("refresh"):
            await coord.async_refresh()
        return _response(coord)

    hass.services.async_register(
        DOMAIN,
        "set_schedule",
        set_schedule,
        schema=vol.Schema(
            {vol.Optional(ATTR_ENTRY): cv.string, vol.Required(ATTR_SCHEDULE): dict}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "get_forecast",
        get_forecast,
        schema=vol.Schema(
            {vol.Optional(ATTR_ENTRY): cv.string, vol.Optional("refresh", default=False): cv.boolean}
        ),
        supports_response=SupportsResponse.ONLY,
    )
    return True


def _add_card_js(hass: HomeAssistant) -> None:
    from homeassistant.components.frontend import add_extra_js_url

    add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    _LOGGER.debug("Registered schedule card %s", CARD_URL)


@callback
def _async_register_card(hass: HomeAssistant) -> None:
    """Load the schedule card on every dashboard.

    During start-up this integration can be set up before the frontend, so
    if the frontend isn't loaded yet, wait for it rather than skipping.
    """

    if "frontend" in hass.config.components:
        _add_card_js(hass)
        return

    @callback
    def _on_loaded(event: Event) -> None:
        if event.data.get(ATTR_COMPONENT) == "frontend":
            unsub()
            _add_card_js(hass)

    unsub = hass.bus.async_listen(EVENT_COMPONENT_LOADED, _on_loaded)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HeatLoadCoordinator(hass, entry)
    await coordinator.async_load_schedule()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_watch_battery_inputs())
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


# --------------------------------------------------------------------------
# Websocket API for the schedule card
# --------------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/entries"})
@callback
def ws_entries(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    connection.send_result(
        msg["id"],
        [
            {"entry_id": e.entry_id, "title": e.title}
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ],
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/schedule/get", vol.Optional("entry_id"): str}
)
@callback
def ws_get_schedule(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    try:
        coord = _coordinator(hass, msg.get("entry_id"))
    except ServiceValidationError:
        connection.send_error(msg["id"], "not_found", _not_found_message(hass))
        return
    connection.send_result(
        msg["id"], {"entry_id": coord.config_entry.entry_id, "schedule": coord.schedule}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/schedule/set",
        vol.Optional("entry_id"): str,
        vol.Required("schedule"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_schedule(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    try:
        coord = _coordinator(hass, msg.get("entry_id"))
    except ServiceValidationError:
        connection.send_error(msg["id"], "not_found", _not_found_message(hass))
        return
    try:
        cleaned = await coord.async_set_schedule(msg["schedule"])
    except (ValueError, KeyError, TypeError) as err:
        connection.send_error(msg["id"], "invalid_schedule", str(err))
        return
    connection.send_result(msg["id"], {"schedule": cleaned})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/forecast", vol.Optional("entry_id"): str}
)
@callback
def ws_forecast(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        coord = _coordinator(hass, msg.get("entry_id"))
    except ServiceValidationError:
        connection.send_error(msg["id"], "not_found", _not_found_message(hass))
        return
    connection.send_result(msg["id"], _response(coord))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/plan_data", vol.Optional("entry_id"): str}
)
@callback
def ws_plan_data(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Everything the battery planner saw on its last run, and what it decided."""
    try:
        coord = _coordinator(hass, msg.get("entry_id"))
    except ServiceValidationError:
        connection.send_error(msg["id"], "not_found", _not_found_message(hass))
        return
    if not coord.plan_inputs:
        connection.send_error(msg["id"], "no_plan", "The battery planner hasn't made a plan yet.")
        return
    connection.send_result(msg["id"], coord.plan_inputs)
