"""Tests for the common Solplanet entity implementation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    DOMAIN,
    INVERTER_IDENTIFIER,
)
from custom_components.solplanet.entity import (
    SolplanetEntity,
    SolplanetEntityDescription,
    get_entity_unique_id,
)
from custom_components.solplanet.exceptions import InverterInSleepModeError
from tests.helpers import FakeCoordinator


def _description(**kwargs) -> SolplanetEntityDescription:
    values = {
        "key": "power",
        "name": "Power",
        "data_field_device_type": INVERTER_IDENTIFIER,
        "data_field_data_type": "data",
        "data_field_path": ["power"],
    }
    values.update(kwargs)
    return SolplanetEntityDescription(**values)


def _coordinator(payload) -> FakeCoordinator:
    return FakeCoordinator(
        {
            INVERTER_IDENTIFIER: {"inv-1": {"data": payload}},
            BATTERY_IDENTIFIER: {"bat-1": {"data": payload}},
        }
    )


def test_unique_id_uses_stable_device_type_and_optional_suffix() -> None:
    """Entity unique IDs retain the legacy inverter format and disambiguate other devices."""
    assert get_entity_unique_id(_description(data_field_path=["vac", 1]), "INV") == ("solplanet_INV_vac_1")
    battery = _description(
        data_field_device_type=BATTERY_IDENTIFIER,
        unique_id_suffix="state",
    )
    assert get_entity_unique_id(battery, "BAT") == "solplanet_battery_BAT_state"


def test_entity_reads_dict_object_and_list_paths_and_transforms_value() -> None:
    """Nested values can traverse mappings, response objects and lists."""
    coordinator = _coordinator({"response": SimpleNamespace(values=[2, 3])})
    entity = SolplanetEntity(
        _description(
            data_field_path=["response", "values", 1],
            data_field_value_mapper=lambda value: value + 1,
            data_field_value_multiply=10,
        ),
        "inv-1",
        coordinator,
    )
    assert entity._attr_native_value == 40
    assert entity.unique_id == "solplanet_inv-1_response_values_1"


def test_missing_invalid_and_nan_values_become_unknown() -> None:
    """Missing fields, invalid list indices and sentinel values map to Unknown."""
    coordinator = _coordinator({"values": [1]})
    out_of_range = SolplanetEntity(_description(data_field_path=["values", 9]), "inv-1", coordinator)
    assert out_of_range._attr_native_value is None

    invalid_index = SolplanetEntity(_description(data_field_path=["values", "invalid"]), "inv-1", coordinator)
    assert invalid_index._attr_native_value is None

    scalar_path = SolplanetEntity(_description(data_field_path=["values", 0, "child"]), "inv-1", coordinator)
    assert scalar_path._attr_native_value is None

    nan = SolplanetEntity(
        _description(data_field_path=["values", 0], data_field_NaN_value=1),
        "inv-1",
        coordinator,
    )
    assert nan._attr_native_value is None


def test_missing_device_payload_is_sleep_mode_and_entity_stays_unknown() -> None:
    """A sleeping inverter's absent payload does not prevent entity creation."""
    coordinator = FakeCoordinator({INVERTER_IDENTIFIER: {}})
    entity = SolplanetEntity(_description(), "inv-1", coordinator)
    assert entity._attr_native_value is None
    assert not entity.has_value_in_response()
    try:
        entity._get_value_from_coordinator()
    except InverterInSleepModeError:
        pass
    else:
        raise AssertionError("missing payload did not raise the sleep-mode marker")


def test_coordinator_update_refreshes_value_before_writing_state() -> None:
    """Coordinator notifications refresh the cached native value."""
    coordinator = _coordinator({"power": 1})
    entity = SolplanetEntity(_description(), "inv-1", coordinator)
    coordinator.data[INVERTER_IDENTIFIER]["inv-1"]["data"]["power"] = 2
    with patch.object(CoordinatorEntity, "_handle_coordinator_update") as parent:
        entity._handle_coordinator_update()
    assert entity._attr_native_value == 2
    parent.assert_called_once_with()
    assert entity.has_value_in_response()


def test_availability_requires_success_present_device_and_no_endpoint_failure() -> None:
    """Availability distinguishes missing values from failed or removed devices."""
    coordinator = _coordinator({"power": None})
    entity = SolplanetEntity(_description(), "inv-1", coordinator)
    assert entity.available

    coordinator.failed_device_ids.add("inv-1")
    assert not entity.available
    coordinator.failed_device_ids.clear()
    coordinator.last_update_success = False
    assert not entity.available
    coordinator.last_update_success = True
    coordinator.data[INVERTER_IDENTIFIER].clear()
    assert not entity.available


def test_device_info_identifiers_match_device_registry_convention() -> None:
    """Inverter and child-device identifiers use their established formats."""
    inverter = SolplanetEntity(_description(), "inv-1", _coordinator({"power": 1}))
    assert inverter.device_info == {"identifiers": {(DOMAIN, "inv-1")}}

    battery_coordinator = _coordinator({"power": 1})
    battery = SolplanetEntity(
        _description(data_field_device_type=BATTERY_IDENTIFIER),
        "bat-1",
        battery_coordinator,
    )
    assert battery.device_info == {"identifiers": {(DOMAIN, "battery_bat-1")}}


def test_extra_state_attributes_are_optional_and_failure_safe() -> None:
    """Attribute callbacks receive the endpoint payload without breaking entity state."""
    coordinator = _coordinator({"power": 7})
    plain = SolplanetEntity(_description(), "inv-1", coordinator)
    assert plain.extra_state_attributes is None

    described = SolplanetEntity(
        _description(attributes_fn=lambda data: {"double": data["power"] * 2}),
        "inv-1",
        coordinator,
    )
    assert described.extra_state_attributes == {"double": 14}

    broken = SolplanetEntity(_description(attributes_fn=lambda _data: 1 / 0), "inv-1", coordinator)
    assert broken.extra_state_attributes is None

    coordinator.data[INVERTER_IDENTIFIER].clear()
    assert described.extra_state_attributes is None
