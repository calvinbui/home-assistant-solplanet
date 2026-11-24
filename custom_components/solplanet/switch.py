"""Solplanet switch platform."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolplanetConfigEntry
from .const import BATTERY_IDENTIFIER, DISCOVERY_SIGNAL, INVERTER_IDENTIFIER
from .entity import (
    SolplanetEntity,
    SolplanetEntityDescription,
    get_entity_unique_id,
    should_create_entity,
)

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0

# Battery "More Settings" are controlled via Modbus RTU over `fdbg.cgi` using holding-register offsets:
# 1500 power, 1501 sleep flag, 1502 LED color, 1503 LED brightness.
# Power and Sleep are exposed as HA switches and delegate writes via coordinator helper methods.


@dataclass(frozen=True, kw_only=True)
class SolplanetSwitchEntityDescription(SolplanetEntityDescription, SwitchEntityDescription):
    """Describe Solplanet switch entity."""

    coordinator_method: str


class SolplanetSwitch(SolplanetEntity, SwitchEntity):
    """Representation of a Solplanet switch."""

    entity_description: SolplanetSwitchEntityDescription

    @property
    @override
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        try:
            value = self._get_value_from_coordinator()
            return None if value is None else bool(value)
        except Exception:  # noqa: BLE001
            return None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._call_coordinator(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._call_coordinator(False)

    async def _call_coordinator(self, on: bool) -> None:
        """Call the coordinator setter for this switch."""
        method = getattr(self.coordinator, self.entity_description.coordinator_method)
        await method(on)


def create_inverter_switches(isn: str) -> list[SolplanetSwitchEntityDescription]:
    """Create switch entities for inverter settings."""
    return [
        SolplanetSwitchEntityDescription(
            key=f"{isn}_inverter_power",
            translation_key="inverter_power",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="more_settings",
            data_field_path=["power_on"],
            unique_id_suffix="inverter_power",
            coordinator_method="set_inverter_power",
        ),
    ]


def create_battery_switches(isn: str) -> list[SolplanetSwitchEntityDescription]:
    """Create switch entities for battery settings."""
    return [
        SolplanetSwitchEntityDescription(
            key=f"{isn}_battery_power",
            translation_key="battery_power",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="more_settings",
            data_field_path=["power_on"],
            unique_id_suffix="battery_power",
            coordinator_method="set_battery_power",
        ),
        SolplanetSwitchEntityDescription(
            key=f"{isn}_battery_sleep_enabled",
            translation_key="battery_sleep",
            entity_category=EntityCategory.CONFIG,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="more_settings",
            data_field_path=["sleep_enabled"],
            unique_id_suffix="battery_sleep_enabled",
            coordinator_method="set_battery_sleep_enabled",
        ),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches for Solplanet from a config entry."""
    coordinator = entry.runtime_data.coordinator

    description_factories = {
        INVERTER_IDENTIFIER: create_inverter_switches,
        BATTERY_IDENTIFIER: create_battery_switches,
    }
    known_device_ids = {
        device_type: set(coordinator.data[device_type]) for device_type in description_factories
    }

    known_unique_ids: set[str] = set()

    def _create_switches(device_type: str, device_ids: set[str]) -> list[SolplanetSwitch]:
        factory = description_factories[device_type]
        switches: list[SolplanetSwitch] = []
        for device_id in device_ids:
            for description in factory(device_id):
                unique_id = get_entity_unique_id(description, device_id)
                if unique_id in known_unique_ids or not should_create_entity(
                    coordinator, "switch", description, device_id
                ):
                    continue
                known_unique_ids.add(unique_id)
                switches.append(
                    SolplanetSwitch(description=description, isn=device_id, coordinator=coordinator)
                )
        return switches

    @callback
    def _async_add_discovered_switches(
        config_entry_id: str,
        device_type: str,
        device_ids: set[str],
    ) -> None:
        """Add switches for devices found after setup."""
        if config_entry_id != entry.entry_id or device_type not in description_factories:
            return
        new_device_ids = device_ids - known_device_ids[device_type]
        if not new_device_ids:
            return
        known_device_ids[device_type].update(new_device_ids)
        async_add_entities(_create_switches(device_type, new_device_ids))

    @callback
    def _async_add_supported_switches() -> None:
        """Add switches whose settings the dongle returned after setup."""
        new_switches = [
            switch
            for device_type, device_ids in known_device_ids.items()
            for switch in _create_switches(device_type, device_ids)
        ]
        if new_switches:
            async_add_entities(new_switches)

    entry.async_on_unload(async_dispatcher_connect(hass, DISCOVERY_SIGNAL, _async_add_discovered_switches))
    entry.async_on_unload(coordinator.async_add_listener(_async_add_supported_switches))
    async_add_entities(
        switch
        for device_type, device_ids in known_device_ids.items()
        for switch in _create_switches(device_type, device_ids)
    )
