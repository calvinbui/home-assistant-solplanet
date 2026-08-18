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


def test_regressed_battery_energy_values_are_retained_by_field() -> None:
    """Only numeric daily counters that regress are retained."""
    previous = SimpleNamespace(etdpv=297, ebi=356, ebo=258)
    current = SimpleNamespace(etdpv=0, ebi=357, ebo=None, pb=5148)
    candidates: dict[str, tuple[float | int, int]] = {"ebi": (100, 1), "ebo": (200, 1)}

    assert retain_previous_battery_energy_values(current, previous, candidates) == (
        ("etdpv",),
        (),
    )
    assert current.etdpv == 297
    assert current.ebi == 357
    assert current.ebo is None
    assert current.pb == 5148
    assert candidates == {"etdpv": (0, 0)}


def test_battery_energy_reset_requires_two_confirmations() -> None:
    """A reset is exposed only after two consistent follow-up payloads."""
    previous = SimpleNamespace(etdpv=297)
    candidates: dict[str, tuple[float | int, int]] = {}
    first = SimpleNamespace(etdpv=0)
    second = SimpleNamespace(etdpv=1)
    third = SimpleNamespace(etdpv=2)

    assert retain_previous_battery_energy_values(first, previous, candidates) == (
        ("etdpv",),
        (),
    )
    assert retain_previous_battery_energy_values(second, previous, candidates) == (
        ("etdpv",),
        (),
    )
    assert retain_previous_battery_energy_values(third, previous, candidates) == (
        (),
        ("etdpv",),
    )
    assert (first.etdpv, second.etdpv, third.etdpv) == (297, 297, 2)
    assert candidates == {}


def test_battery_energy_regression_recovery_clears_candidate() -> None:
    """A counter recovering after two low payloads is not treated as a reset."""
    previous = SimpleNamespace(etdpv=297)
    candidates: dict[str, tuple[float | int, int]] = {}
    first_low = SimpleNamespace(etdpv=0)
    second_low = SimpleNamespace(etdpv=1)
    recovered = SimpleNamespace(etdpv=298)

    retain_previous_battery_energy_values(first_low, previous, candidates)
    retain_previous_battery_energy_values(second_low, previous, candidates)

    assert retain_previous_battery_energy_values(recovered, previous, candidates) == (
        (),
        (),
    )
    assert (first_low.etdpv, second_low.etdpv) == (297, 297)
    assert recovered.etdpv == 298
    assert candidates == {}


def test_lower_battery_energy_follow_up_restarts_confirmation() -> None:
    """A further decrease starts a new confirmation sequence."""
    previous = SimpleNamespace(etdpv=297)
    candidates: dict[str, tuple[float | int, int]] = {"etdpv": (2, 1)}
    current = SimpleNamespace(etdpv=1)

    assert retain_previous_battery_energy_values(current, previous, candidates) == (
        ("etdpv",),
        (),
    )
    assert current.etdpv == 297
    assert candidates == {"etdpv": (1, 0)}


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
