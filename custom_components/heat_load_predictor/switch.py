"""Flow control switch: lets the planner write the flow temperature."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSION
from .coordinator import HeatLoadCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([FlowControlSwitch(entry.runtime_data)])


class FlowControlSwitch(CoordinatorEntity[HeatLoadCoordinator], SwitchEntity, RestoreEntity):
    """On: the planned flow temperature is written to the heat pump's flow
    target. Off (the default): nothing is written, and turning it off puts
    back the flow target the heat pump had before."""

    _attr_has_entity_name = True
    _attr_translation_key = "flow_control"
    _attr_icon = "mdi:thermometer-water"

    def __init__(self, coordinator: HeatLoadCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_flow_control"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Heat Load Predictor",
            model="Thermal model",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.flow_control = True
            restore = last.attributes.get("restore_to")
            self.coordinator.flow_restore_to = float(restore) if restore is not None else None
            await self.coordinator.async_request_refresh()

    @property
    def is_on(self) -> bool:
        return self.coordinator.flow_control

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        return {
            "status": c._flow_control_status,
            "restore_to": c.flow_restore_to,
            "last_written": c._flow_written,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_flow_control(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_flow_control(False)
        self.async_write_ha_state()
