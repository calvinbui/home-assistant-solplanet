"""Tests for Solplanet polling coordinators."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from aiohttp import ClientResponseError
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solplanet.client import GetMeterDataResponse
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    CONF_INTERVAL,
    DEFAULT_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)
from custom_components.solplanet.coordinator import (
    SolplanetBatteryUpdateCoordinator,
    SolplanetDataUpdateCoordinator,
    SolplanetDongleUpdateCoordinator,
    SolplanetInverterUpdateCoordinator,
    SolplanetMetadataUpdateCoordinator,
    SolplanetMeterUpdateCoordinator,
    SolplanetRuntimeData,
    _is_enabled,
    _legacy_meter_payload_looks_valid,
)
from custom_components.solplanet.modbus import DataType


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        data={CONF_INTERVAL: DEFAULT_INTERVAL},
        domain="solplanet",
    )


def _api(version: str = "v2") -> SimpleNamespace:
    """Return an API double with the complete coordinator surface."""
    return SimpleNamespace(
        version=version,
        client=SimpleNamespace(get=AsyncMock(), post=AsyncMock()),
        get_inverter_info=AsyncMock(),
        get_inverter_data=AsyncMock(),
        get_battery_info=AsyncMock(),
        get_battery_data=AsyncMock(),
        get_meter_info=AsyncMock(),
        get_meter_data=AsyncMock(),
        get_schedule=AsyncMock(return_value={"raw": {}}),
        modbus_read_holding_registers=AsyncMock(return_value=[1]),
        modbus_write_single_holding_register=AsyncMock(),
        modbus_write_multiple_holding_registers=AsyncMock(),
        set_battery_work_mode=AsyncMock(),
        set_battery_soc_min=AsyncMock(),
        set_battery_soc_max=AsyncMock(),
        set_schedule_slots=AsyncMock(),
        set_schedule_power=AsyncMock(),
        set_schedule_pin=AsyncMock(),
        set_schedule_pout=AsyncMock(),
    )


def _base_coordinator(
    *, version: str = "v2"
) -> tuple[HomeAssistant, SimpleNamespace, SolplanetRuntimeData, SolplanetDataUpdateCoordinator]:
    hass = HomeAssistant("/tmp")
    api = _api(version)
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetDataUpdateCoordinator(
        hass,
        runtime,
        _entry(),
        "test",
        timedelta(seconds=10),
    )
    runtime.metadata_coordinator = SimpleNamespace(async_request_refresh=AsyncMock())
    return hass, api, runtime, coordinator


def _assert_translated_exception(
    error: HomeAssistantError,
    key: str,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Assert an exception references the Solplanet translation catalog."""
    assert error.translation_domain == DOMAIN
    assert error.translation_key == key
    assert error.translation_placeholders == placeholders


@pytest.mark.asyncio
async def test_inverter_failure_only_marks_failed_device() -> None:
    """A failed device does not make healthy peers unavailable."""
    hass = HomeAssistant("/tmp")
    api = SimpleNamespace(
        get_inverter_data=AsyncMock(
            side_effect=[{"pac": 100}, RuntimeError("offline")]
        )
    )
    runtime = SolplanetRuntimeData(api)
    runtime.data[INVERTER_IDENTIFIER] = {
        "inv-1": {"data": None},
        "inv-2": {"data": None},
    }
    coordinator = SolplanetInverterUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.failed_device_ids == {"inv-2"}
    assert runtime.data[INVERTER_IDENTIFIER]["inv-1"]["data"] == {"pac": 100}
    assert runtime.data[INVERTER_IDENTIFIER]["inv-2"]["data"] is None


@pytest.mark.asyncio
async def test_inverter_backoff_after_three_full_failures_and_recovers() -> None:
    """A repeatedly unavailable endpoint backs off, then restores its cadence."""
    hass = HomeAssistant("/tmp")
    api = SimpleNamespace(get_inverter_data=AsyncMock(side_effect=RuntimeError("offline")))
    runtime = SolplanetRuntimeData(api)
    runtime.data[INVERTER_IDENTIFIER] = {"inv-1": {"data": None}}
    coordinator = SolplanetInverterUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    for _ in range(3):
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert coordinator.update_interval == timedelta(minutes=10)

    api.get_inverter_data.side_effect = None
    api.get_inverter_data.return_value = {"pac": 123}
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.update_interval == timedelta(seconds=10)


@pytest.mark.asyncio
async def test_battery_zero_filled_payloads_retain_data_and_back_off() -> None:
    """Transient zero-filled battery payloads do not overwrite good data."""
    hass = HomeAssistant("/tmp")
    zero_payload = SimpleNamespace(
        cst=0,
        bst=0,
        vb=0,
        tb=0,
        soc=0,
        soh=0,
        cli=0,
        clo=0,
        pac=0,
    )
    api = SimpleNamespace(get_battery_data=AsyncMock(return_value=zero_payload))
    runtime = SolplanetRuntimeData(api)
    runtime.data[BATTERY_IDENTIFIER] = {"bat-1": {"data": {"soc": 67, "pac": 42}}}
    coordinator = SolplanetBatteryUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    for _ in range(3):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert runtime.data[BATTERY_IDENTIFIER]["bat-1"]["data"] == {"soc": 67, "pac": 42}
    assert coordinator.update_interval == timedelta(minutes=10)


