"""Solplanet binary sensor platform."""

import logging
from dataclasses import dataclass
from typing import cast, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolplanetConfigEntry
from .client import BatterySchedule
from .const import BATTERY_IDENTIFIER, DISCOVERY_SIGNAL, INVERTER_IDENTIFIER
from .coordinator import SolplanetDataUpdateCoordinator
from .entity import SolplanetEntity, SolplanetEntityDescription, get_entity_unique_id, should_create_entity

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SolplanetBinarySensorEntityDescription(SolplanetEntityDescription, BinarySensorEntityDescription):
    """Describe Solplanet binary sensor entity."""


class SolplanetBinarySensor(SolplanetEntity, BinarySensorEntity):
    """Representation of a Solplanet binary sensor."""

    entity_description: SolplanetBinarySensorEntityDescription

    def __init__(
        self,
        description: SolplanetBinarySensorEntityDescription,
        isn: str,
        coordinator: SolplanetDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(description=description, isn=isn, coordinator=coordinator)
        self._attr_is_on = None  # Initialize the binary sensor state

    @property
    @override
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        return cast(bool | None, self._get_value_from_coordinator())


def create_battery_binary_sensors(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetBinarySensorEntityDescription]:
    """Create binary sensors for battery."""

    def value_mapper(raw: object) -> bool | None:
        if not isinstance(raw, dict):
            return None
        has_schedule = any(
            any(code != 0 for code in raw.get(day, [])) for day in BatterySchedule.DAYS if day in raw
        )
        return has_schedule

    return [
        SolplanetBinarySensorEntityDescription(
            key=f"{isn}_schedule_configured",
            translation_key="schedule_configured",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="schedule",
            data_field_path=["raw"],
            data_field_value_mapper=value_mapper,
            entity_category=EntityCategory.DIAGNOSTIC,
            attributes_fn=lambda schedule: {
                "raw_schedule": schedule["raw"],
                "formatted_schedule": {
                    day: [slot.human_readable() for slot in day_slots]
                    for day, day_slots in schedule["slots"].items()
                },
                "pin": schedule["Pin"],
                "pout": schedule["Pout"],
            },
        ),
    ]


def create_inverter_binary_sensors(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetBinarySensorEntityDescription]:
    """Create binary sensors for inverter.

    Grid status is only offered once the inverter has reported it (or if it is already registered).
    Some V2 inverters never return it in the live JSON and reject the Modbus fallback register, so it
    would stay Unknown forever.
    """
    grid_status = SolplanetBinarySensorEntityDescription(
        key=f"{isn}_grid_sts",
        translation_key="grid_status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        data_field_device_type=INVERTER_IDENTIFIER,
        data_field_data_type="data",
        data_field_path=["grid_sts"],
        data_field_value_mapper=lambda v: {0: False, 1: True}.get(v),
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    return [grid_status] if should_create_entity(coordinator, "binary_sensor", grid_status, isn) else []


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data.coordinator
    api = entry.runtime_data.api

    description_factories = {BATTERY_IDENTIFIER: create_battery_binary_sensors}
    if api.version == "v2":
        description_factories[INVERTER_IDENTIFIER] = create_inverter_binary_sensors

    known_device_ids = {
        device_type: set(coordinator.data[device_type]) for device_type in description_factories
    }

    known_unique_ids: set[str] = set()

    def _create_sensors(device_type: str, device_ids: set[str]) -> list[SolplanetBinarySensor]:
        factory = description_factories[device_type]
        sensors: list[SolplanetBinarySensor] = []
        for device_id in device_ids:
            for description in factory(coordinator, device_id):
                unique_id = get_entity_unique_id(description, device_id)
                if unique_id in known_unique_ids:
                    continue
                known_unique_ids.add(unique_id)
                sensors.append(
                    SolplanetBinarySensor(description=description, isn=device_id, coordinator=coordinator)
                )
        return sensors

    sensors = [
        sensor
        for device_type, device_ids in known_device_ids.items()
        for sensor in _create_sensors(device_type, device_ids)
    ]

    @callback
    def _async_add_discovered_sensors(
        config_entry_id: str,
        device_type: str,
        device_ids: set[str],
    ) -> None:
        """Add binary sensors for devices found after setup."""
        if config_entry_id != entry.entry_id or device_type not in description_factories:
            return
        new_device_ids = device_ids - known_device_ids[device_type]
        if not new_device_ids:
            return
        known_device_ids[device_type].update(new_device_ids)
        async_add_entities(_create_sensors(device_type, new_device_ids))

    @callback
    def _async_add_reported_sensors() -> None:
        """Add inverter sensors whose value the inverter first reported after setup."""
        new_sensors = _create_sensors(INVERTER_IDENTIFIER, known_device_ids[INVERTER_IDENTIFIER])
        if new_sensors:
            async_add_entities(new_sensors)

    entry.async_on_unload(async_dispatcher_connect(hass, DISCOVERY_SIGNAL, _async_add_discovered_sensors))
    if INVERTER_IDENTIFIER in description_factories:
        entry.async_on_unload(
            coordinator.runtime.coordinator_for(INVERTER_IDENTIFIER, "data").async_add_listener(
                _async_add_reported_sensors
            )
        )

    # Entities backed by an optional inverter value are added once that value is reported.
    async_add_entities(sensors)
