"""Tests for Solplanet payload validation."""

from types import SimpleNamespace

import pytest

from custom_components.solplanet.validation import (
    is_zero_filled_battery_payload,
    normalize_mac_address,
    retain_previous_battery_energy_values,
)


_CORE_FIELDS = ("cst", "bst", "vb", "tb", "soc", "soh", "cli", "clo")


def test_zero_filled_battery_stub_is_detected() -> None:
    """All core battery values at zero identify the transient dongle stub."""
    payload = SimpleNamespace(**dict.fromkeys(_CORE_FIELDS, 0), pb=123)
    assert is_zero_filled_battery_payload(payload)


@pytest.mark.parametrize("field", _CORE_FIELDS)
def test_real_battery_value_prevents_stub_detection(field: str) -> None:
    """Any non-zero core value means the payload contains real telemetry."""
    values = dict.fromkeys(_CORE_FIELDS, 0)
    values[field] = 1
    assert not is_zero_filled_battery_payload(SimpleNamespace(**values))


def test_missing_fields_do_not_look_like_zero_values() -> None:
    """Absent fields cannot be mistaken for an all-zero response."""
    assert not is_zero_filled_battery_payload(SimpleNamespace(cst=0))
    assert not is_zero_filled_battery_payload(None)


def test_regressed_battery_energy_values_are_retained_within_device_day() -> None:
    """Only numeric daily counters that regress are replaced."""
    previous = SimpleNamespace(
        tim="2026-08-08 10:23:52", etdpv=297, ebi=356, ebo=258
    )
    current = SimpleNamespace(
        tim="20260808102752", etdpv=0, ebi=357, ebo=None, pb=5148
    )

    assert retain_previous_battery_energy_values(current, previous) == ("etdpv",)
    assert current.etdpv == 297
    assert current.ebi == 357
    assert current.ebo is None
    assert current.pb == 5148


def test_battery_energy_values_can_reset_on_a_new_device_day() -> None:
    """Daily energy resets are accepted when the payload date advances."""
    previous = SimpleNamespace(
        tim="2026-08-08 23:59:52", etdpv=297, ebi=356, ebo=258
    )
    current = SimpleNamespace(
        tim="2026-08-09 00:00:52", etdpv=0, ebi=0, ebo=0
    )

    assert retain_previous_battery_energy_values(current, previous) == ()
    assert (current.etdpv, current.ebi, current.ebo) == (0, 0, 0)


def test_battery_energy_regression_without_valid_timestamps_is_retained() -> None:
    """An invalid device clock must not allow a corrupt counter regression."""
    previous = SimpleNamespace(tim="", etdpv=297)
    current = SimpleNamespace(tim="", etdpv=0)

    assert retain_previous_battery_energy_values(current, previous) == ("etdpv",)
    assert current.etdpv == 297


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("AABBCCDDEEFF", "aa:bb:cc:dd:ee:ff"),
        ("AA-BB-CC-DD-EE-FF", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
        ("00:00:00:00:00:00", None),
        ("FFFFFFFFFFFF", None),
        ("not-a-mac", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_mac_address(value: object, expected: str | None) -> None:
    """Only real unicast-style device MAC values are returned."""
    assert normalize_mac_address(value) == expected