@pytest.mark.asyncio
async def test_battery_energy_reset_is_accepted_after_two_confirmations() -> None:
    """Confirmed counter resets are delayed without discarding live telemetry."""
    hass = HomeAssistant("/tmp")
    previous = SimpleNamespace(etdpv=297, ebi=356, ebo=258, pb=6436)
    updates = [
        SimpleNamespace(etdpv=0, ebi=0, ebo=0, pb=5148),
        SimpleNamespace(etdpv=1, ebi=1, ebo=1, pb=1050),
        SimpleNamespace(etdpv=2, ebi=2, ebo=2, pb=500),
    ]
    api = SimpleNamespace(get_battery_data=AsyncMock(side_effect=updates))
    runtime = SolplanetRuntimeData(api)
    runtime.data[BATTERY_IDENTIFIER] = {"bat-1": {"data": previous}}
    coordinator = SolplanetBatteryUpdateCoordinator(
        hass, runtime, _entry(), timedelta(seconds=10)
    )

    await coordinator.async_refresh()
    first = runtime.data[BATTERY_IDENTIFIER]["bat-1"]["data"]
    assert (first.etdpv, first.ebi, first.ebo, first.pb) == (297, 356, 258, 5148)

    await coordinator.async_refresh()
    second = runtime.data[BATTERY_IDENTIFIER]["bat-1"]["data"]
    assert (second.etdpv, second.ebi, second.ebo, second.pb) == (297, 356, 258, 1050)

    await coordinator.async_refresh()
    third = runtime.data[BATTERY_IDENTIFIER]["bat-1"]["data"]
    assert (third.etdpv, third.ebi, third.ebo, third.pb) == (2, 2, 2, 500)
    assert coordinator._battery_energy_regression_candidates == {}


@pytest.mark.asyncio
async def test_battery_energy_transient_regression_recovers_without_reset() -> None:
    """A two-poll counter regression is discarded when the values recover."""
    hass = HomeAssistant("/tmp")
    previous = SimpleNamespace(etdpv=297, ebi=356, ebo=258)
    updates = [
        SimpleNamespace(etdpv=0, ebi=0, ebo=209),
        SimpleNamespace(etdpv=1, ebi=1, ebo=210),
        SimpleNamespace(etdpv=298, ebi=357, ebo=259),
    ]
    api = SimpleNamespace(get_battery_data=AsyncMock(side_effect=updates))
    runtime = SolplanetRuntimeData(api)
    runtime.data[BATTERY_IDENTIFIER] = {"bat-1": {"data": previous}}
    coordinator = SolplanetBatteryUpdateCoordinator(
        hass, runtime, _entry(), timedelta(seconds=10)
    )

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    data = runtime.data[BATTERY_IDENTIFIER]["bat-1"]["data"]
    assert (data.etdpv, data.ebi, data.ebo) == (298, 357, 259)
    assert coordinator._battery_energy_regression_candidates == {}


@pytest.mark.asyncio
async def test_coordinator_lock_serializes_endpoint_updates() -> None:
    """Endpoint coordinators share the runtime lock."""
    hass = HomeAssistant("/tmp")
    active = 0
    maximum = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def get_data(_device_id: str) -> dict[str, int]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        await release.wait()
        active -= 1
        return {"pac": 1}

    api = SimpleNamespace(get_inverter_data=AsyncMock(side_effect=get_data))
    runtime = SolplanetRuntimeData(api)
    runtime.data[INVERTER_IDENTIFIER] = {"inv-1": {"data": None}}
    first_coordinator = SolplanetInverterUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))
    second_coordinator = SolplanetInverterUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    first = asyncio.create_task(first_coordinator.async_refresh())
    await started.wait()
    second = asyncio.create_task(second_coordinator.async_refresh())
    await asyncio.sleep(0)
    assert maximum == 1
    release.set()
    await asyncio.gather(first, second)

    assert maximum == 1


@pytest.mark.asyncio
async def test_runtime_routes_coordinators_and_requests_metadata_refresh() -> None:
    """Runtime data routes entities to endpoint coordinators."""
    runtime = SolplanetRuntimeData(_api())
    with pytest.raises(RuntimeError, match="not initialized"):
        _ = runtime.coordinator

    metadata = SimpleNamespace(async_request_refresh=AsyncMock())
    runtime.metadata_coordinator = metadata
    runtime.inverter_coordinator = inverter = Mock()
    runtime.battery_coordinator = battery = Mock()
    runtime.meter_coordinator = meter = Mock()
    runtime.dongle_coordinator = dongle = Mock()

    assert runtime.coordinator_for(INVERTER_IDENTIFIER, "data") is inverter
    assert runtime.coordinator_for(BATTERY_IDENTIFIER, "data") is battery
    assert runtime.coordinator_for(METER_IDENTIFIER, "data") is meter
    assert runtime.coordinator_for(METER_IDENTIFIER, "app_data") is meter
    assert runtime.coordinator_for(DONGLE_IDENTIFIER, "warnings") is dongle
    assert runtime.coordinator_for(INVERTER_IDENTIFIER, "info") is metadata

    runtime.inverter_coordinator = None
    assert runtime.coordinator_for(INVERTER_IDENTIFIER, "data") is metadata
    await runtime.async_request_metadata_refresh()
    metadata.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_base_coordinator_helpers_and_update_contract() -> None:
    """The base coordinator exposes shared data, rates, and update failures."""
    _, api, runtime, coordinator = _base_coordinator()
    runtime.data[INVERTER_IDENTIFIER] = {
        "small": {"info": SimpleNamespace(rate=5000)},
        "large": {"info": SimpleNamespace(rate=12000)},
        "bad": {"info": SimpleNamespace(rate=-1)},
        "not-an-entry": None,
    }
    assert coordinator.api is api
    assert coordinator.get_max_inverter_rate_w() == 12000
    coordinator.data = None
    assert coordinator.get_max_inverter_rate_w() == 10000

    with pytest.raises(NotImplementedError):
        await coordinator._async_update_runtime_data()
    with pytest.raises(UpdateFailed) as exc_info:
        await coordinator._async_update_data()
    _assert_translated_exception(
        exc_info.value,
        "update_failed",
        {"source": "test", "error": ""},
    )


