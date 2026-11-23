"""Solplanet base entity."""

import logging
from collections import abc
from dataclasses import dataclass
from typing import Any, cast, override

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BATTERY_IDENTIFIER, DOMAIN, INVERTER_IDENTIFIER
from .coordinator import SolplanetDataUpdateCoordinator
from .exceptions import InverterInSleepModeError

_LOGGER = logging.getLogger(__name__)


def battery_more_setting_supported(coordinator: Any, isn: str, key: str) -> bool:
    """Return whether the dongle has returned a battery "More Settings" value.

    Some battery/dongle combinations answer the Modbus read for these registers with an empty
    or malformed frame, so the settings never populate. Entities backed by them are only created
    once a value has been read; they are added later if a read succeeds.
    """
    settings = coordinator.data.get(BATTERY_IDENTIFIER, {}).get(isn, {}).get("more_settings")
    return isinstance(settings, dict) and settings.get(key) is not None


@dataclass(frozen=True, kw_only=True)
class SolplanetEntityDescription(EntityDescription):
    """Describe Solplanet sensor entity."""

    data_field_device_type: str
    data_field_path: list[str | int]
    data_field_data_type: str
    data_field_NaN_value: int | None = None
    data_field_value_multiply: float | None = None
    data_field_value_mapper: abc.Callable[[Any], Any] | None = None
    unique_id_suffix: str | None = None
    attributes_fn: abc.Callable[[Any], dict[str, Any]] | None = None


def get_entity_unique_id(description: SolplanetEntityDescription, device_id: str) -> str:
    """Return the stable entity-registry unique ID for a description and device."""
    suffix = description.unique_id_suffix or "_".join(
        str(path_item) for path_item in description.data_field_path
    )
    if description.data_field_device_type == INVERTER_IDENTIFIER:
        return f"solplanet_{device_id}_{suffix}"
    return f"solplanet_{description.data_field_device_type}_{device_id}_{suffix}"


class SolplanetEntity(CoordinatorEntity[SolplanetDataUpdateCoordinator], Entity):
    """Base class for Solplanet entities backed by the coordinator.

    Notes:
    - Do not set `entity_id` manually. Home Assistant assigns it via the entity registry.
    - Use `unique_id` for stable entity IDs across restarts.
    """

    entity_description: SolplanetEntityDescription
    unique_id_suffix: str
    _attr_has_entity_name = True

    def __init__(
        self,
        description: SolplanetEntityDescription,
        isn: str,
        coordinator: SolplanetDataUpdateCoordinator,
    ) -> None:
        """Initialize the entity."""
        coordinator = coordinator.runtime.coordinator_for(
            description.data_field_device_type,
            description.data_field_data_type,
        )
        super().__init__(coordinator)
        self.entity_description = description
        self.unique_id_suffix = description.unique_id_suffix or "_".join(
            str(path_item) for path_item in description.data_field_path
        )
        self._isn = isn

        # Stable unique_id for the entity registry
        self._attr_unique_id = get_entity_unique_id(description, isn)

        # Set initial value (may be None if inverter is sleeping / data not ready yet)
        self._set_native_value()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._set_native_value()
        super()._handle_coordinator_update()

    def _set_native_value(self) -> None:
        try:
            self._attr_native_value = self._get_value_from_coordinator()
        except InverterInSleepModeError:
            # When the inverter is sleeping or a specific device payload isn't present,
            # keep the entity "available" and just show unknown (None) state.
            self._attr_native_value = None
            _LOGGER.debug(
                "No data for %s (%s) - inverter may be sleeping",
                self.entity_description.key,
                self._isn,
            )

    def _get_value_from_coordinator(self) -> float | int | str | None:
        """Return the value from coordinator data."""
        try:
            data = self.coordinator.data[self.entity_description.data_field_device_type][self._isn][
                self.entity_description.data_field_data_type
            ]
        except KeyError:
            raise InverterInSleepModeError from None

        for path_item in self.entity_description.data_field_path:
            if isinstance(data, list):
                try:
                    data = data[int(path_item)]
                except IndexError, TypeError, ValueError:
                    return None
            elif hasattr(data, "__dict__"):
                data = getattr(data, str(path_item), None)
            elif isinstance(data, dict):
                data = data.get(path_item)
            else:
                return None

        if data is None:
            return None

        if self.entity_description.data_field_value_mapper is not None:
            data = self.entity_description.data_field_value_mapper(data)

        if (
            self.entity_description.data_field_NaN_value is not None
            and data == self.entity_description.data_field_NaN_value
        ):
            _LOGGER.debug("NaN value received from Inverter")
            return None

        if data is not None and self.entity_description.data_field_value_multiply is not None:
            data = data * self.entity_description.data_field_value_multiply

        return cast(float | int | str | None, data)

    def has_value_in_response(self) -> bool:
        """Return if entity has a non-None value in the latest coordinator payload.

        Note: avoid using this to decide whether to add entities. If the inverter is slow/sleeping
        at startup, entities would never be created.
        """
        try:
            return self._get_value_from_coordinator() is not None
        except InverterInSleepModeError:
            return False

    @property
    @override
    def available(self) -> bool:
        """Return entity availability.

        A missing value remains Unknown while the device stays available. Endpoint failures and
        devices no longer present in the latest inventory are unavailable.
        """
        return (
            self.coordinator.last_update_success
            and self._isn not in self.coordinator.failed_device_ids
            and self._isn in self.coordinator.data.get(self.entity_description.data_field_device_type, {})
        )

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information about this sensor."""
        device_type = self.entity_description.data_field_device_type
        if device_type == INVERTER_IDENTIFIER:
            return DeviceInfo(identifiers={(DOMAIN, self._isn)})
        return DeviceInfo(identifiers={(DOMAIN, f"{device_type}_{self._isn or ''}")})

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if not self.entity_description.attributes_fn:
            return None

        try:
            data = self.coordinator.data[self.entity_description.data_field_device_type][self._isn][
                self.entity_description.data_field_data_type
            ]
        except KeyError:
            return None

        try:
            return self.entity_description.attributes_fn(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error getting attributes: %s", err)
            return None
