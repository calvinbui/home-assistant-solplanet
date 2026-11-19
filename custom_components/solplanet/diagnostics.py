"""Diagnostics support for the Solplanet integration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import SolplanetConfigEntry
from .const import (
    BATTERY_IDENTIFIER,
    CONF_INTERVAL,
    DEFAULT_INTERVAL,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)
from .coordinator import SolplanetDataUpdateCoordinator

DEVICE_TYPES = (
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    BATTERY_IDENTIFIER,
    METER_IDENTIFIER,
)
PROTOCOL_DEVICE_TYPES = {
    "v1": (INVERTER_IDENTIFIER, METER_IDENTIFIER),
    "v2": DEVICE_TYPES,
}

COORDINATORS = {
    "metadata": "metadata_coordinator",
    "inverter": "inverter_coordinator",
    "battery": "battery_coordinator",
    "meter": "meter_coordinator",
    "dongle": "dongle_coordinator",
}


def _field(value: Any, name: str) -> Any:
    """Read a field from either an API dictionary or response model."""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _dongle_diagnostics(device_data: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted dongle summary."""
    info = device_data.get("data") or {}
    network = device_data.get("network") or {}
    return {
        "device_id": REDACTED,
        "serial_number": REDACTED,
        "manufacturer": _field(info, "brd") or _field(info, "muf"),
        "model": _field(info, "mod"),
        "hardware_version": _field(info, "hw"),
        "software_version": _field(info, "sw"),
        "network_mode": _field(network, "mode"),
        "signal_strength": _field(network, "srh"),
        "warnings_available": device_data.get("warnings") is not None,
        "warnings_present": bool(device_data.get("warnings")),
    }


def _inverter_diagnostics(device_data: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted inverter summary."""
    info = device_data.get("info") or {}
    return {
        "device_id": REDACTED,
        "serial_number": REDACTED,
        "model": _field(info, "model"),
        "rated_power_w": _field(info, "rate"),
        "main_software_version": _field(info, "msw"),
        "slave_software_version": _field(info, "ssw"),
        "security_software_version": _field(info, "tsw"),
        "communication_version": _field(info, "cmv"),
        "data_available": device_data.get("data") is not None,
        "settings_available": bool(device_data.get("more_settings")),
    }


def _battery_diagnostics(device_data: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted battery summary."""
    info = device_data.get("info") or {}
    battery = _field(info, "battery") or {}
    return {
        "device_id": REDACTED,
        "serial_number": REDACTED,
        "manufacturer_code": _field(info, "muf"),
        "model_code": _field(info, "mod"),
        "battery_type": _field(info, "type"),
        "battery_count": _field(info, "num"),
        "module_count": _field(battery, "modeltotal"),
        "hardware_version": _field(battery, "hardwarever"),
        "software_version": _field(battery, "softwarever"),
        "data_available": device_data.get("data") is not None,
        "schedule_available": bool(device_data.get("schedule")),
    }


def _meter_diagnostics(device_data: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted meter summary."""
    info = device_data.get("app_info") or device_data.get("info") or {}
    return {
        "device_id": REDACTED,
        "serial_number": REDACTED,
        "manufacturer": _field(info, "manufactory"),
        "model": _field(info, "name") or _field(info, "equipModel"),
        "meter_type": _field(info, "type"),
        "data_available": any(device_data.get(data_type) is not None for data_type in ("data", "app_data")),
        "settings_available": bool(device_data.get("meter_req")),
    }


DEVICE_DIAGNOSTICS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    DONGLE_IDENTIFIER: _dongle_diagnostics,
    INVERTER_IDENTIFIER: _inverter_diagnostics,
    BATTERY_IDENTIFIER: _battery_diagnostics,
    METER_IDENTIFIER: _meter_diagnostics,
}


def _coordinator_status(
    coordinator: SolplanetDataUpdateCoordinator | None,
) -> dict[str, Any]:
    """Return non-sensitive update state for one endpoint coordinator."""
    if coordinator is None:
        return {"initialized": False}

    update_interval = coordinator.update_interval
    return {
        "initialized": True,
        "last_update_success": coordinator.last_update_success,
        "update_interval_seconds": (update_interval.total_seconds() if update_interval is not None else None),
        "failed_device_count": len(coordinator.failed_device_ids),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a Solplanet config entry."""
    runtime = entry.runtime_data

    return {
        "config_entry": {
            "title": REDACTED,
            "unique_id": REDACTED,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(
                {
                    CONF_HOST: entry.data.get(CONF_HOST),
                    CONF_INTERVAL: entry.data.get(CONF_INTERVAL, DEFAULT_INTERVAL),
                },
                {CONF_HOST},
            ),
        },
        "protocol_version": runtime.api.version,
        "supported_device_types": list(PROTOCOL_DEVICE_TYPES.get(runtime.api.version, DEVICE_TYPES)),
        "device_counts": {
            device_type: len(runtime.data.get(device_type, {})) for device_type in DEVICE_TYPES
        },
        "coordinators": {
            name: _coordinator_status(getattr(runtime, attribute)) for name, attribute in COORDINATORS.items()
        },
        "devices": {
            device_type: [
                DEVICE_DIAGNOSTICS[device_type](device_data)
                for device_data in runtime.data.get(device_type, {}).values()
            ]
            for device_type in DEVICE_TYPES
        },
    }