@pytest.mark.asyncio
async def test_base_coordinator_resets_backoff_after_success() -> None:
    """A successful update resets a previous failure interval."""
    _, _, runtime, coordinator = _base_coordinator()
    coordinator._failed_update_count = 2
    coordinator.update_interval = timedelta(minutes=10)
    coordinator._async_update_runtime_data = AsyncMock()

    assert await coordinator._async_update_data() is runtime.data
    assert coordinator._failed_update_count == 0
    assert coordinator.update_interval == timedelta(seconds=10)


@pytest.mark.asyncio
async def test_inverter_power_write_and_v1_error() -> None:
    """Inverter power writes the documented register and normalizes V1 errors."""
    _, api, runtime, coordinator = _base_coordinator()
    await coordinator.set_inverter_power(True)
    api.modbus_write_single_holding_register.assert_awaited_once_with(
        data_type=DataType.U16,
        device_address=3,
        register_address=40201,
        value=1,
        dry_run=False,
    )
    runtime.coordinator.async_request_refresh.assert_awaited_once()

    api.modbus_write_single_holding_register.side_effect = NotImplementedError
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_inverter_power(False)
    _assert_translated_exception(exc_info.value, "modbus_unsupported_v1")

    api.modbus_write_single_holding_register.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_inverter_power(False)
    _assert_translated_exception(
        exc_info.value,
        "inverter_power_failed",
        {"error": "offline"},
    )


@pytest.mark.asyncio
async def test_dongle_operations() -> None:
    """Dongle operations validate protocol, payloads, refreshes, and errors."""
    _, api, runtime, coordinator = _base_coordinator()
    fixed_now = datetime(2026, 7, 19, 12, 34, 56)
    with patch("custom_components.solplanet.coordinator.dt_util.now", return_value=fixed_now):
        await coordinator.dongle_sync_time()
    api.client.post.assert_awaited_with(
        "setting.cgi",
        {"device": 1, "action": "settime", "value": {"time": "20260719123456"}},
    )
    runtime.coordinator.async_request_refresh.assert_awaited_once()

    await coordinator.dongle_reboot()
    api.client.post.assert_awaited_with(
        "setting.cgi",
        {"device": 1, "action": "operation", "value": {"reboot": 1}},
    )

    api.client.post.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.dongle_sync_time()
    _assert_translated_exception(
        exc_info.value,
        "sync_time_failed",
        {"error": "offline"},
    )
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.dongle_reboot()
    _assert_translated_exception(
        exc_info.value,
        "reboot_dongle_failed",
        {"error": "offline"},
    )

    _, _, _, v1_coordinator = _base_coordinator(version="v1")
    with pytest.raises(HomeAssistantError) as exc_info:
        await v1_coordinator.dongle_sync_time()
    _assert_translated_exception(exc_info.value, "dongle_operation_unsupported_v1")
    with pytest.raises(HomeAssistantError) as exc_info:
        await v1_coordinator.dongle_reboot()
    _assert_translated_exception(exc_info.value, "dongle_operation_unsupported_v1")


@pytest.mark.asyncio
async def test_meter_power_limit_write() -> None:
    """Meter power-limit writes validate both protocol and response."""
    _, api, runtime, coordinator = _base_coordinator()
    api.client.post.return_value = {"cmd": "set_meter_rsp", "status": 200}
    await coordinator.set_meter_power_limit({"enable": 1})
    api.client.post.assert_awaited_once_with(
        "setting.cgi",
        {"cmd": "set_meter_req", "payload": {"enable": 1}},
    )
    await asyncio.sleep(0)
    runtime.coordinator.async_request_refresh.assert_awaited_once()

    api.client.post.return_value = {"status": 500}
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_meter_power_limit({})
    _assert_translated_exception(
        exc_info.value,
        "unexpected_meter_response",
        {"response": "{'status': 500}"},
    )
    api.client.post.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_meter_power_limit({})
    _assert_translated_exception(
        exc_info.value,
        "set_meter_power_limit_failed",
        {"error": "offline"},
    )

    api.client.post.side_effect = ClientResponseError(
        request_info=Mock(), history=(), status=404, message="Not Found"
    )
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_meter_power_limit({})
    _assert_translated_exception(exc_info.value, "meter_power_limit_unavailable")

    response_error = ClientResponseError(
        request_info=Mock(), history=(), status=500, message="Server Error"
    )
    api.client.post.side_effect = response_error
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_meter_power_limit({})
    _assert_translated_exception(
        exc_info.value,
        "set_meter_power_limit_failed",
        {"error": str(response_error)},
    )

    _, _, _, v1_coordinator = _base_coordinator(version="v1")
    with pytest.raises(HomeAssistantError) as exc_info:
        await v1_coordinator.set_meter_power_limit({})
    _assert_translated_exception(exc_info.value, "meter_power_limit_unsupported_v1")


