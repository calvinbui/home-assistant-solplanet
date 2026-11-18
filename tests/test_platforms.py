"""Tests for Solplanet binary sensor, button, number, select and switch platforms."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.solplanet import binary_sensor, button, number, select, switch
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
)
from custom_components.solplanet.entity import SolplanetEntity

from tests.helpers import FakeCoordinator, FakeEntry, integration_data


async def _setup_platform(module, coordinator, *, version: str = "v2"):
    """Run a platform setup and expose its discovery callback and added entities."""
    callbacks = []
    added = []

    def connect(_hass, _signal, callback):
        callbacks.append(callback)
        return lambda: None

    def add_entities(entities):
        added.extend(list(entities))

    entry = FakeEntry(coordinator, version=version)
    with patch.object(module, "async_dispatcher_connect", side_effect=connect):
        await module.async_setup_entry(SimpleNamespace(), entry, add_entities)
    return entry, callbacks[0], added


def test_binary_sensor_descriptions_mappers_and_attributes() -> None:
    """Binary descriptions expose schedule/grid state without collapsing unknown data."""
    coordinator = FakeCoordinator()
    schedule = binary_sensor.create_battery_binary_sensors(coordinator, "bat-1")[0]
    assert schedule.data_field_value_mapper("invalid") is None
    assert schedule.data_field_value_mapper({}) is False
    assert schedule.data_field_value_mapper({"Mon": [0, 1]}) is True
    attrs = schedule.attributes_fn(
        coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["schedule"]
    )
    assert attrs["raw_schedule"] == {"Mon": [1]}
    assert attrs["formatted_schedule"]["Mon"] == ["01:00 - 02:00 (charge)"]
    assert attrs["pin"] == 1000
    assert attrs["pout"] == 900

    grid = binary_sensor.create_inverter_binary_sensors(coordinator, "inv-1")[0]
    assert grid.data_field_value_mapper(0) is False
    assert grid.data_field_value_mapper(1) is True
    assert grid.data_field_value_mapper(None) is None
    entity = binary_sensor.SolplanetBinarySensor(grid, "inv-1", coordinator)
    assert entity.is_on is True


@pytest.mark.parametrize("version, expected", [("v2", 2), ("v1", 1)])
async def test_binary_sensor_setup_and_discovery(version: str, expected: int) -> None:
    """Platform setup imports both catalogs and adds genuinely new devices once."""
    coordinator = FakeCoordinator()
    entry, discover, added = await _setup_platform(
        binary_sensor, coordinator, version=version
    )
    assert len(added) == expected
    assert entry.unloads

    before = len(added)
    discover("other-entry", BATTERY_IDENTIFIER, {"bat-2"})
    discover(entry.entry_id, "unsupported", {"bat-2"})
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-1"})
    assert len(added) == before

    coordinator.data[BATTERY_IDENTIFIER]["bat-2"] = deepcopy(
        coordinator.data[BATTERY_IDENTIFIER]["bat-1"]
    )
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-1", "bat-2"})
    assert len(added) == before + 1


async def test_button_descriptions_actions_setup_and_discovery() -> None:
    """Dongle buttons call coordinator actions and follow discovery events."""
    coordinator = FakeCoordinator()
    descriptions = button.create_dongle_entities_description(coordinator, "dongle-1")
    assert {description.unique_id_suffix for description in descriptions} == {
        "sync_time",
        "reboot",
    }
    await descriptions[0].callback()
    coordinator.dongle_sync_time.assert_awaited_once_with()

    entity = button.SolplanetButton(descriptions[1], "dongle-1", coordinator)
    assert entity._set_native_value() is None
    await entity.async_press()
    coordinator.dongle_reboot.assert_awaited_once_with()

    entry, discover, added = await _setup_platform(button, coordinator)
    assert len(added) == 2
    discover("other-entry", DONGLE_IDENTIFIER, {"dongle-2"})
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"dongle-2"})
    discover(entry.entry_id, DONGLE_IDENTIFIER, {"dongle-1"})
    assert len(added) == 2
    coordinator.data[DONGLE_IDENTIFIER]["dongle-2"] = deepcopy(
        coordinator.data[DONGLE_IDENTIFIER]["dongle-1"]
    )
    discover(entry.entry_id, DONGLE_IDENTIFIER, {"dongle-2"})
    assert len(added) == 4


async def test_number_catalog_values_callbacks_and_capabilities() -> None:
    """Number controls honor model capabilities and dynamic inverter power limits."""
    coordinator = FakeCoordinator()
    descriptions = number.create_battery_entities_description(coordinator, "bat-1")
    assert len(descriptions) == 5
    schedule_power = next(
        item for item in descriptions if item.key.endswith("schedule_pin")
    )
    soc = next(item for item in descriptions if item.key.endswith("soc_max"))
    schedule_entity = number.SolplanetNumber(schedule_power, "bat-1", coordinator)
    soc_entity = number.SolplanetNumber(soc, "bat-1", coordinator)
    assert schedule_entity.native_max_value == 5000
    assert schedule_entity.native_step == 1
    assert soc_entity.native_max_value == 100
    await soc_entity.async_set_native_value(82.9)
    coordinator.set_battery_soc_max.assert_awaited_once_with("bat-1", 82)

    without_led = FakeCoordinator(integration_data(led_battery=False))
    assert len(number.create_battery_entities_description(without_led, "bat-1")) == 4


async def test_number_setup_discovery_deduplication_and_late_metadata() -> None:
    """Number setup adds new devices and controls revealed by later metadata once."""
    coordinator = FakeCoordinator(integration_data(led_battery=False))
    entry, discover, added = await _setup_platform(number, coordinator)
    assert len(added) == 4
    assert len(coordinator.listeners) == 1

    discover("other", BATTERY_IDENTIFIER, {"bat-2"})
    discover(entry.entry_id, INVERTER_IDENTIFIER, {"bat-2"})
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-1"})
    assert len(added) == 4

    coordinator.data[BATTERY_IDENTIFIER]["bat-2"] = deepcopy(
        coordinator.data[BATTERY_IDENTIFIER]["bat-1"]
    )
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-2"})
    assert len(added) == 8

    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["info"].muf = 5
    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["info"].mod = 12
    coordinator.listeners[0]()
    assert len(added) == 9
    coordinator.listeners[0]()
    assert len(added) == 9


async def test_select_options_mapping_selection_and_refresh() -> None:
    """Select controls preserve labels while sending the vendor value."""
    coordinator = FakeCoordinator()
    descriptions = select.create_battery_entities_description(coordinator, "bat-1")
    assert len(descriptions) == 2
    work_mode, led = descriptions
    assert [option.label for option in work_mode.get_options()] == [
        "Self-consumption",
        "Backup",
    ]
    assert led.data_field_value_mapper(2) == "Mint"
    assert led.data_field_value_mapper(99) == "Index 99"
    assert led.data_field_value_mapper(None) is None
    assert led.attributes_fn({"led_color_index": 2}) == {"index": 2, "hex": "#69F9CB"}
    assert led.attributes_fn(None) == {"index": None, "hex": None}

    entity = select.SolplanetSelect(work_mode, "bat-1", coordinator)
    assert entity.options == ["Self-consumption", "Backup"]
    assert entity.current_option == "Self-consumption"
    await entity.async_select_option("Backup")
    coordinator.set_battery_work_mode.assert_awaited_once()
    await entity.async_select_option("missing")
    coordinator.set_battery_work_mode.assert_awaited_once()

    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["work_modes"]["all"].append(
        SimpleNamespace(name="Peak shaving")
    )
    with patch.object(SolplanetEntity, "_handle_coordinator_update") as parent:
        entity._handle_coordinator_update()
    parent.assert_called_once_with()
    assert entity.options[-1] == "Peak shaving"

    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["more_settings"][
        "led_color_index"
    ] = 9
    led_options = led.get_options()
    assert led_options[-1].label == "Index 9"
    assert led_options[-1].value == 9

    # A malformed/non-numeric current value must not pollute the fixed palette.
    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["more_settings"][
        "led_color_index"
    ] = "invalid"
    assert [option.value for option in led.get_options()] == [1, 2, 3, 4, 5]

    without_led = FakeCoordinator(integration_data(led_battery=False))
    assert len(select.create_battery_entities_description(without_led, "bat-1")) == 1


async def test_select_setup_discovery_deduplication_and_late_metadata() -> None:
    """Select setup reacts to device discovery and late LED capabilities."""
    coordinator = FakeCoordinator(integration_data(led_battery=False))
    entry, discover, added = await _setup_platform(select, coordinator)
    assert len(added) == 1
    discover("other", BATTERY_IDENTIFIER, {"bat-2"})
    discover(entry.entry_id, INVERTER_IDENTIFIER, {"bat-2"})
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-1"})
    assert len(added) == 1

    coordinator.data[BATTERY_IDENTIFIER]["bat-2"] = deepcopy(
        coordinator.data[BATTERY_IDENTIFIER]["bat-1"]
    )
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-2"})
    assert len(added) == 2

    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["info"].muf = 5
    coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["info"].mod = 12
    coordinator.listeners[0]()
    assert len(added) == 3
    coordinator.listeners[0]()
    assert len(added) == 3


async def test_switch_catalog_state_actions_setup_and_discovery() -> None:
    """Switches read boolean state, dispatch setters and add new devices once."""
    coordinator = FakeCoordinator()
    inverter_description = switch.create_inverter_switches("inv-1")[0]
    battery_descriptions = switch.create_battery_switches("bat-1")
    assert len(battery_descriptions) == 2

    inverter = switch.SolplanetSwitch(inverter_description, "inv-1", coordinator)
    assert inverter.is_on is True
    await inverter.async_turn_off(unused=True)
    await inverter.async_turn_on()
    assert coordinator.set_inverter_power.await_args_list[0].args == (False,)
    assert coordinator.set_inverter_power.await_args_list[1].args == (True,)

    with patch.object(inverter, "_get_value_from_coordinator", side_effect=ValueError):
        assert inverter.is_on is None

    entry, discover, added = await _setup_platform(switch, coordinator)
    assert len(added) == 3
    discover("other", BATTERY_IDENTIFIER, {"bat-2"})
    discover(entry.entry_id, "unsupported", {"bat-2"})
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-1"})
    assert len(added) == 3

    coordinator.data[BATTERY_IDENTIFIER]["bat-2"] = deepcopy(
        coordinator.data[BATTERY_IDENTIFIER]["bat-1"]
    )
    discover(entry.entry_id, BATTERY_IDENTIFIER, {"bat-2"})
    assert len(added) == 5
