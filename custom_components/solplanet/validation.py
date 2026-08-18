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
_BATTERY_ENERGY_RESET_CONFIRMATIONS = 2
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


def retain_previous_battery_energy_values(
    data: object,
    previous_data: object,
    candidates: dict[str, tuple[float | int, int]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Retain regressed daily counters until a reset is confirmed."""
    retained: list[str] = []
    confirmed: list[str] = []
    for field in _BATTERY_DAILY_ENERGY_FIELDS:
        previous_value = getattr(previous_data, field, None)
        current_value = getattr(data, field, None)
        if not isinstance(previous_value, (int, float)) or not isinstance(
            current_value, (int, float)
        ):
            candidates.pop(field, None)
            continue
        if current_value >= previous_value:
            candidates.pop(field, None)
            continue

        candidate = candidates.get(field)
        if candidate is not None and current_value >= candidate[0]:
            confirmation_count = candidate[1] + 1
            if confirmation_count >= _BATTERY_ENERGY_RESET_CONFIRMATIONS:
                candidates.pop(field)
                confirmed.append(field)
                continue
        else:
            confirmation_count = 0

        candidates[field] = (current_value, confirmation_count)
        setattr(data, field, previous_value)
        retained.append(field)

    return tuple(retained), tuple(confirmed)