@pytest.mark.asyncio
async def test_compatibility_meter_power_limit_write() -> None:
    """Compatibility writes use setmeter and validate its response."""
    _, api, runtime, coordinator = _base_coordinator()
    payload = {"regulate": 10, "target": 500}
    api.client.post.return_value = {"dat": "ok"}

    await coordinator.set_compatibility_meter_power_limit(payload)

    api.client.post.assert_awaited_once_with(
        "setting.cgi",
        {"device": 3, "action": "setmeter", "value": payload},
    )
    await asyncio.sleep(0)
    runtime.coordinator.async_request_refresh.assert_awaited_once()

    api.client.post.return_value = {"dat": "error"}
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_compatibility_meter_power_limit(payload)
    _assert_translated_exception(
        exc_info.value,
        "unexpected_meter_response",
        {"response": "{'dat': 'error'}"},
    )

    api.client.post.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_compatibility_meter_power_limit(payload)
    _assert_translated_exception(
        exc_info.value,
        "set_meter_power_limit_failed",
        {"error": "offline"},
    )

    _, _, _, v1_coordinator = _base_coordinator(version="v1")
    with pytest.raises(HomeAssistantError) as exc_info:
        await v1_coordinator.set_compatibility_meter_power_limit(payload)
    _assert_translated_exception(exc_info.value, "meter_power_limit_unsupported_v1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_offset", "argument", "expected_value"),
    [
        ("set_battery_power", 1500, True, 1),
        ("set_battery_sleep_enabled", 1501, True, 0),
        ("set_battery_led_color_index", 1502, 3, 3),
        ("set_battery_led_brightness", 1503, 75, 75),
    ],
)
async def test_battery_more_setting_writes(
    method_name: str,
    expected_offset: int,
    argument: object,
    expected_value: int,
) -> None:
    """Battery more-settings helpers map values to their Modbus registers."""
    _, api, runtime, coordinator = _base_coordinator()
    await getattr(coordinator, method_name)(argument)
    api.modbus_write_multiple_holding_registers.assert_awaited_once_with(
        device_address=3,
        register_address=40001 + expected_offset,
        values=[expected_value],
    )
    runtime.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_battery_more_setting_v1_error() -> None:
    """Unsupported battery Modbus writes surface a Home Assistant error."""
    _, api, _, coordinator = _base_coordinator(version="v1")
    api.modbus_write_multiple_holding_registers.side_effect = NotImplementedError
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_battery_power(False)
    _assert_translated_exception(exc_info.value, "modbus_unsupported_v1")

    api.modbus_write_multiple_holding_registers.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_battery_power(False)
    _assert_translated_exception(
        exc_info.value,
        "battery_operation_failed",
        {"error": "offline"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "api_method", "arguments", "expected_arguments"),
    [
        ("set_battery_work_mode", "set_battery_work_mode", ("bat", "mode"), ("bat", "mode")),
        ("set_battery_soc_min", "set_battery_soc_min", ("bat", 10), ("bat", 10)),
        ("set_battery_soc_max", "set_battery_soc_max", ("bat", 90), ("bat", 90)),
        ("set_battery_schedule_pin", "set_schedule_pin", ("bat", 2500), (2500,)),
        ("set_battery_schedule_pout", "set_schedule_pout", ("bat", 3000), (3000,)),
    ],
)
async def test_battery_api_writes_and_errors(
    method_name: str,
    api_method: str,
    arguments: tuple[object, ...],
    expected_arguments: tuple[object, ...],
) -> None:
    """Battery setters delegate, refresh metadata, and normalize V1 errors."""
    _, api, runtime, coordinator = _base_coordinator()
    await getattr(coordinator, method_name)(*arguments)
    getattr(api, api_method).assert_awaited_once_with(*expected_arguments)
    runtime.coordinator.async_request_refresh.assert_awaited_once()

    getattr(api, api_method).side_effect = NotImplementedError
    with pytest.raises(HomeAssistantError) as exc_info:
        await getattr(coordinator, method_name)(*arguments)
    _assert_translated_exception(exc_info.value, "battery_operation_unsupported_v1")

    getattr(api, api_method).side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await getattr(coordinator, method_name)(*arguments)
    _assert_translated_exception(
        exc_info.value,
        "battery_operation_failed",
        {"error": "offline"},
    )


@pytest.mark.asyncio
async def test_battery_schedule_writes() -> None:
    """Schedule slot and power writes preserve the current power values."""
    _, api, runtime, coordinator = _base_coordinator()
    api.get_schedule.return_value = {"raw": {"Pin": 1000, "Pout": 2000}}
    encoded = {"encoded": True}
    with patch(
        "custom_components.solplanet.coordinator.BatterySchedule.encode_schedule",
        return_value=encoded,
    ) as encode:
        await coordinator.set_battery_schedule_slots("bat", {"charge": []})
    encode.assert_called_once_with({"charge": []}, pin=1000, pout=2000)
    api.set_schedule_slots.assert_awaited_once_with(encoded)

    await coordinator.set_battery_schedule_power(1200, 2200)
    api.set_schedule_power.assert_awaited_once_with(1200, 2200)
    await asyncio.sleep(0)
    assert runtime.coordinator.async_request_refresh.await_count == 2

    api.get_schedule.side_effect = NotImplementedError
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_battery_schedule_slots("bat", {})
    _assert_translated_exception(exc_info.value, "battery_operation_unsupported_v1")

    api.get_schedule.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_battery_schedule_slots("bat", {})
    _assert_translated_exception(
        exc_info.value,
        "battery_operation_failed",
        {"error": "offline"},
    )

    api.set_schedule_power.side_effect = NotImplementedError
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_battery_schedule_power()
    _assert_translated_exception(exc_info.value, "battery_operation_unsupported_v1")

    api.set_schedule_power.side_effect = RuntimeError("offline")
    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.set_battery_schedule_power()
    _assert_translated_exception(
        exc_info.value,
        "battery_operation_failed",
        {"error": "offline"},
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, False),
        (SimpleNamespace(tim="2026-07-19 12:00:00", pac=0), True),
        (SimpleNamespace(tim="", pac=1), True),
        (SimpleNamespace(tim="", pac=0, itd=0, otd=0, iet=0, oet=0), False),
    ],
)
def test_legacy_meter_payload_validation(payload: object, expected: bool) -> None:
    assert _legacy_meter_payload_looks_valid(payload) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, True),
        ("1", True),
        ("0", False),
        (None, False),
        ("invalid", False),
    ],
)
def test_meter_enabled_flag_validation(value: object, expected: bool) -> None:
    """Meter enable flags accept integers and numeric strings."""
    assert _is_enabled(value) is expected


