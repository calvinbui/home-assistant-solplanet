"""Solplanet selects platform."""

import logging
from collections import abc
from dataclasses import dataclass
from typing import Any, cast, override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolplanetConfigEntry
from .const import BATTERY_IDENTIFIER, BATTERY_MODELS_WITH_LED, DISCOVERY_SIGNAL
from .coordinator import SolplanetDataUpdateCoordinator
from .entity import (
    SolplanetEntity,
    SolplanetEntityDescription,
    battery_more_setting_supported,
    get_entity_unique_id,
)

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


class SolplanetSelectOption:
    """Representation of a Solplanet select option."""

    def __init__(self, label: str, value: Any) -> None:
        """Initialize the select option."""
        self.label = label
        self.value = value


@dataclass(frozen=True, kw_only=True)
class SolplanetSelectEntityDescription(SolplanetEntityDescription, SelectEntityDescription):
    """Describe Solplanet select entity."""

    callback: abc.Callable[[SolplanetSelectOption], Any]
    get_options: abc.Callable[[], list[SolplanetSelectOption]]


class SolplanetSelect(SolplanetEntity, SelectEntity):
    """Representation of a Solplanet select."""

    entity_description: SolplanetSelectEntityDescription
    _attr_native_value: str | None
    _select_options: list[SolplanetSelectOption]

    def __init__(
        self,
        description: SolplanetSelectEntityDescription,
        isn: str,
        coordinator: SolplanetDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(description=description, isn=isn, coordinator=coordinator)
        self._refresh_options()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        super()._handle_coordinator_update()
        self._refresh_options()

    @override
    def _set_native_value(self) -> None:
        super()._set_native_value()
        self._attr_current_option = self._attr_native_value

    @override
    async def async_select_option(self, option: str) -> None:
        """Handle the option selection."""
        item = next((x for x in self._select_options if x.label == option), None)

        if item is not None:
            await self.entity_description.callback(item)

    def _refresh_options(self) -> None:
        self._select_options = self.entity_description.get_options()
        self._attr_options = [x.label for x in self._select_options]


def create_battery_entities_description(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetSelectEntityDescription]:
    """Create entities for battery."""
    LED_COLOR_MAP: dict[int, dict[str, str]] = {
        1: {"name": "Cyan", "hex": "#67F9FD"},
        2: {"name": "Mint", "hex": "#69F9CB"},
        3: {"name": "Lime", "hex": "#6CF86C"},
        4: {"name": "Pink", "hex": "#F3B0FC"},
        5: {"name": "Purple", "hex": "#C2B2FB"},
    }

    def _format_led_color_label(index: int) -> str:
        entry = LED_COLOR_MAP.get(index)
        if entry:
            return entry["name"]
        return f"Index {index}"

    def _get_led_color_options() -> list[SolplanetSelectOption]:
        # The device exposes a fixed LED palette (indices 1-5). Include the current value
        # if it ever reports something outside the known range.
        current = (
            coordinator.data.get(BATTERY_IDENTIFIER, {})
            .get(isn, {})
            .get("more_settings", {})
            .get("led_color_index")
        )

        indices = set(LED_COLOR_MAP.keys())
        if isinstance(current, int):
            indices.add(current)

        return [SolplanetSelectOption(label=_format_led_color_label(i), value=i) for i in sorted(indices)]

    def _get_led_color_attributes(settings: Any) -> dict[str, Any]:
        if not isinstance(settings, dict):
            return {"index": None, "hex": None}

        index = settings.get("led_color_index")
        color = LED_COLOR_MAP.get(cast(int, index))
        return {
            "index": index,
            "hex": color.get("hex") if color is not None else None,
        }

    battery_info = coordinator.data[BATTERY_IDENTIFIER][isn].get("info")
    has_led = (
        (battery_info.muf, battery_info.mod) in BATTERY_MODELS_WITH_LED
        if battery_info and battery_info.muf is not None and battery_info.mod is not None
        else False
    ) and battery_more_setting_supported(coordinator, isn, "led_color_index")

    entities = [
        SolplanetSelectEntityDescription(
            key=f"{isn}_work_mode",
            translation_key="work_mode",
            entity_category=EntityCategory.CONFIG,
            unique_id_suffix="work_mode",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="work_modes",
            data_field_path=["selected", "name"],
            get_options=lambda: [
                SolplanetSelectOption(label=x.name, value=x)
                for x in coordinator.data[BATTERY_IDENTIFIER][isn]["work_modes"]["all"]
            ],
            callback=lambda option: coordinator.set_battery_config(isn, mode=option.value),
        ),
    ]

    if has_led:
        entities.append(
            SolplanetSelectEntityDescription(
                key=f"{isn}_led_color",
                translation_key="led_color",
                entity_category=EntityCategory.CONFIG,
                unique_id_suffix="led_color",
                data_field_device_type=BATTERY_IDENTIFIER,
                data_field_data_type="more_settings",
                data_field_path=["led_color_index"],
                # Entity expects a string option; we store int in value and use label for display.
                get_options=_get_led_color_options,
                callback=lambda option: coordinator.set_battery_led_color_index(int(option.value)),
                data_field_value_mapper=lambda v: _format_led_color_label(int(v)) if v is not None else None,
                attributes_fn=_get_led_color_attributes,
            )
        )

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities for Solplanet from a config entry."""
    coordinator = entry.runtime_data.coordinator

    known_unique_ids: set[str] = set()

    def _create_selects(device_ids: set[str]) -> list[SolplanetSelect]:
        new_selects: list[SolplanetSelect] = []
        for device_id in device_ids:
            for description in create_battery_entities_description(coordinator, device_id):
                unique_id = get_entity_unique_id(description, device_id)
                if unique_id in known_unique_ids:
                    continue
                select = SolplanetSelect(
                    description=description,
                    isn=device_id,
                    coordinator=coordinator,
                )
                known_unique_ids.add(unique_id)
                new_selects.append(select)
        return new_selects

    @callback
    def _async_add_discovered_selects(
        config_entry_id: str,
        device_type: str,
        device_ids: set[str],
    ) -> None:
        """Add selects for batteries found after setup."""
        if config_entry_id != entry.entry_id or device_type != BATTERY_IDENTIFIER:
            return
        async_add_entities(_create_selects(device_ids))

    @callback
    def _async_add_metadata_descriptions() -> None:
        """Add controls for battery capabilities first reported after setup."""
        new_selects = _create_selects(set(coordinator.data[BATTERY_IDENTIFIER]))
        if new_selects:
            async_add_entities(new_selects)

    entry.async_on_unload(async_dispatcher_connect(hass, DISCOVERY_SIGNAL, _async_add_discovered_selects))
    entry.async_on_unload(coordinator.async_add_listener(_async_add_metadata_descriptions))

    # Always add entities; values may be missing during startup/inverter sleep.
    async_add_entities(_create_selects(set(coordinator.data[BATTERY_IDENTIFIER])))
