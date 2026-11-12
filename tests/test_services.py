"""Tests for Solplanet service target resolution and handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.solplanet import services
from custom_components.solplanet.client import BatterySchedule, ScheduleSlot
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    DOMAIN,
    METER_IDENTIFIER,
)

from tests.helpers import FakeCoordinator


class FakeServiceRegistry:
    """Capture Home Assistant service registrations."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, vol.Schema] = {}

    def async_register(self, domain, name, handler, *, schema) -> None:
        assert domain == DOMAIN
        self.handlers[name] = handler
        self.schemas[name] = schema


def _call(**data):
    return SimpleNamespace(data=data)


def _device(*identifiers):
    return SimpleNamespace(identifiers=identifiers)


def _assert_translated_exception(
    error: HomeAssistantError,
    key: str,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Assert an exception references the Solplanet translation catalog."""
    assert error.translation_domain == DOMAIN
    assert error.translation_key == key
    assert error.translation_placeholders == placeholders


def test_build_target_extracts_only_supported_target_fields() -> None:
    """Merged service target fields are copied from call data."""
    assert services._build_target(_call(value=1)) == {}
    assert services._build_target(_call(entity_id="sensor.one", value=1)) == {
        "entity_id": "sensor.one"
    }
    assert services._build_target(
        _call(device_id=["one"], entity_id=["sensor.one"])
    ) == {
        "entity_id": ["sensor.one"],
        "device_id": ["one"],
    }


async def test_battery_target_resolution_handles_entity_and_device_lists() -> None:
    """Battery targets resolve through entity registry devices and ignore unrelated IDs."""
    devices = {
        "battery": _device((DOMAIN, "battery_BAT-1")),
        "battery2": _device(("other", "battery_WRONG"), (DOMAIN, "battery_BAT-2")),
        "meter": _device((DOMAIN, "meter_METER-1")),
    }
    device_registry = SimpleNamespace(
        async_get=lambda device_id: devices.get(device_id)
    )
    entities = {
        "sensor.battery": SimpleNamespace(device_id="battery"),
        "sensor.meter": SimpleNamespace(device_id="meter"),
        "sensor.no_device": SimpleNamespace(device_id=None),
        "sensor.missing_device": SimpleNamespace(device_id="missing"),
    }
    entity_registry = SimpleNamespace(
        async_get=lambda entity_id: entities.get(entity_id)
    )
    with (
        patch.object(services.dr, "async_get", return_value=device_registry),
        patch.object(services.er, "async_get", return_value=entity_registry),
    ):
        result = await services.get_isn_from_target(
            SimpleNamespace(),
            {
                "entity_id": [
                    "sensor.battery",
                    "sensor.meter",
                    "sensor.no_device",
                    "sensor.missing_device",
                    "sensor.unknown",
                ],
                "device_id": ["battery2", "meter", "missing"],
            },
        )
        single = await services.get_isn_from_target(
            SimpleNamespace(), {"entity_id": "sensor.battery", "device_id": "battery"}
        )
        entity_only = await services.get_isn_from_target(
            SimpleNamespace(), {"entity_id": "sensor.battery"}
        )
        device_only = await services.get_isn_from_target(
            SimpleNamespace(), {"device_id": "battery2"}
        )
    assert set(result) == {"BAT-1", "BAT-2"}
    assert single == ["BAT-1"]
    assert entity_only == ["BAT-1"]
    assert device_only == ["BAT-2"]


async def test_meter_target_resolution_handles_entity_and_device_lists() -> None:
    """Meter targets use the same registry path and require meter identifiers."""
    devices = {
        "meter": _device(("other", "meter_WRONG"), (DOMAIN, "meter_METER-1")),
        "meter2": _device((DOMAIN, "meter_METER-2")),
        "battery": _device((DOMAIN, "battery_BAT-1")),
    }
    device_registry = SimpleNamespace(
        async_get=lambda device_id: devices.get(device_id)
    )
    entities = {
        "sensor.meter": SimpleNamespace(device_id="meter"),
        "sensor.battery": SimpleNamespace(device_id="battery"),
        "sensor.no_device": SimpleNamespace(device_id=None),
        "sensor.missing": SimpleNamespace(device_id="missing"),
    }
    entity_registry = SimpleNamespace(
        async_get=lambda entity_id: entities.get(entity_id)
    )
    with (
        patch.object(services.dr, "async_get", return_value=device_registry),
        patch.object(services.er, "async_get", return_value=entity_registry),
    ):
        result = await services.get_meter_isn_from_target(
            SimpleNamespace(),
            {
                "entity_id": [
                    "sensor.meter",
                    "sensor.battery",
                    "sensor.no_device",
                    "sensor.missing",
                    "unknown",
                ],
                "device_id": ["meter2", "battery", "missing"],
            },
        )
        single = await services.get_meter_isn_from_target(
            SimpleNamespace(), {"entity_id": "sensor.meter", "device_id": "meter"}
        )
        entity_only = await services.get_meter_isn_from_target(
            SimpleNamespace(), {"entity_id": "sensor.meter"}
        )
        device_only = await services.get_meter_isn_from_target(
            SimpleNamespace(), {"device_id": "meter2"}
        )
    assert set(result) == {"METER-1", "METER-2"}
    assert single == ["METER-1"]
    assert entity_only == ["METER-1"]
    assert device_only == ["METER-2"]


async def _setup_services(coordinator: FakeCoordinator | object | None = None):
    registry = FakeServiceRegistry()
    coordinator = coordinator or FakeCoordinator()
    hass = SimpleNamespace(
        services=registry,
        data={DOMAIN: {"entry": SimpleNamespace(coordinator=coordinator)}},
    )
    await services.async_setup_services(hass)
    assert set(registry.handlers) == {
        "set_schedule_slots",
        "replace_schedule_day",
        "clear_schedule",
        "set_meter_limit_power",
        "set_meter_limit_current",
        "set_meter_zero_power",
        "disable_meter_power_limit",
        "set_compatibility_meter_power_limit",
    }
    return hass, registry, coordinator


def _schedule_call(**overrides):
    data = {
        "device_id": "battery-device",
        "day": "Mon",
        "start_hour": 3,
        "start_minute": 0,
        "duration": 1,
        "mode": "charge",
    }
    data.update(overrides)
    return _call(**data)


async def test_replace_schedule_day_validates_and_forwards_slots_and_power() -> None:
    """Replacement validates the complete day and forwards optional power values."""
    _hass, registry, coordinator = await _setup_services()
    handler = registry.handlers["replace_schedule_day"]
    call = _call(
        device_id="battery-device",
        day="Mon",
        slots=[
            {
                "start_hour": 3,
                "start_minute": 0,
                "duration": 1,
                "mode": "charge",
            },
            {
                "start_hour": 8,
                "start_minute": 30,
                "duration": 2,
                "mode": "discharge",
            },
        ],
        pin=2500,
        pout=3000,
    )

    with patch.object(services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])):
        await handler(call)

    coordinator.replace_battery_schedule_day.assert_awaited_once_with(
        "bat-1",
        "Mon",
        [ScheduleSlot(3, 0, 1, "charge"), ScheduleSlot(8, 30, 2, "discharge")],
        pin=2500,
        pout=3000,
    )


async def test_replace_schedule_day_rejects_overlaps_without_writing() -> None:
    """Overlapping or invalid replacement slots fail before a device write."""
    _hass, registry, coordinator = await _setup_services()
    handler = registry.handlers["replace_schedule_day"]
    call = _call(
        device_id="battery-device",
        day="Mon",
        slots=[
            {
                "start_hour": 8,
                "start_minute": 0,
                "duration": 2,
                "mode": "charge",
            },
            {
                "start_hour": 9,
                "start_minute": 30,
                "duration": 1,
                "mode": "discharge",
            },
        ],
    )

    with (
        patch.object(services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await handler(call)

    _assert_translated_exception(
        exc_info.value,
        "invalid_schedule",
        {"error": ("Slot 08:00 - 10:00 (charge) overlaps with 09:30 - 10:30 (discharge)")},
    )
    coordinator.replace_battery_schedule_day.assert_not_awaited()


async def test_replace_schedule_day_schema_rejects_unsafe_values() -> None:
    """The service schema bounds slot and power values and rejects fractional inputs."""
    _hass, registry, _coordinator = await _setup_services()
    schema = registry.schemas["replace_schedule_day"]
    base = {
        "day": "Mon",
        "slots": [
            {
                "start_hour": 8,
                "start_minute": 0,
                "duration": 1,
                "mode": "charge",
            }
        ],
    }
    with pytest.raises(vol.Invalid):
        schema({**base, "pin": 150})
    with pytest.raises(vol.Invalid):
        schema({**base, "slots": [{**base["slots"][0], "start_hour": 8.5}]})
    with pytest.raises(vol.Invalid):
        schema({**base, "slots": [{**base["slots"][0], "start_minute": 15}]})
    with pytest.raises(vol.Invalid):
        schema({**base, "slots": [{**base["slots"][0], "start_minute": 0.5}]})
    with pytest.raises(vol.Invalid):
        schema({**base, "slots": base["slots"] * 7})


async def test_set_schedule_slots_updates_copy_and_validates_failures() -> None:
    """Schedule slots are appended, validated, and failed validation leaves cached data intact."""
    _hass, registry, coordinator = await _setup_services()
    handler = registry.handlers["set_schedule_slots"]

    with patch.object(services, "get_isn_from_target", AsyncMock(return_value=[])):
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_schedule_call())
    _assert_translated_exception(exc_info.value, "no_valid_battery_targets")

    with patch.object(
        services, "get_isn_from_target", AsyncMock(return_value=["missing"])
    ):
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_schedule_call())
    _assert_translated_exception(
        exc_info.value,
        "battery_coordinator_not_found",
        {"identifiers": "missing"},
    )

    with patch.object(
        services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])
    ):
        await handler(_schedule_call())
    new_slots = coordinator.set_battery_schedule_slots.await_args.args[1]
    assert len(new_slots["Mon"]) == 2

    original_slots = coordinator.data[BATTERY_IDENTIFIER]["bat-1"]["schedule"]["slots"]
    original_count = len(original_slots["Mon"])
    with (
        patch.object(
            services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await handler(_schedule_call(start_hour=1))
    assert exc_info.value.translation_placeholders is not None
    assert "overlaps" in exc_info.value.translation_placeholders["error"]
    _assert_translated_exception(
        exc_info.value,
        "invalid_schedule",
        exc_info.value.translation_placeholders,
    )
    assert len(original_slots["Mon"]) == original_count

    original_slots["Mon"] = [ScheduleSlot(index, 0, 1, "charge") for index in range(6)]
    with (
        patch.object(
            services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await handler(_schedule_call(start_hour=12))
    _assert_translated_exception(exc_info.value, "schedule_slot_limit")

    coordinator.set_battery_schedule_slots.side_effect = ConnectionError("offline")
    original_slots["Mon"] = []
    with (
        patch.object(
            services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])
        ),
        pytest.raises(HomeAssistantError) as exc_info,
    ):
        await handler(_schedule_call())
    assert not isinstance(exc_info.value, ServiceValidationError)
    _assert_translated_exception(
        exc_info.value,
        "communication_error",
        {"error": "offline"},
    )


async def test_clear_schedule_handles_day_all_missing_and_unmatched_targets() -> None:
    """Schedule clearing can reset one day or all days and reports invalid targets."""
    _hass, registry, coordinator = await _setup_services()
    handler = registry.handlers["clear_schedule"]

    with patch.object(services, "get_isn_from_target", AsyncMock(return_value=[])):
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_call(day="Mon"))
    _assert_translated_exception(exc_info.value, "no_valid_battery_targets")

    with patch.object(
        services, "get_isn_from_target", AsyncMock(return_value=["missing"])
    ):
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_call(day="Mon"))
    _assert_translated_exception(
        exc_info.value,
        "battery_coordinator_not_found",
        {"identifiers": "missing"},
    )

    with patch.object(
        services, "get_isn_from_target", AsyncMock(return_value=["bat-1"])
    ):
        await handler(_call(day="Mon"))
        one_day = coordinator.set_battery_schedule_slots.await_args.args[1]
        assert one_day["Mon"] == []
        await handler(_call(day="all"))
        all_days = coordinator.set_battery_schedule_slots.await_args.args[1]
    assert all_days == {day: [] for day in BatterySchedule.DAYS}


def _power_call(**overrides):
    data = {
        "device_id": "meter-device",
        "abs": 1,
        "limitType": 0,
        "target": 3000,
        "powerDiff": 100,
        "lostTime": 10,
        "lostPowerMax": 4000,
    }
    data.update(overrides)
    return _call(**data)


async def test_meter_power_handler_builds_absolute_and_percent_payloads() -> None:
    """Power limiting builds the two vendor payload variants."""
    _hass, registry, coordinator = await _setup_services()
    handler = registry.handlers["set_meter_limit_power"]
    with patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=["meter-1"])
    ):
        await handler(_power_call())
        assert coordinator.set_meter_power_limit.await_args.args[0] == {
            "regulate": 10,
            "ctrlType": 0,
            "abs": 1,
            "limitType": 0,
            "lostTime": 10,
            "lostPowerMax": 4000,
            "powerDiff": 100,
            "target": 3000,
        }
        await handler(_power_call(limitType=1, target=None, targetPer=75))
        assert coordinator.set_meter_power_limit.await_args.args[0]["targetPer"] == 75

        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call(target=None))
        _assert_translated_exception(exc_info.value, "absolute_power_target_required")
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call(limitType=1, target=None))
        _assert_translated_exception(exc_info.value, "percentage_power_target_required")
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call(target=5001))
        _assert_translated_exception(
            exc_info.value,
            "target_exceeds_rated_power",
            {"max_rate": "5000"},
        )
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call(lostPowerMax=5001))
        _assert_translated_exception(
            exc_info.value,
            "lost_power_exceeds_rated_power",
            {"max_rate": "5000"},
        )


async def test_meter_power_rate_fallback_and_target_failures() -> None:
    """Rated-power validation uses a safe fallback and reports unresolved meters."""
    _hass, registry, coordinator = await _setup_services()
    handler = registry.handlers["set_meter_limit_power"]
    coordinator.get_max_inverter_rate_w = Mock(side_effect=RuntimeError("bad metadata"))

    with patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=["meter-1"])
    ):
        await handler(_power_call(target=9999, lostPowerMax=9999))
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call(target=10001, lostPowerMax=1))
        _assert_translated_exception(
            exc_info.value,
            "target_exceeds_rated_power",
            {"max_rate": "10000"},
        )

    with patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=[])
    ):
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call())
    _assert_translated_exception(exc_info.value, "no_valid_meter_targets")
    with patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=["missing"])
    ):
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(_power_call())
    _assert_translated_exception(
        exc_info.value,
        "meter_coordinator_not_found",
        {"identifiers": "missing"},
    )


async def test_meter_power_uses_default_rate_without_metadata_helper() -> None:
    """Power validation defaults to 10 kW when a coordinator has no rating helper."""
    coordinator = SimpleNamespace(
        data=FakeCoordinator().data,
        set_meter_power_limit=AsyncMock(),
    )
    _hass, registry, coordinator = await _setup_services(coordinator)
    with patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=["meter-1"])
    ):
        await registry.handlers["set_meter_limit_power"](
            _power_call(target=9000, lostPowerMax=9000)
        )
    coordinator.set_meter_power_limit.assert_awaited_once()


async def test_current_zero_and_disable_meter_handlers() -> None:
    """Current, zero-power and disable services validate and forward exact payloads."""
    _hass, registry, coordinator = await _setup_services()
    resolve = patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=["meter-1"])
    )
    with resolve:
        current = registry.handlers["set_meter_limit_current"]
        with pytest.raises(ServiceValidationError) as exc_info:
            await current(
                _call(
                    device_id="meter-device",
                    lostTime=10,
                    lostCurrMax=11,
                    maxOutCurr=10,
                    maxInCurr=9,
                    currDiff=1,
                )
            )
        _assert_translated_exception(
            exc_info.value,
            "lost_current_exceeds_output_current",
        )
        await current(
            _call(
                device_id="meter-device",
                lostTime="10",
                lostCurrMax="8",
                maxOutCurr="10",
                maxInCurr="9",
                currDiff="1",
            )
        )
        assert coordinator.set_meter_power_limit.await_args.args[0] == {
            "regulate": 10,
            "ctrlType": 1,
            "failSafe": 1,
            "usageType": 0,
            "lostTime": 10,
            "lostCurrMax": 8,
            "maxOutCurr": 10,
            "maxInCurr": 9,
            "currDiff": 1,
        }

        await registry.handlers["set_meter_zero_power"](
            _call(device_id="meter-device", lostTime="15")
        )
        assert coordinator.set_meter_power_limit.await_args.args[0] == {
            "regulate": 10,
            "ctrlType": 2,
            "lostTime": 15,
        }

        await registry.handlers["disable_meter_power_limit"](
            _call(device_id="meter-device")
        )
        assert coordinator.set_meter_power_limit.await_args.args[0] == {"regulate": 5}


async def test_compatibility_meter_power_handler_builds_setmeter_payload() -> None:
    """The 404 compatibility action scales percentage and maps its two toggles."""
    _hass, registry, coordinator = await _setup_services()
    with patch.object(
        services, "get_meter_isn_from_target", AsyncMock(return_value=["meter-1"])
    ):
        await registry.handlers["set_compatibility_meter_power_limit"](
            _call(
                device_id="meter-device",
                enabled=True,
                target_percentage=5,
                fail_safe=False,
            )
        )
        coordinator.set_compatibility_meter_power_limit.assert_awaited_once_with(
            {
                "enb": 1,
                "mod": 1,
                "regulate": 10,
                "power_limit_mode": 0,
                "com_interruption_enable_g100": 0,
                "billing_method": 0,
                "offset": 0,
                "target": 500,
                "abs": 0,
            }
        )

        await registry.handlers["set_compatibility_meter_power_limit"](
            _call(
                device_id="meter-device",
                enabled=False,
                target_percentage=-0.5,
                fail_safe=True,
            )
        )
        payload = coordinator.set_compatibility_meter_power_limit.await_args.args[0]
        assert payload["regulate"] == 5
        assert payload["target"] == -50
        assert payload["com_interruption_enable_g100"] == 1