@pytest.mark.asyncio
async def test_metadata_refresh_dispatches_new_devices() -> None:
    """Metadata refresh primes live data and dispatches newly discovered IDs."""
    hass = HomeAssistant("/tmp")
    runtime = SolplanetRuntimeData(_api())
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    live = SimpleNamespace(async_refresh=AsyncMock())
    runtime.inverter_coordinator = live
    coordinator._new_device_ids = {
        INVERTER_IDENTIFIER: {"inv"},
        BATTERY_IDENTIFIER: {"bat"},
    }
    coordinator._async_update_runtime_data = AsyncMock()

    with patch("custom_components.solplanet.coordinator.async_dispatcher_send") as dispatch:
        assert await coordinator._async_update_data() is runtime.data

    live.async_refresh.assert_awaited_once()
    assert dispatch.call_count == 2
    assert coordinator._new_device_ids == {}


@pytest.mark.asyncio
async def test_metadata_refresh_primes_each_available_live_coordinator() -> None:
    """Every newly discovered device refreshes its corresponding coordinator."""
    hass = HomeAssistant("/tmp")
    runtime = SolplanetRuntimeData(_api())
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    live_coordinators = {
        INVERTER_IDENTIFIER: SimpleNamespace(async_refresh=AsyncMock()),
        BATTERY_IDENTIFIER: SimpleNamespace(async_refresh=AsyncMock()),
        METER_IDENTIFIER: SimpleNamespace(async_refresh=AsyncMock()),
        DONGLE_IDENTIFIER: SimpleNamespace(async_refresh=AsyncMock()),
    }
    runtime.inverter_coordinator = live_coordinators[INVERTER_IDENTIFIER]
    runtime.battery_coordinator = live_coordinators[BATTERY_IDENTIFIER]
    runtime.meter_coordinator = live_coordinators[METER_IDENTIFIER]
    runtime.dongle_coordinator = live_coordinators[DONGLE_IDENTIFIER]
    coordinator._new_device_ids = {
        device_type: {f"{device_type}-1"} for device_type in live_coordinators
    }
    coordinator._async_update_runtime_data = AsyncMock()

    with patch("custom_components.solplanet.coordinator.async_dispatcher_send") as dispatch:
        assert await coordinator._async_update_data() is runtime.data

    for live_coordinator in live_coordinators.values():
        live_coordinator.async_refresh.assert_awaited_once()
    assert dispatch.call_count == len(live_coordinators)


@pytest.mark.asyncio
async def test_metadata_inventory_orchestration_and_empty_inventory() -> None:
    """Metadata updates each endpoint family and records newly seen devices."""
    hass = HomeAssistant("/tmp")
    api = _api()
    inverter = SimpleNamespace(isn="inv", isStorage=lambda: False)
    api.get_inverter_info.return_value = SimpleNamespace(inv=[inverter])
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())

    async def update_inverters(_inventory: list[object]) -> None:
        runtime.data[INVERTER_IDENTIFIER]["inv"] = {}

    with (
        patch.object(coordinator, "_async_update_dongle_metadata", AsyncMock()) as dongle,
        patch.object(
            coordinator,
            "_async_update_inverter_metadata",
            AsyncMock(side_effect=update_inverters),
        ) as update_inv,
        patch.object(coordinator, "_async_update_battery_metadata", AsyncMock()) as update_bat,
        patch.object(coordinator, "_async_update_meter_metadata", AsyncMock()) as update_meter,
    ):
        await coordinator._async_update_runtime_data()

    dongle.assert_awaited_once()
    update_inv.assert_awaited_once_with([inverter])
    update_bat.assert_awaited_once_with([inverter])
    update_meter.assert_awaited_once_with([inverter])
    assert coordinator._new_device_ids == {INVERTER_IDENTIFIER: {"inv"}}

    api.get_inverter_info.return_value = SimpleNamespace(inv=[])
    with pytest.raises(RuntimeError, match="No inverters"):
        await coordinator._async_update_runtime_data()

    api.version = "v1"
    api.get_inverter_info.return_value = SimpleNamespace(inv=[inverter])
    with patch.object(coordinator, "_async_update_dongle_metadata", AsyncMock()) as dongle:
        await coordinator._async_update_runtime_data()
    dongle.assert_not_awaited()


@pytest.mark.asyncio
async def test_dongle_metadata_success_and_fallbacks() -> None:
    """Dongle metadata preserves diagnostics when network reads fail."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    runtime.data[DONGLE_IDENTIFIER] = {
        "dongle": {"network": {"old": True}, "warnings": {"warn": 1}}
    }
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())

    api.client.get.side_effect = [
        {"psn": "dongle", "nam": "Gateway"},
        RuntimeError("network offline"),
    ]
    await coordinator._async_update_dongle_metadata()
    assert runtime.data[DONGLE_IDENTIFIER]["dongle"] == {
        "data": {"psn": "dongle", "nam": "Gateway"},
        "network": {"old": True},
        "warnings": {"warn": 1},
    }

    api.client.get.reset_mock(side_effect=True)
    api.client.get.side_effect = RuntimeError("offline")
    before = runtime.data[DONGLE_IDENTIFIER]
    await coordinator._async_update_dongle_metadata()
    assert runtime.data[DONGLE_IDENTIFIER] is before

    api.client.get.side_effect = None
    api.client.get.return_value = ["unexpected"]
    await coordinator._async_update_dongle_metadata()
    assert runtime.data[DONGLE_IDENTIFIER] is before


@pytest.mark.asyncio
async def test_inverter_metadata_success_and_read_failure() -> None:
    """Inverter metadata retains telemetry and tolerates setting-read failures."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    runtime.data[INVERTER_IDENTIFIER] = {"inv": {"data": {"pac": 5}}}
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    info = SimpleNamespace(isn="inv")

    api.modbus_read_holding_registers.return_value = [1]
    await coordinator._async_update_inverter_metadata([SimpleNamespace(isn=None), info])
    assert runtime.data[INVERTER_IDENTIFIER]["inv"] == {
        "data": {"pac": 5},
        "info": info,
        "more_settings": {"power_on": True},
    }

    api.modbus_read_holding_registers.side_effect = RuntimeError("unsupported")
    await coordinator._async_update_inverter_metadata([info])
    assert runtime.data[INVERTER_IDENTIFIER]["inv"]["more_settings"] == {"power_on": True}

    api.modbus_read_holding_registers.side_effect = None
    api.modbus_read_holding_registers.return_value = 0
    await coordinator._async_update_inverter_metadata([info])
    assert runtime.data[INVERTER_IDENTIFIER]["inv"]["more_settings"] == {"power_on": False}

    api.modbus_read_holding_registers.return_value = []
    await coordinator._async_update_inverter_metadata([info])
    assert runtime.data[INVERTER_IDENTIFIER]["inv"]["more_settings"] == {"power_on": False}


