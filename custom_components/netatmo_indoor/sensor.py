"""Humidity, noise, and the actual source measurement timestamp."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfSoundPressure
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import IndoorConfigEntry
from .const import DOMAIN, MAX_MEASUREMENT_AGE
from .coordinator import IndoorCoordinator

SENSORS = (
    SensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="noise",
        translation_key="noise",
        device_class=SensorDeviceClass.SOUND_PRESSURE,
        native_unit_of_measurement=UnitOfSoundPressure.DECIBEL,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="measured_at",
        translation_key="measured_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IndoorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create only indoor entities, including currently missing readings."""
    async_add_entities(IndoorSensor(entry.runtime_data, desc) for desc in SENSORS)


class IndoorSensor(CoordinatorEntity[IndoorCoordinator], SensorEntity):
    """Never substitute zero or the polling time for a missing measurement."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: IndoorCoordinator, description: SensorEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        unit = coordinator.data
        self._attr_unique_id = f"{unit.device_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unit.device_id)},
            manufacturer="Netatmo",
            model=unit.model,
            name=unit.name,
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        unit = self.coordinator.data
        if self.entity_description.key == "measured_at":
            return unit.measured_at is not None
        if not unit.reachable or unit.measured_at is None:
            return False
        age = (dt_util.utcnow() - unit.measured_at).total_seconds()
        return 0 <= age <= MAX_MEASUREMENT_AGE

    @property
    def native_value(self) -> float | datetime | None:
        unit = self.coordinator.data
        if self.entity_description.key == "humidity":
            return unit.humidity
        if self.entity_description.key == "noise":
            return unit.noise
        return unit.measured_at

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        unit = self.coordinator.data
        return {
            "measurement_time": (
                unit.measured_at.isoformat() if unit.measured_at else None
            ),
            "reachable": unit.reachable,
        }
