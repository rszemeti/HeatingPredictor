"""Sensors for Heat Load Predictor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSION
from .coordinator import HeatLoadCoordinator


@dataclass(frozen=True, kw_only=True)
class HeatLoadSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], float | None]
    with_forecast: bool = False


SENSORS: tuple[HeatLoadSensorDescription, ...] = (
    HeatLoadSensorDescription(
        key="current_period",
        translation_key="current_period",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda d: d["current_kwh"],
        with_forecast=True,
    ),
    HeatLoadSensorDescription(
        key="next_24h",
        translation_key="next_24h",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value=lambda d: d["next_24h_kwh"],
    ),
    HeatLoadSensorDescription(
        key="rest_of_today",
        translation_key="rest_of_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value=lambda d: d["rest_of_today_kwh"],
    ),
    HeatLoadSensorDescription(
        key="tomorrow",
        translation_key="tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value=lambda d: d["tomorrow_kwh"],
    ),
    HeatLoadSensorDescription(
        key="peak_power_24h",
        translation_key="peak_power_24h",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value=lambda d: d["peak_kw_24h"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HeatLoadCoordinator = entry.runtime_data
    async_add_entities(HeatLoadSensor(coordinator, desc) for desc in SENSORS)


class HeatLoadSensor(CoordinatorEntity[HeatLoadCoordinator], SensorEntity):
    """One figure from the heating load forecast."""

    _attr_has_entity_name = True
    # The per-period list is large; keep it out of the recorder database.
    _unrecorded_attributes = frozenset({"forecast"})
    entity_description: HeatLoadSensorDescription

    def __init__(self, coordinator: HeatLoadCoordinator, desc: HeatLoadSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{desc.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Heat Load Predictor",
            model="Thermal model",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = self.entity_description.value(self.coordinator.data)
        return None if value is None else round(value, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        attrs: dict[str, Any] = {"period_minutes": data["period_minutes"]}
        if self.entity_description.key == "tomorrow":
            attrs["complete"] = data["tomorrow_complete"]
        if self.entity_description.with_forecast:
            attrs["start_temp_measured"] = data["start_temp_measured"]
            attrs["forecast"] = data["periods"]
        return attrs
