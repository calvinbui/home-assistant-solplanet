"""Validation helpers for Solplanet device payloads."""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import format_mac


_BATTERY_ZERO_STUB_FIELDS = (
    "cst",
    "bst",
    "vb",
    "tb",
    "soc",
    "soh",
    "cli",
    "clo",
)
_BATTERY_DAILY_ENERGY_FIELDS = ("etdpv", "ebi", "ebo")
_MAC_ADDRESS_PATTERN = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")
_INVALID_MAC_ADDRESSES = {
    "00:00:00:00:00:00",
    "ff:ff:ff:ff:ff:ff",
}


def normalize_mac_address(value: object) -> str | None:
    """Return a usable normalized network MAC address."""
    if not isinstance(value, str) or not value:
        return None

    normalized = format_mac(value)
    if (
        not _MAC_ADDRESS_PATTERN.fullmatch(normalized)
        or normalized in _INVALID_MAC_ADDRESSES
        or int(normalized[:2], 16) & 1
    ):
        return None
    return normalized


def is_zero_filled_battery_payload(data: object) -> bool:
    """Return whether battery data matches the dongle's transient zero stub.

    A real battery may legitimately report zero for individual values such as
    power, current, or even SOC. The transient dongle response is distinct: all
    core status, health, and electrical values are zero at once.
    """
    return all(getattr(data, field, None) == 0 for field in _BATTERY_ZERO_STUB_FIELDS)


def retain_previous_battery_energy_values(data: object, previous_data: object) -> tuple[str, ...]:
    """Retain daily energy counters when a same-day payload regresses."""
    previous_day = _battery_payload_day(previous_data)
    current_day = _battery_payload_day(data)
    if previous_day is not None and current_day is not None and previous_day != current_day:
        return ()

    retained: list[str] = []
    for field in _BATTERY_DAILY_ENERGY_FIELDS:
        previous_value = getattr(previous_data, field, None)
        current_value = getattr(data, field, None)
        if not isinstance(previous_value, (int, float)) or not isinstance(
            current_value, (int, float)
        ):
            continue
        if current_value >= previous_value:
            continue
        setattr(data, field, previous_value)
        retained.append(field)

    return tuple(retained)


def _battery_payload_day(data: object) -> str | None:
    """Return the device-local calendar day from a battery payload timestamp."""
    timestamp = getattr(data, "tim", None)
    if not isinstance(timestamp, str):
        return None
    digits = re.sub(r"\D", "", timestamp)
    return digits[:8] if len(digits) >= 8 else None