@pytest.mark.asyncio
async def test_battery_metadata_protocol_inventory_and_success() -> None:
    """Battery metadata handles protocol limits and populates storage devices."""
    hass = HomeAssistant("/tmp")
    api = _api(version="v1")
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    storage = SimpleNamespace(isn="bat", isStorage=lambda: True)
    nonstorage = SimpleNamespace(isn="inv", isStorage=lambda: False)

    runtime.data[BATTERY_IDENTIFIER] = {"old": {}}
    await coordinator._async_update_battery_metadata([storage])
    assert runtime.data[BATTERY_IDENTIFIER] == {}

    api.version = "v2"
    await coordinator._async_update_battery_metadata([nonstorage])
    assert runtime.data[BATTERY_IDENTIFIER] == {}

    battery_info = SimpleNamespace(type=1, mod_r=2)
    api.get_schedule.return_value = {"raw": {"Pin": 1}}
    api.modbus_read_holding_registers.return_value = [1, 0, 4, 80]
    api.get_battery_info.return_value = battery_info
    modes = Mock()
    modes.get_all_modes.return_value = ["self-use"]
    modes.get_mode.return_value = "self-use"
    with patch("custom_components.solplanet.coordinator.BatteryWorkModes", return_value=modes):
        await coordinator._async_update_battery_metadata([storage])

    entry = runtime.data[BATTERY_IDENTIFIER]["bat"]
    assert entry["info"] is battery_info
    assert entry["work_modes"] == {"all": ["self-use"], "selected": "self-use"}
    assert entry["more_settings"] == {
        "power_on": True,
        "sleep_enabled": True,
        "led_color_index": 4,
        "led_brightness": 80,
    }

    api.modbus_read_holding_registers.return_value = [1]
    with patch("custom_components.solplanet.coordinator.BatteryWorkModes", return_value=modes):
        await coordinator._async_update_battery_metadata([storage])
    assert runtime.data[BATTERY_IDENTIFIER]["bat"]["more_settings"] == entry["more_settings"]


@pytest.mark.asyncio
async def test_battery_metadata_preserves_previous_values_on_failures() -> None:
    """Metadata endpoint failures do not erase known battery settings."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    previous = {
        "data": {"soc": 50},
        "info": None,
        "work_modes": {"all": [], "selected": None},
        "schedule": {"old": True},
        "more_settings": {"power_on": True},
    }
    runtime.data[BATTERY_IDENTIFIER] = {"bat": previous}
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    storage = SimpleNamespace(isn="bat", isStorage=lambda: True)
    api.get_schedule.side_effect = RuntimeError("offline")
    api.modbus_read_holding_registers.side_effect = RuntimeError("unsupported")
    api.get_battery_info.side_effect = RuntimeError("offline")

    await coordinator._async_update_battery_metadata([storage])
    assert runtime.data[BATTERY_IDENTIFIER]["bat"] == previous


@pytest.mark.asyncio
async def test_meter_metadata_prefers_app_inventory_and_falls_back() -> None:
    """V2 meter discovery prefers app data and otherwise uses legacy metadata."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    app_meters = {"meter": {"app_info": {}}}

    with (
        patch.object(
            coordinator,
            "_async_get_app_meter_inventory",
            AsyncMock(return_value=app_meters),
        ),
        patch.object(coordinator, "_async_update_legacy_meter_metadata", AsyncMock()) as legacy,
    ):
        await coordinator._async_update_meter_metadata([])
    assert runtime.data[METER_IDENTIFIER] == app_meters
    legacy.assert_not_awaited()

    with (
        patch.object(
            coordinator,
            "_async_get_app_meter_inventory",
            AsyncMock(return_value=None),
        ),
        patch.object(coordinator, "_async_update_legacy_meter_metadata", AsyncMock()) as legacy,
    ):
        await coordinator._async_update_meter_metadata([])
    legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_app_meter_inventory_success_and_failures() -> None:
    """App meter discovery validates responses and preserves existing data."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    previous = {"main": {"app_data": {"pac": 1}}}
    api.client.post.side_effect = [
        {
            "status": 200,
            "payload": {
                "mainMeter": [{"sn": "main", "equipModel": 1}],
                "subMeter": [{"address": 2}, "invalid"],
            },
        },
        {"status": 200, "payload": {"enabled": 1}},
    ]
    result = await coordinator._async_get_app_meter_inventory(previous)
    assert result == {
        "main": {
            "app_data": {"pac": 1},
            "app_info": {"sn": "main", "equipModel": 1},
            "meter_req": {"enabled": 1},
        },
        "addr_2": {"app_info": {"address": 2}},
    }

    api.client.post.reset_mock(side_effect=True)
    api.client.post.side_effect = RuntimeError("offline")
    assert await coordinator._async_get_app_meter_inventory({}) is None

    api.client.post.side_effect = None
    api.client.post.return_value = {"status": 500}
    assert await coordinator._async_get_app_meter_inventory({}) is None
    api.client.post.return_value = {"status": 200, "payload": {}}
    assert await coordinator._async_get_app_meter_inventory({}) is None


@pytest.mark.asyncio
async def test_app_meter_configuration_failure_is_nonfatal() -> None:
    """A meter can be discovered even when its settings endpoint fails."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    api.client.post.side_effect = [
        {
            "status": 200,
            "payload": {"mainMeter": [], "subMeter": [{"address": 7}]},
        },
        RuntimeError("unsupported"),
    ]
    result = await coordinator._async_get_app_meter_inventory({})
    assert result == {
        "addr_7": {"app_info": {"address": 7}, "app_data": None}
    }

    api.client.post.side_effect = [
        {
            "status": 200,
            "payload": {"mainMeter": [], "subMeter": [{"address": 7}]},
        },
        {"status": 500},
    ]
    result = await coordinator._async_get_app_meter_inventory({})
    assert result == {
        "addr_7": {"app_info": {"address": 7}, "app_data": None}
    }


