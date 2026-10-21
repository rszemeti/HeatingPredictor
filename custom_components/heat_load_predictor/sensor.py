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
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BATTERY_ENABLED, DOMAIN, VERSION
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
    entities: list[SensorEntity] = [HeatLoadSensor(coordinator, desc) for desc in SENSORS]
    if entry.options.get(CONF_BATTERY_ENABLED):
        entities += [BatteryPlanSensor(coordinator), BatteryMinSocSensor(coordinator)]
    async_add_entities(entities)


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


class _BatteryBase(CoordinatorEntity[HeatLoadCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HeatLoadCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def _plan(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        plan = data.get("battery")
        return None if not plan or "error" in plan else plan

    @property
    def available(self) -> bool:
        return super().available and self._plan is not None


class BatteryPlanSensor(_BatteryBase):
    """The battery plan: summary as state, full plan in attributes."""

    _unrecorded_attributes = frozenset({"windows", "slots", "soc_forecast", "next"})

    def __init__(self, coordinator: HeatLoadCoordinator) -> None:
        super().__init__(coordinator, "battery_plan")

    @property
    def native_value(self) -> str | None:
        plan = self._plan
        return plan["summary"] if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        plan = self._plan
        if not plan:
            return None
        return {k: plan[k] for k in ("soc_now", "solar_forecast", "next", "slots", "windows", "soc_forecast")}


class BatteryMinSocSensor(_BatteryBase):
    """Lowest projected battery SOC over the next 24 hours."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: HeatLoadCoordinator) -> None:
        super().__init__(coordinator, "battery_min_soc_24h")

    @property
    def native_value(self) -> float | None:
        plan = self._plan
        return plan["min_soc_24h"] if plan else None
