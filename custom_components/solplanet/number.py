"""Solplanet number platform."""

import logging
from collections import abc
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolplanetConfigEntry
from .const import BATTERY_IDENTIFIER, BATTERY_MODELS_WITH_LED, DISCOVERY_SIGNAL
from .coordinator import SolplanetDataUpdateCoordinator
from .entity import SolplanetEntity, SolplanetEntityDescription, get_entity_unique_id

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SolplanetNumberEntityDescription(SolplanetEntityDescription, NumberEntityDescription):
    """Describe Solplanet number entity."""

    callback: abc.Callable[[float], Any]


class SolplanetNumber(SolplanetEntity, NumberEntity):
    """Representation of a Solplanet number."""

    entity_description: SolplanetNumberEntityDescription
    _attr_native_value: float | None

    def __init__(
        self,
        description: SolplanetNumberEntityDescription,
        isn: str,
        coordinator: SolplanetDataUpdateCoordinator,
    ) -> None:
        """Initialize the number."""
        super().__init__(description=description, isn=isn, coordinator=coordinator)

    @property
    @override
    def native_max_value(self) -> float:
        """Return max value.

        Some values depend on inverter model (e.g. export / schedule power), so we scale
        the UI range dynamically using the inverter `rate`.
        """
        if self.entity_description.key.endswith(("_schedule_pin", "_schedule_pout")):
            return float(self.coordinator.get_max_inverter_rate_w())
        return super().native_max_value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set the selected value."""
        await self.entity_description.callback(value)


def create_battery_entities_description(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetNumberEntityDescription]:
    """Create entities for battery."""
    battery_info = coordinator.data[BATTERY_IDENTIFIER][isn].get("info")
    has_led = (
        (battery_info.muf, battery_info.mod) in BATTERY_MODELS_WITH_LED
        if battery_info and battery_info.muf is not None and battery_info.mod is not None
        else False
    )

    entities = [
        SolplanetNumberEntityDescription(
            key=f"{isn}_soc_max",
            translation_key="soc_max",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="info",
            data_field_path=["charge_max"],
            native_min_value=10,
            native_max_value=100,
            native_step=1,
            native_unit_of_measurement=PERCENTAGE,
            callback=lambda value: coordinator.set_battery_soc_max(isn, int(value)),
        ),
        SolplanetNumberEntityDescription(
            key=f"{isn}_soc_min",
            translation_key="soc_min",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="info",
            data_field_path=["discharge_max"],
            native_min_value=5,
            native_max_value=100,
            native_step=1,
            native_unit_of_measurement=PERCENTAGE,
            callback=lambda value: coordinator.set_battery_soc_min(isn, int(value)),
        ),
        SolplanetNumberEntityDescription(
            key=f"{isn}_schedule_pin",
            translation_key="schedule_input_power",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="schedule",
            data_field_path=["Pin"],  # Changed to use dict key
            native_min_value=0,
            native_max_value=10000,
            native_step=1,
            native_unit_of_measurement=UnitOfPower.WATT,
            callback=lambda value: coordinator.set_battery_schedule_pin(isn, int(value)),
        ),
        SolplanetNumberEntityDescription(
            key=f"{isn}_schedule_pout",
            translation_key="schedule_output_power",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="schedule",
            data_field_path=["Pout"],  # Changed to use dict key
            native_min_value=0,
            native_max_value=10000,
            native_step=1,
            native_unit_of_measurement=UnitOfPower.WATT,
            callback=lambda value: coordinator.set_battery_schedule_pout(isn, int(value)),
        ),
    ]

    if has_led:
        entities.append(
            SolplanetNumberEntityDescription(
                key=f"{isn}_led_brightness",
                translation_key="led_brightness",
                entity_category=EntityCategory.CONFIG,
                data_field_device_type=BATTERY_IDENTIFIER,
                data_field_data_type="more_settings",
                data_field_path=["led_brightness"],
                native_min_value=0,
                native_max_value=100,
                native_step=1,
                native_unit_of_measurement=PERCENTAGE,
                callback=lambda value: coordinator.set_battery_led_brightness(int(value)),
            )
        )

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for Solplanet from a config entry."""
    coordinator = entry.runtime_data.coordinator

    known_unique_ids: set[str] = set()

    def _create_numbers(device_ids: set[str]) -> list[SolplanetNumber]:
        new_numbers: list[SolplanetNumber] = []
        for device_id in device_ids:
            for description in create_battery_entities_description(coordinator, device_id):
                unique_id = get_entity_unique_id(description, device_id)
                if unique_id in known_unique_ids:
                    continue
                number = SolplanetNumber(
                    description=description,
                    isn=device_id,
                    coordinator=coordinator,
                )
                known_unique_ids.add(unique_id)
                new_numbers.append(number)
        return new_numbers

    @callback
    def _async_add_discovered_numbers(
        config_entry_id: str,
        device_type: str,
        device_ids: set[str],
    ) -> None:
        """Add numbers for batteries found after setup."""
        if config_entry_id != entry.entry_id or device_type != BATTERY_IDENTIFIER:
            return
        async_add_entities(_create_numbers(device_ids))

    @callback
    def _async_add_metadata_descriptions() -> None:
        """Add controls for battery capabilities first reported after setup."""
        new_numbers = _create_numbers(set(coordinator.data[BATTERY_IDENTIFIER]))
        if new_numbers:
            async_add_entities(new_numbers)

    entry.async_on_unload(async_dispatcher_connect(hass, DISCOVERY_SIGNAL, _async_add_discovered_numbers))
    entry.async_on_unload(coordinator.async_add_listener(_async_add_metadata_descriptions))

    # Always add entities; values may be missing during startup/inverter sleep.
    async_add_entities(_create_numbers(set(coordinator.data[BATTERY_IDENTIFIER])))