@pytest.mark.asyncio
async def test_indexed_meter_discovery_uses_three_phase_get_payloads() -> None:
    """V2 fallback discovery creates main and sub-meter entries from indexed GETs."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    info = SimpleNamespace(
        sn=None,
        manufactory=None,
        name=None,
        mod=6,
        sec_enb=1,
        sec_mod=7,
    )
    api.get_meter_info.return_value = info
    api.client.post.side_effect = RuntimeError("404 from getting.cgi")
    main = GetMeterDataResponse(
        tim="2026-07-23 23:14:25",
        pac=0,
        itd=9,
        otd=9,
        iet=6,
        oet=136,
        mod=6,
        meter_general={"prc": -707, "sac": 804, "avg_v": 2402},
        prc_phs=[-443, -146, -120],
    )
    sub = GetMeterDataResponse(
        tim="2026-07-23 23:15:06",
        pac=0,
        itd=3864,
        otd=0,
        iet=448,
        oet=0,
        mod=7,
        meter_general={"prc": 156, "sac": 158, "avg_v": 2409},
        prc_phs=[103, 24, 28],
    )
    api.get_meter_data.side_effect = [main, sub]

    await coordinator._async_update_meter_metadata(
        [SimpleNamespace(isn="INVERTER-SERIAL")]
    )

    assert runtime.data[METER_IDENTIFIER] == {
        "INVERTER-SERIAL": {
            "info": info,
            "data": main,
            "model_name": "EASTRON SEM3-M-2L-CT1 (Grid)",
        },
        "INVERTER-SERIAL_sub1": {
            "info": info,
            "data": sub,
            "model_name": "EASTRON SEM3-M-2L-CT2",
            "submeter_index": 1,
            "is_submeter": True,
        },
    }
    api.client.post.assert_awaited_once_with(
        "getting.cgi",
        {"cmd": "get_app_dev_info_req", "payload": {"type": [4]}},
    )
    assert api.get_meter_data.await_args_list == [
        call(submeter=None),
        call(submeter=1),
    ]


@pytest.mark.asyncio
async def test_indexed_meter_discovery_ignores_failed_submeter_probe() -> None:
    """A failed secondary probe does not discard the available main meter."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    info = SimpleNamespace(sn="meter", mod=6, sec_enb="1", sec_mod=7)
    main = GetMeterDataResponse(tim="now", pac=1)
    api.get_meter_info.return_value = info
    api.get_meter_data.side_effect = [main, RuntimeError("offline")]

    await coordinator._async_update_legacy_meter_metadata([], {})

    assert runtime.data[METER_IDENTIFIER] == {
        "meter": {
            "info": info,
            "data": main,
            "model_name": "EASTRON SEM3-M-2L-CT1 (Grid)",
        }
    }
    assert api.get_meter_data.await_args_list == [
        call(submeter=None),
        call(submeter=1),
    ]


@pytest.mark.asyncio
async def test_legacy_meter_metadata_paths() -> None:
    """Legacy meter discovery handles new, existing, invalid, and missing meters."""
    hass = HomeAssistant("/tmp")
    api = _api(version="v1")
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    inverter = SimpleNamespace(isn="fallback")
    info = SimpleNamespace(sn="meter")
    data = SimpleNamespace(tim="now")
    api.get_meter_info.return_value = info
    api.get_meter_data.return_value = data

    await coordinator._async_update_legacy_meter_metadata([inverter], {})
    assert runtime.data[METER_IDENTIFIER] == {
        "meter": {"info": info, "data": data}
    }

    await coordinator._async_update_legacy_meter_metadata(
        [inverter], {"meter": {"data": data}}
    )
    assert api.get_meter_data.await_count == 1

    api.get_meter_info.side_effect = RuntimeError("offline")
    before = runtime.data[METER_IDENTIFIER]
    await coordinator._async_update_legacy_meter_metadata([inverter], {})
    assert runtime.data[METER_IDENTIFIER] is before

    api.get_meter_info.side_effect = None
    api.get_meter_info.return_value = SimpleNamespace(sn=None)
    await coordinator._async_update_legacy_meter_metadata([], {})
    assert runtime.data[METER_IDENTIFIER] is before

    api.get_meter_info.return_value = info
    api.get_meter_data.side_effect = RuntimeError("offline")
    await coordinator._async_update_legacy_meter_metadata([inverter], {})
    assert runtime.data[METER_IDENTIFIER] is before

    api.version = "v2"
    api.get_meter_data.side_effect = None
    api.get_meter_data.return_value = SimpleNamespace(tim="", pac=0, itd=0, otd=0, iet=0, oet=0)
    await coordinator._async_update_legacy_meter_metadata([inverter], {})
    assert runtime.data[METER_IDENTIFIER] is before


