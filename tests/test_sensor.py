"""Tests for the Solplanet sensor catalog and platform setup."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.solplanet import sensor
from custom_components.solplanet.client import GetInverterDataResponse, GetMeterDataResponse
from custom_components.solplanet.const import (
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)
from tests.helpers import FakeCoordinator, FakeEntry, integration_data


def test_sensor_value_mapper_helpers_cover_known_unknown_and_truncation() -> None:
    """Catalog mapper helpers preserve values and bound textual states."""
    response = GetInverterDataResponse(vpv=[200], ipv=[30])
    assert sensor._create_mppt_power_mapper(0)(response) == 6000
    assert sensor._create_mppt_power_mapper(0)(GetInverterDataResponse()) is None

    mapper = sensor._create_dict_mapper({1: "Ready"})
    assert mapper(1) == "Ready"
    assert mapper(7) == "Unknown (code: 7)"

    flags = sensor._create_dict_set_mapper(
        2,
        ["first", "second"],
        [{0: "First bit", 1: "Second bit"}, {0: "Third bit", 1: "Fourth bit"}],
        "No faults",
    )
    assert flags(SimpleNamespace(first=None, second=3)) == "No faults"
    assert flags(SimpleNamespace(first=2, second=3)) == "First bit"

    unknown = sensor._create_dict_set_mapper(2, ["field"], [{}], "None")
    assert unknown(SimpleNamespace(field=2)) == "Unknown (code: 0)"

    long = sensor._create_dict_set_mapper(
        4,
        ["field"],
        [{index: "x" * 100 for index in range(4)}],
        "None",
    )(SimpleNamespace(field=0))
    assert len(long) == 255
    assert long.endswith("...")


def test_inverter_catalog_handles_sleeping_and_live_dimensions() -> None:
    """Fixed inverter sensors exist while phase and MPPT sensors follow live dimensions."""
    coordinator = FakeCoordinator()
    live = sensor.create_inverter_entities_description(coordinator, "inv-1")
    assert len(live) == 11 + 6 + 6 + 6
    assert sum(item.key.startswith("inv-1_mppt_power") for item in live) == 2

    coordinator.data[INVERTER_IDENTIFIER]["inv-1"]["data"] = None
    sleeping = sensor.create_inverter_entities_description(coordinator, "inv-1")
    assert len(sleeping) == 17


def test_meter_catalog_covers_v2_submeter_v1_and_limit_modes() -> None:
    """Meter descriptions distinguish V2 main meters, sub-meters and V1 data."""
    coordinator = FakeCoordinator()
    main = sensor.create_meter_entities_description(coordinator, "meter-1")
    assert len(main) == 19
    limit = next(item for item in main if item.unique_id_suffix == "power_limit_control")
    mapper = limit.data_field_value_mapper
    assert mapper(None) is None
    assert mapper({}) == "Disabled"
    assert mapper({"regulate": "bad"}) == "Disabled"
    assert mapper({"regulate": "10", "ctrlType": 0}) == "Limit power"
    assert mapper({"regulate": 10, "ctrlType": "1"}) == "Limit current"
    assert mapper({"regulate": 10, "ctrlType": 2}) == "Zero power"
    assert mapper({"regulate": 10, "ctrlType": "bad"}) == "Enabled (unknown type)"

    modern_by_suffix = {item.unique_id_suffix: item for item in main}
    assert (
        sensor.SolplanetSensor(
            modern_by_suffix["export_power_limit_setpoint"], "meter-1", coordinator
        )._attr_native_value
        == 3000
    )
    assert (
        sensor.SolplanetSensor(
            modern_by_suffix["power_limit_phase_mode"], "meter-1", coordinator
        )._attr_native_value
        == "Phase-balanced"
    )
    assert (
        sensor.SolplanetSensor(
            modern_by_suffix["communication_loss_timeout"], "meter-1", coordinator
        )._attr_native_value
        == 60
    )

    coordinator.data[METER_IDENTIFIER]["sub-1"] = {"app_info": {"model": 1}}
    assert sensor.create_meter_entities_description(coordinator, "sub-1") == []

    coordinator.data[METER_IDENTIFIER]["v1"] = {
        "data": SimpleNamespace(pac=1, iet=2, oet=3, itd=4, otd=5),
        "info": SimpleNamespace(
            regulate=10,
            abs=0,
            exp_m=500,
            abs_offset=0,
        ),
    }
    legacy = sensor.create_meter_entities_description(coordinator, "v1")
    assert len(legacy) == 8
    legacy_by_suffix = {item.unique_id_suffix: item for item in legacy}
    assert (
        sensor.SolplanetSensor(legacy_by_suffix["power_limit_control"], "v1", coordinator)._attr_native_value
        == "Limit power"
    )
    assert (
        sensor.SolplanetSensor(
            legacy_by_suffix["power_limit_phase_mode"], "v1", coordinator
        )._attr_native_value
        == "Phase-balanced"
    )
    assert (
        sensor.SolplanetSensor(
            legacy_by_suffix["export_power_limit_setpoint_percentage"],
            "v1",
            coordinator,
        )._attr_native_value
        == 5
    )
    assert "export_power_limit_setpoint" not in legacy_by_suffix

    indexed = GetMeterDataResponse(
        pac=0,
        iet=448,
        meter_general={"prc": 156},
    )
    coordinator.data[METER_IDENTIFIER]["indexed"] = {
        "data": indexed,
        "info": {},
        "is_submeter": True,
        "submeter_index": 1,
    }
    indexed_descriptions = sensor.create_meter_entities_description(
        coordinator,
        "indexed",
    )
    power = next(item for item in indexed_descriptions if item.unique_id_suffix == "submeter_power")
    assert power.data_field_path == ["pac"]
    assert sensor.SolplanetSensor(power, "indexed", coordinator)._attr_native_value == 0
    imported = next(
        item for item in indexed_descriptions if item.unique_id_suffix == "submeter_energy_imported_total"
    )
    assert sensor.SolplanetSensor(imported, "indexed", coordinator)._attr_native_value == pytest.approx(44.8)


def test_dongle_and_battery_catalog_mappers() -> None:
    """Diagnostic text mappers handle empty, long and unusual gateway values."""
    coordinator = FakeCoordinator()
    dongle = sensor.create_dongle_entities_description(coordinator, "dongle-1")
    assert len(dongle) == 7
    mode_mapper = dongle[0].data_field_value_mapper
    assert mode_mapper(None) is None
    assert mode_mapper(123) == "123"
    assert len(mode_mapper("x" * 300)) == 255

    warning_mapper = dongle[-1].data_field_value_mapper
    assert warning_mapper(None) == "No warnings"
    assert warning_mapper({}) == "No warnings"
    assert warning_mapper({"code": 1}) == "{'code': 1}"

    battery = sensor.create_battery_entities_description(coordinator, "bat-1")
    assert len(battery) > 25
    status = next(item for item in battery if item.key.endswith("_cst"))
    assert status.data_field_value_mapper(999) == "Fault (code: 999)"
    errors = next(item for item in battery if item.unique_id_suffix == "eb1")
    assert (
        errors.data_field_value_mapper(SimpleNamespace(eb1=0xFFFF, eb2=0xFFFF, eb3=0xFFFF, eb4=0xFFFF))
        == "No errors"
    )


async def _setup_sensor_platform(coordinator, *, inverter_listener: bool = True):
    callbacks = []
    added = []

    def connect(_hass, _signal, callback):
        callbacks.append(callback)
        return lambda: None

    def add_entities(entities):
        added.extend(list(entities))

    entry = FakeEntry(coordinator)
    if not inverter_listener:
        entry.runtime_data.inverter_coordinator = None
    with patch.object(sensor, "async_dispatcher_connect", side_effect=connect):
        await sensor.async_setup_entry(SimpleNamespace(), entry, add_entities)
    return entry, callbacks[0], added


async def test_sensor_setup_imports_full_catalog_and_discovers_devices() -> None:
    """Actual platform setup creates every device catalog and deduplicates discovery."""
    coordinator = FakeCoordinator()
    entry, discover, added = await _setup_sensor_platform(coordinator, inverter_listener=False)
    initial = len(added)
    assert initial > 80
    assert entry.unloads

    discover("other-entry", INVERTER_IDENTIFIER, {"inv-2"})
    discover(entry.entry_id, "unsupported", {"inv-2"})
    discover(entry.entry_id, INVERTER_IDENTIFIER, {"inv-1"})
    assert len(added) == initial

    coordinator.data[METER_IDENTIFIER]["meter-2"] = deepcopy(coordinator.data[METER_IDENTIFIER]["meter-1"])
    discover(entry.entry_id, METER_IDENTIFIER, {"meter-2"})
    assert len(added) > initial

    # Repeating the same discovery signal cannot create duplicate unique IDs.
    after_discovery = len(added)
    discover(entry.entry_id, METER_IDENTIFIER, {"meter-2"})
    assert len(added) == after_discovery


async def test_sensor_metadata_listener_adds_late_inverter_dimensions_once() -> None:
    """Phase/MPPT entities appear when live data arrives after a sleeping startup."""
    data = integration_data()
    live = data[INVERTER_IDENTIFIER]["inv-1"]["data"]
    data[INVERTER_IDENTIFIER]["inv-1"]["data"] = None
    coordinator = FakeCoordinator(data)
    _entry, _discover, added = await _setup_sensor_platform(coordinator)
    initial = len(added)
    assert len(coordinator.listeners) == 2

    coordinator.data[INVERTER_IDENTIFIER]["inv-1"]["data"] = live
    coordinator.listeners[0]()
    assert len(added) == initial + 12
    coordinator.listeners[0]()
    assert len(added) == initial + 12
    coordinator.listeners[1]()
    assert len(added) == initial + 12


async def test_dedicated_inverter_listener_can_add_late_dimensions() -> None:
    """The live inverter coordinator listener also creates late dimension sensors."""
    data = integration_data()
    live = data[INVERTER_IDENTIFIER]["inv-1"]["data"]
    data[INVERTER_IDENTIFIER]["inv-1"]["data"] = None
    coordinator = FakeCoordinator(data)
    _entry, _discover, added = await _setup_sensor_platform(coordinator)
    initial = len(added)
    coordinator.data[INVERTER_IDENTIFIER]["inv-1"]["data"] = live
    coordinator.listeners[1]()
    assert len(added) == initial + 12
