"""Tests for Solplanet integration setup and device registration."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

import custom_components.solplanet as integration
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    CONF_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    MAX_INTERVAL,
    METER_IDENTIFIER,
    MIN_INTERVAL,
)
from custom_components.solplanet.coordinator import SolplanetRuntimeData


def _entry(*, interval: int = 60) -> SimpleNamespace:
    """Return the config-entry surface used by setup."""
    return SimpleNamespace(
        data={CONF_HOST: "inverter.local", CONF_INTERVAL: interval},
        entry_id="entry-id",
        runtime_data=None,
        async_on_unload=Mock(),
        version=1,
        minor_version=2,
    )


def _hass() -> SimpleNamespace:
    """Return the Home Assistant surface used by integration setup."""
    return SimpleNamespace(
        data={DOMAIN: {}},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(),
            async_update_entry=Mock(),
        ),
    )


def _registry(*devices: SimpleNamespace) -> SimpleNamespace:
    """Return the device-registry surface used by setup and cleanup."""
    return SimpleNamespace(
        async_get_or_create=Mock(),
        async_remove_device=Mock(),
        async_update_device=Mock(),
        devices=SimpleNamespace(
            get_devices_for_config_entry_id=Mock(return_value=list(devices))
        ),
    )


def _device(device_id: str, *identifiers: tuple[str, str]) -> SimpleNamespace:
    """Return a minimal device-registry entry."""
    return SimpleNamespace(id=device_id, identifiers=set(identifiers))


def test_register_devices_covers_all_inventory_types() -> None:
    """Metadata inventory is represented accurately in the device registry."""
    battery_hardware = SimpleNamespace(
        partno="BAT-SERIAL",
        softwarever="2.0",
        hardwarever="1.0",
    )
    battery_info = SimpleNamespace(
        isn="BAT-INV",
        muf=5,
        mod=1,
        battery=battery_hardware,
    )
    inverter_info = SimpleNamespace(
        isn="INV-1",
        model="ASW5000",
        msw="M1",
        ssw="S1",
        tsw="T1",
    )
    legacy_meter = SimpleNamespace(
        sn="LEGACY-METER",
        manufactory="Eastron",
        name="SDM630",
    )
    runtime = SimpleNamespace(
        data={
            DONGLE_IDENTIFIER: {
                "DG-1": {
                    "data": {
                        "nam": "Roof gateway",
                        "brd": "Solplanet",
                        "mod": "WiFi Stick",
                        "psn": "DG-SERIAL",
                        "ethmac": "AA-BB-CC-DD-EE-FF",
                        "wlanmac": "122233445566",
                        "hw": "H1",
                        "sw": "S1",
                    }
                },
                "DG-2": {"data": None},
            },
            INVERTER_IDENTIFIER: {"INV-1": {"info": inverter_info}},
            BATTERY_IDENTIFIER: {
                "BAT-1": {"info": battery_info},
                "missing": {"info": None},
            },
            METER_IDENTIFIER: {
                "APP-METER": {"app_info": {"sn": "APP-SERIAL", "equipModel": "1"}},
                "UNKNOWN-APP": {"app_info": {"equipModel": "not-a-number"}},
                "LEGACY-METER": {"info": legacy_meter},
                "missing": {},
            },
        }
    )
    entry = SimpleNamespace(
        data={CONF_MAC: "788899AABBCC"},
        entry_id="entry-id",
        runtime_data=runtime,
    )
    registry = _registry()

    integration._register_devices(registry, entry)

    assert registry.async_get_or_create.call_count == 7
    calls = [call.kwargs for call in registry.async_get_or_create.call_args_list]
    assert calls[0]["name"] == "Roof gateway"
    assert calls[0]["connections"] == {
        (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff"),
        (dr.CONNECTION_NETWORK_MAC, "12:22:33:44:55:66"),
        (dr.CONNECTION_NETWORK_MAC, "78:88:99:aa:bb:cc"),
    }
    assert calls[1]["name"] == "Solplanet Dongle"
    assert calls[2]["sw_version"] == "Master: M1, Slave: S1, Security: T1"
    assert calls[3]["name"] == "ASW2.5S-LB-G1"
    assert calls[3]["identifiers"] == {(DOMAIN, "battery_BAT-1")}
    assert calls[3]["manufacturer"] == "Solplanet"
    assert calls[3]["serial_number"] == "BAT-SERIAL"
    assert calls[4]["model"] == "EASTRON SDM630-Modbus V2"
    assert calls[5]["name"] == "Meter"
    assert calls[6]["name"] == "Energy meter"


def test_register_devices_labels_indexed_sub_meter() -> None:
    """Indexed telemetry creates distinct main and sub-meter registry devices."""
    meter_info = SimpleNamespace(sn=None, manufactory="Eastron", name="SDM630")
    runtime = SimpleNamespace(
        data={
            DONGLE_IDENTIFIER: {},
            INVERTER_IDENTIFIER: {},
            BATTERY_IDENTIFIER: {},
            METER_IDENTIFIER: {
                "INV": {"info": meter_info},
                "INV_sub1": {
                    "info": meter_info,
                    "model_name": "EASTRON SEM3-M-2L-CT2",
                    "is_submeter": True,
                    "submeter_index": 1,
                },
            },
        }
    )
    entry = SimpleNamespace(data={}, entry_id="entry-id", runtime_data=runtime)
    registry = _registry()

    integration._register_devices(registry, entry)

    calls = [call.kwargs for call in registry.async_get_or_create.call_args_list]
    assert [item["name"] for item in calls] == ["Energy meter", "EASTRON SEM3-M-2L-CT2"]
    assert calls[0]["serial_number"] is None
    assert calls[1]["serial_number"] == "INV_sub1"
    assert calls[1]["model"] == "EASTRON SEM3-M-2L-CT2"
    assert calls[1]["identifiers"] == {(DOMAIN, "meter_INV_sub1")}


def test_register_devices_detaches_only_authoritatively_stale_devices() -> None:
    """A complete inventory detaches stale devices without deleting shared entries."""
    inverter_info = SimpleNamespace(
        isn="INV-1",
        model="ASW5000",
        msw="M1",
        ssw="S1",
        tsw="T1",
    )
    runtime = SimpleNamespace(
        data={
            DONGLE_IDENTIFIER: {"DG-1": {"data": {}}},
            INVERTER_IDENTIFIER: {"INV-1": {"info": inverter_info}},
            BATTERY_IDENTIFIER: {},
            METER_IDENTIFIER: {
                "METER-1": {"app_info": {"sn": "METER-1", "equipModel": "1"}}
            },
        }
    )
    entry = SimpleNamespace(entry_id="entry-id", runtime_data=runtime)
    registry = _registry(
        _device("current-inverter", (DOMAIN, "INV-1")),
        _device(
            "current-multi-identifier",
            (DOMAIN, "INV-1"),
            (DOMAIN, "OLD-ALIAS"),
        ),
        _device("stale-inverter", (DOMAIN, "OLD-INV")),
        _device("stale-battery", (DOMAIN, "battery_OLD-BAT")),
        _device("current-dongle", (DOMAIN, "dongle_DG-1")),
        _device("stale-dongle", (DOMAIN, "dongle_OLD-DG")),
        _device("current-meter", (DOMAIN, "meter_METER-1")),
        _device("stale-meter", (DOMAIN, "meter_OLD-METER")),
        _device("other-integration", ("other", "device")),
    )

    integration._register_devices(registry, entry)

    assert registry.async_update_device.call_args_list == [
        call(device_id="stale-inverter", remove_config_entry_id="entry-id"),
        call(device_id="stale-battery", remove_config_entry_id="entry-id"),
        call(device_id="stale-dongle", remove_config_entry_id="entry-id"),
        call(device_id="stale-meter", remove_config_entry_id="entry-id"),
    ]
    registry.async_remove_device.assert_not_called()
    registry.devices.get_devices_for_config_entry_id.assert_called_once_with("entry-id")


def test_register_devices_preserves_uncertain_dongle_and_meter_entries() -> None:
    """Empty optional endpoint caches are not proof that their devices vanished."""
    inverter_info = SimpleNamespace(
        isn="INV-1",
        model="ASW5000",
        msw="M1",
        ssw="S1",
        tsw="T1",
    )
    entry = SimpleNamespace(
        data={CONF_MAC: "AABBCCDDEEFF"},
        entry_id="entry-id",
        runtime_data=SimpleNamespace(
            data={
                DONGLE_IDENTIFIER: {},
                INVERTER_IDENTIFIER: {"INV-1": {"info": inverter_info}},
                BATTERY_IDENTIFIER: {"BAT-PRESENT": {"info": None}},
                METER_IDENTIFIER: {},
            }
        ),
    )
    registry = _registry(
        _device("stale-dongle", (DOMAIN, "dongle_OLD-DG")),
        _device("stale-meter", (DOMAIN, "meter_OLD-METER")),
        _device("current-battery", (DOMAIN, "battery_BAT-PRESENT")),
        _device("stale-battery", (DOMAIN, "battery_OLD-BAT")),
    )

    integration._register_devices(registry, entry)

    assert registry.async_get_or_create.call_args.kwargs["connections"] == {
        (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")
    }
    registry.async_update_device.assert_called_once_with(
        device_id="stale-battery",
        remove_config_entry_id="entry-id",
    )


@pytest.mark.asyncio
async def test_manual_device_removal_requires_a_stale_solplanet_identifier() -> None:
    """The registry delete button is allowed only for absent Solplanet devices."""
    inverter_info = SimpleNamespace(isn="INV-1")
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            data={
                DONGLE_IDENTIFIER: {},
                INVERTER_IDENTIFIER: {"INV-1": {"info": inverter_info}},
                BATTERY_IDENTIFIER: {},
                METER_IDENTIFIER: {},
            }
        )
    )

    assert not await integration.async_remove_config_entry_device(
        SimpleNamespace(), entry, _device("current", (DOMAIN, "INV-1"))
    )
    assert await integration.async_remove_config_entry_device(
        SimpleNamespace(), entry, _device("stale", (DOMAIN, "OLD-INV"))
    )
    assert not await integration.async_remove_config_entry_device(
        SimpleNamespace(), entry, _device("unrelated", ("other", "device"))
    )


@pytest.mark.asyncio
async def test_async_setup_registers_services() -> None:
    """YAML setup initializes domain storage and registers services."""
    hass = SimpleNamespace(data={})
    with patch.object(integration, "async_setup_services", AsyncMock()) as setup_services:
        assert await integration.async_setup(hass, {}) is True

    assert hass.data[DOMAIN] == {}
    setup_services.assert_awaited_once_with(hass)


@pytest.mark.asyncio
async def test_setup_entry_imports_real_platforms_and_builds_runtime() -> None:
    """A full setup forwards every importable platform and wires all coordinators."""
    hass = _hass()
    entry = _entry(interval=1)
    api = SimpleNamespace(version="v2")
    client = Mock()
    registry = _registry()
    metadata = SimpleNamespace(
        async_config_entry_first_refresh=AsyncMock(),
        async_add_listener=Mock(return_value="remove-listener"),
        last_update_success=True,
    )
    inverter = SimpleNamespace(async_refresh=AsyncMock())
    battery = SimpleNamespace(async_refresh=AsyncMock())
    meter = SimpleNamespace(async_refresh=AsyncMock())
    dongle = SimpleNamespace(async_refresh=AsyncMock())

    async def forward_and_import(_entry: object, platforms: list[object]) -> None:
        for platform in platforms:
            importlib.import_module(f"custom_components.solplanet.{platform.value}")

    hass.config_entries.async_forward_entry_setups.side_effect = forward_and_import

    with (
        patch.object(integration, "async_get_clientsession", return_value="session") as get_session,
        patch.object(integration, "SolplanetClient", return_value=client) as client_factory,
        patch.object(integration.SolplanetApiAdapter, "create", AsyncMock(return_value=api)),
        patch.object(integration.dr, "async_get", return_value=registry),
        patch.object(
            integration,
            "SolplanetMetadataUpdateCoordinator",
            return_value=metadata,
        ) as metadata_factory,
        patch.object(
            integration,
            "SolplanetInverterUpdateCoordinator",
            return_value=inverter,
        ) as inverter_factory,
        patch.object(
            integration,
            "SolplanetBatteryUpdateCoordinator",
            return_value=battery,
        ),
        patch.object(
            integration,
            "SolplanetMeterUpdateCoordinator",
            return_value=meter,
        ),
        patch.object(
            integration,
            "SolplanetDongleUpdateCoordinator",
            return_value=dongle,
        ),
    ):
        assert await integration.async_setup_entry(hass, entry) is True

    get_session.assert_called_once_with(hass)
    client_factory.assert_called_once_with("inverter.local", "session")
    metadata_factory.assert_called_once()
    metadata.async_config_entry_first_refresh.assert_awaited_once()
    inverter.async_refresh.assert_awaited_once()
    battery.async_refresh.assert_awaited_once()
    meter.async_refresh.assert_awaited_once()
    dongle.async_refresh.assert_awaited_once()
    assert inverter_factory.call_args.args[3].total_seconds() == MIN_INTERVAL
    assert isinstance(entry.runtime_data, SolplanetRuntimeData)
    assert entry.runtime_data.metadata_coordinator is metadata
    assert hass.data[DOMAIN][entry.entry_id] is entry.runtime_data
    entry.async_on_unload.assert_called_once_with("remove-listener")
    listener = metadata.async_add_listener.call_args.args[0]
    assert registry.devices.get_devices_for_config_entry_id.call_count == 1
    metadata.last_update_success = False
    listener()
    assert registry.devices.get_devices_for_config_entry_id.call_count == 1
    metadata.last_update_success = True
    listener()
    assert registry.devices.get_devices_for_config_entry_id.call_count == 2
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry,
        integration.PLATFORMS,
    )


@pytest.mark.asyncio
async def test_setup_entry_clamps_large_poll_interval() -> None:
    """Setup clamps persisted out-of-range intervals before creating live coordinators."""
    hass = _hass()
    entry = _entry(interval=99999)
    api = SimpleNamespace(version="v2")
    registry = _registry()
    metadata = SimpleNamespace(
        async_config_entry_first_refresh=AsyncMock(),
        async_add_listener=Mock(return_value=None),
        last_update_success=True,
    )
    live = [SimpleNamespace(async_refresh=AsyncMock()) for _ in range(4)]

    with (
        patch.object(integration, "async_get_clientsession", return_value="session"),
        patch.object(integration, "SolplanetClient"),
        patch.object(integration.SolplanetApiAdapter, "create", AsyncMock(return_value=api)),
        patch.object(integration.dr, "async_get", return_value=registry),
        patch.object(integration, "SolplanetMetadataUpdateCoordinator", return_value=metadata),
        patch.object(
            integration,
            "SolplanetInverterUpdateCoordinator",
            return_value=live[0],
        ) as inverter_factory,
        patch.object(integration, "SolplanetBatteryUpdateCoordinator", return_value=live[1]),
        patch.object(integration, "SolplanetMeterUpdateCoordinator", return_value=live[2]),
        patch.object(integration, "SolplanetDongleUpdateCoordinator", return_value=live[3]),
    ):
        assert await integration.async_setup_entry(hass, entry) is True

    assert inverter_factory.call_args.args[3].total_seconds() == MAX_INTERVAL


@pytest.mark.asyncio
async def test_setup_entry_translates_detection_failure() -> None:
    """Protocol detection failures defer config-entry setup."""
    hass = _hass()
    entry = _entry()
    with (
        patch.object(integration, "async_get_clientsession", return_value="session"),
        patch.object(integration, "SolplanetClient"),
        patch.object(
            integration.SolplanetApiAdapter,
            "create",
            AsyncMock(side_effect=RuntimeError("not ready")),
        ),
    ):
        with pytest.raises(ConfigEntryNotReady) as exc_info:
            await integration.async_setup_entry(hass, entry)
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "protocol_detection_failed"
    assert exc_info.value.translation_placeholders == {"error": "not ready"}


@pytest.mark.asyncio
async def test_unload_entry_removes_runtime_only_after_success() -> None:
    """Runtime data remains available when a platform refuses to unload."""
    hass = _hass()
    entry = _entry()
    runtime = object()
    hass.data[DOMAIN][entry.entry_id] = runtime
    hass.config_entries.async_unload_platforms.side_effect = [False, True]

    assert await integration.async_unload_entry(hass, entry) is False
    assert hass.data[DOMAIN][entry.entry_id] is runtime
    assert await integration.async_unload_entry(hass, entry) is True
    assert entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_migrate_entry_versions() -> None:
    """Migration rejects future versions and upgrades pre-interval entries."""
    hass = _hass()
    future = SimpleNamespace(
        version=2,
        minor_version=0,
        data={},
        entry_id="future",
    )
    assert await integration.async_migrate_entry(hass, future) is False

    old = SimpleNamespace(
        version=1,
        minor_version=1,
        data={CONF_HOST: "inverter.local"},
        entry_id="old",
    )
    assert await integration.async_migrate_entry(hass, old) is True
    hass.config_entries.async_update_entry.assert_called_once_with(
        old,
        data={CONF_HOST: "inverter.local", CONF_INTERVAL: 60},
        version=1,
        minor_version=2,
    )

    hass.config_entries.async_update_entry.reset_mock()
    current = SimpleNamespace(
        version=1,
        minor_version=2,
        data={CONF_INTERVAL: 30},
        entry_id="current",
    )
    assert await integration.async_migrate_entry(hass, current) is True
    hass.config_entries.async_update_entry.assert_not_called()