@pytest.mark.asyncio
async def test_legacy_meter_metadata_reuses_existing_entry_without_probe() -> None:
    """Existing legacy meters preserve telemetry until their live coordinator polls."""
    hass = HomeAssistant("/tmp")
    api = _api(version="v1")
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(hass, runtime, _entry())
    info = SimpleNamespace(sn="meter")
    existing_data = SimpleNamespace(tim="previous")
    api.get_meter_info.return_value = info

    await coordinator._async_update_legacy_meter_metadata(
        [], {"meter": {"data": existing_data}}
    )

    assert runtime.data[METER_IDENTIFIER] == {
        "meter": {"info": info, "data": existing_data}
    }
    api.get_meter_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_success_resets_zero_payload_backoff() -> None:
    """Valid battery telemetry restores the configured polling interval."""
    hass = HomeAssistant("/tmp")
    api = _api()
    api.get_battery_data.return_value = SimpleNamespace(soc=50, pac=100)
    runtime = SolplanetRuntimeData(api)
    runtime.data[BATTERY_IDENTIFIER] = {"bat": {"data": None}}
    coordinator = SolplanetBatteryUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))
    coordinator._zero_filled_update_count = 2
    coordinator.update_interval = timedelta(minutes=10)

    await coordinator._async_update_runtime_data()
    assert coordinator._zero_filled_update_count == 0
    assert coordinator.update_interval == timedelta(seconds=10)
    assert runtime.data[BATTERY_IDENTIFIER]["bat"]["data"].soc == 50

    await coordinator._async_update_runtime_data()
    assert coordinator._zero_filled_update_count == 0


@pytest.mark.asyncio
async def test_battery_all_failures_raise_and_empty_inventory_succeeds() -> None:
    """Battery transport failures fail the endpoint without penalizing no-battery sites."""
    hass = HomeAssistant("/tmp")
    api = _api()
    api.get_battery_data.side_effect = RuntimeError("offline")
    runtime = SolplanetRuntimeData(api)
    runtime.data[BATTERY_IDENTIFIER] = {"bat": {"data": None}}
    coordinator = SolplanetBatteryUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    with pytest.raises(RuntimeError, match="offline"):
        await coordinator._async_update_runtime_data()
    assert coordinator.failed_device_ids == {"bat"}

    runtime.data[BATTERY_IDENTIFIER] = {}
    await coordinator._async_update_runtime_data()


@pytest.mark.asyncio
async def test_meter_live_polling_paths() -> None:
    """Meter polling supports app and legacy protocols and validates payloads."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMeterUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    runtime.data[METER_IDENTIFIER] = {
        "main": {"app_info": {}, "app_data": None},
        "sub": {"app_info": {}},
    }
    api.client.post.return_value = {"status": 200, "payload": {"pac": 12}}
    await coordinator._async_update_runtime_data()
    assert runtime.data[METER_IDENTIFIER]["main"]["app_data"] == {"pac": 12}

    api.client.post.return_value = {"status": 500}
    with pytest.raises(RuntimeError, match="Unexpected get_meter_data"):
        await coordinator._async_update_runtime_data()

    runtime.data[METER_IDENTIFIER] = {
        "main": {"data": None},
        "sub": {"submeter_index": 1, "data": None},
    }
    main = GetMeterDataResponse(tim="now", meter_general={"prc": -707})
    sub = GetMeterDataResponse(tim="now", meter_general={"prc": 156})
    api.get_meter_data.side_effect = [main, sub]
    await coordinator._async_update_runtime_data()
    assert runtime.data[METER_IDENTIFIER]["main"]["data"] is main
    assert runtime.data[METER_IDENTIFIER]["sub"]["data"] is sub
    assert api.get_meter_data.await_args_list[-2:] == [
        call(submeter=None),
        call(submeter=1),
    ]
    assert coordinator.failed_device_ids == set()

    api.get_meter_data.side_effect = [RuntimeError("main offline"), sub]
    await coordinator._async_update_runtime_data()
    assert coordinator.failed_device_ids == {"main"}
    assert runtime.data[METER_IDENTIFIER]["sub"]["data"] is sub

    runtime.data[METER_IDENTIFIER] = {}
    api.get_meter_data.side_effect = None
    await coordinator._async_update_runtime_data()

    runtime.data[METER_IDENTIFIER] = {"legacy": {"data": None}}
    api.get_meter_data.return_value = SimpleNamespace(tim="now")
    await coordinator._async_update_runtime_data()
    assert runtime.data[METER_IDENTIFIER]["legacy"]["data"].tim == "now"

    api.get_meter_data.return_value = SimpleNamespace(tim="", pac=0, itd=0, otd=0, iet=0, oet=0)
    with pytest.raises(RuntimeError, match="zero-filled"):
        await coordinator._async_update_runtime_data()


@pytest.mark.asyncio
async def test_dongle_live_polling_paths() -> None:
    """Dongle warnings accept success and 404 while propagating other errors."""
    hass = HomeAssistant("/tmp")
    api = _api()
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetDongleUpdateCoordinator(hass, runtime, _entry(), timedelta(seconds=10))

    await coordinator._async_update_runtime_data()

    runtime.data[DONGLE_IDENTIFIER] = {"dongle": {"warnings": None}}
    api.client.get.return_value = {"warning": 1}
    await coordinator._async_update_runtime_data()
    assert runtime.data[DONGLE_IDENTIFIER]["dongle"]["warnings"] == {"warning": 1}

    api.client.get.side_effect = ClientResponseError(Mock(), (), status=404)
    await coordinator._async_update_runtime_data()
    assert runtime.data[DONGLE_IDENTIFIER]["dongle"]["warnings"] == {}

    api.client.get.side_effect = ClientResponseError(Mock(), (), status=500)
    with pytest.raises(ClientResponseError):
        await coordinator._async_update_runtime_data()
