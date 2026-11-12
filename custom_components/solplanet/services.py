"""Services for Solplanet integration."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .client import BatterySchedule, ScheduleSlot
from .const import BATTERY_IDENTIFIER, DOMAIN, METER_IDENTIFIER

type ServiceTarget = Mapping[str, str | list[str]]
type MeterPayload = dict[str, int]
type MeterPayloadValidator = Callable[[object, MeterPayload], None]


def _coerce_schedule_int(value: object) -> int:
    """Coerce integral schedule values without truncating fractions or accepting booleans."""
    if isinstance(value, bool):
        raise vol.Invalid("Expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as err:
            raise vol.Invalid("Expected an integer") from err
    raise vol.Invalid("Expected an integer")


def _multiple_of_100(value: int) -> int:
    """Require schedule power values to use the inverter's 100 W step."""
    if value % 100:
        raise vol.Invalid("Value must be a multiple of 100")
    return value


_REPLACE_SCHEDULE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("start_hour"): vol.All(_coerce_schedule_int, vol.Range(min=0, max=23)),
        vol.Required("start_minute"): vol.All(_coerce_schedule_int, vol.In([0, 30])),
        vol.Required("duration"): vol.All(_coerce_schedule_int, vol.Range(min=1, max=4)),
        vol.Required("mode"): vol.In(["charge", "discharge"]),
    }
)

_SCHEDULE_POWER_SCHEMA = vol.All(
    _coerce_schedule_int,
    vol.Range(min=0, max=10000),
    _multiple_of_100,
)


def _build_target(call: ServiceCall) -> ServiceTarget:
    """
    Extract entity_id/device_id from call.data.

    Home Assistant merges any `target:` fields into `call.data` before the
    handler runs; `ServiceCall` itself has no `target` attribute.
    """
    target: dict[str, str | list[str]] = {}
    if "entity_id" in call.data:
        target["entity_id"] = call.data["entity_id"]
    if "device_id" in call.data:
        target["device_id"] = call.data["device_id"]
    return target


async def get_isn_from_target(hass: HomeAssistant, target: ServiceTarget) -> list[str]:
    """Get ISNs from target entity_ids or device_ids."""
    isns: set[str] = set()
    prefix = f"{BATTERY_IDENTIFIER}_"
    device_reg = dr.async_get(hass)

    def _isn_from_device(device: dr.DeviceEntry) -> str | None:
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN and identifier[1].startswith(prefix):
                return identifier[1].removeprefix(prefix)
        return None

    # Route entity lookups through the device registry
    if "entity_id" in target:
        entity_reg = er.async_get(hass)
        entity_ids = target["entity_id"] if isinstance(target["entity_id"], list) else [target["entity_id"]]
        for entity_id in entity_ids:
            if (entry := entity_reg.async_get(entity_id)) and entry.device_id:
                if device := device_reg.async_get(entry.device_id):
                    if isn := _isn_from_device(device):
                        isns.add(isn)

    if "device_id" in target:
        device_ids = target["device_id"] if isinstance(target["device_id"], list) else [target["device_id"]]
        for device_id in device_ids:
            if device := device_reg.async_get(device_id):
                if isn := _isn_from_device(device):
                    isns.add(isn)

    return list(isns)


async def get_meter_isn_from_target(
    hass: HomeAssistant, target: ServiceTarget
) -> list[str]:
    """Get meter serial(s) from target entity_ids or device_ids."""
    isns: set[str] = set()

    prefix = f"{METER_IDENTIFIER}_"
    device_reg = dr.async_get(hass)

    if "entity_id" in target:
        entity_reg = er.async_get(hass)
        entity_ids = target["entity_id"] if isinstance(target["entity_id"], list) else [target["entity_id"]]
        for entity_id in entity_ids:
            if (entry := entity_reg.async_get(entity_id)) and entry.device_id:
                if device := device_reg.async_get(entry.device_id):
                    for identifier in device.identifiers:
                        if identifier[0] == DOMAIN and identifier[1].startswith(prefix):
                            isns.add(identifier[1].removeprefix(prefix))
                            break

    if "device_id" in target:
        device_ids = target["device_id"] if isinstance(target["device_id"], list) else [target["device_id"]]
        for device_id in device_ids:
            if device := device_reg.async_get(device_id):
                for identifier in device.identifiers:
                    if identifier[0] != DOMAIN:
                        continue
                    # Device identifiers are created as f"{METER_IDENTIFIER}_{meter_isn}".
                    if identifier[1].startswith(prefix):
                        isns.add(identifier[1].removeprefix(prefix))

    return list(isns)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Solplanet integration."""

    async def replace_schedule_day(call: ServiceCall) -> None:
        """Replace every slot for one day in a single schedule write."""
        target = _build_target(call)
        isns = await get_isn_from_target(hass, target)
        if not isns:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_valid_battery_targets",
            )

        try:
            slots = [ScheduleSlot.from_dict(slot_data) for slot_data in call.data["slots"]]
            if len(slots) > 6:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="schedule_slot_limit",
                )
            ScheduleSlot.validate_slots(slots)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_schedule",
                translation_placeholders={"error": str(err)},
            ) from err

        processed = False
        for runtime in hass.data[DOMAIN].values():
            coordinator = runtime.coordinator
            if isn := next(
                (identifier for identifier in isns if identifier in coordinator.data[BATTERY_IDENTIFIER]),
                None,
            ):
                await coordinator.replace_battery_schedule_day(
                    isn,
                    call.data["day"],
                    slots,
                    pin=call.data.get("pin"),
                    pout=call.data.get("pout"),
                )
                processed = True

        if not processed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="battery_coordinator_not_found",
                translation_placeholders={"identifiers": ", ".join(sorted(isns))},
            )

    async def set_schedule_slots(call: ServiceCall) -> None:
        """Handle set_schedule_slots service."""
        target = _build_target(call)

        isns = await get_isn_from_target(hass, target)
        if not isns:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_valid_battery_targets",
            )

        processed = False
        for isn in isns:
            for runtime in hass.data[DOMAIN].values():
                coordinator = runtime.coordinator
                if isn in coordinator.data[BATTERY_IDENTIFIER]:
                    try:
                        # Get current schedule; "slots" may be absent if the schedule fetch
                        # failed at startup (coordinator stores "schedule": {} in that case).
                        schedule_data = coordinator.data[BATTERY_IDENTIFIER][isn].get("schedule") or {}
                        current_schedule = schedule_data.get("slots") or {}

                        # Create new slot
                        slot = ScheduleSlot.from_time(
                            start=f"{call.data['start_hour']:02d}:{call.data['start_minute']:02d}",
                            duration=call.data["duration"],
                            mode=call.data["mode"],
                        )

                        # Get existing slots for the day
                        new_slots: dict[str, list[ScheduleSlot]] = dict(current_schedule)
                        # Validate a copy so a rejected slot cannot mutate the
                        # coordinator's cached schedule in place.
                        day_slots = list(new_slots.get(call.data["day"], []))

                        if len(day_slots) >= 6:
                            raise ServiceValidationError(
                                translation_domain=DOMAIN,
                                translation_key="schedule_slot_limit",
                            )

                        day_slots.append(slot)
                        ScheduleSlot.validate_slots(
                            day_slots
                        )  # This will raise ValueError for validation issues

                        new_slots[call.data["day"]] = day_slots
                        await coordinator.set_battery_schedule_slots(isn, new_slots)
                        processed = True
                        break

                    except ValueError as err:
                        # Handle validation errors only (overlaps, duration, midnight crossing)
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="invalid_schedule",
                            translation_placeholders={"error": str(err)},
                        ) from err
                    except (KeyError, ConnectionError, TimeoutError) as err:
                        # Handle specific API/network errors
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="communication_error",
                            translation_placeholders={"error": str(err)},
                        ) from err

        if not processed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="battery_coordinator_not_found",
                translation_placeholders={"identifiers": ", ".join(sorted(isns))},
            )

    async def clear_schedule(call: ServiceCall) -> None:
        """Handle clear_schedule service."""
        target = _build_target(call)

        isns = await get_isn_from_target(hass, target)
        if not isns:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_valid_battery_targets",
            )

        processed = False
        for isn in isns:
            for runtime in hass.data[DOMAIN].values():
                coordinator = runtime.coordinator
                if isn in coordinator.data[BATTERY_IDENTIFIER]:
                    schedule_data = coordinator.data[BATTERY_IDENTIFIER][isn].get("schedule") or {}
                    current_schedule = schedule_data.get("slots") or {}

                    if call.data["day"] == "all":
                        new_slots: dict[str, list[ScheduleSlot]] = {
                            day: [] for day in BatterySchedule.DAYS
                        }
                    else:
                        # Keep other days unchanged
                        new_slots = dict(current_schedule)
                        new_slots[call.data["day"]] = []

                    await coordinator.set_battery_schedule_slots(isn, new_slots)
                    processed = True
                    break

        if not processed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="battery_coordinator_not_found",
                translation_placeholders={"identifiers": ", ".join(sorted(isns))},
            )

    async def _apply_meter_payload(
        target: ServiceTarget,
        payload: MeterPayload,
        validator: MeterPayloadValidator | None = None,
        *,
        compatibility: bool = False,
    ) -> None:
        """Apply a meter payload to the targeted main meter device(s)."""
        meter_isns = await get_meter_isn_from_target(hass, target)
        if not meter_isns:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_valid_meter_targets",
            )

        processed = False
        for meter_isn in meter_isns:
            for runtime in hass.data.get(DOMAIN, {}).values():
                coordinator = runtime.coordinator
                if meter_isn in coordinator.data.get(METER_IDENTIFIER, {}):
                    if validator is not None:
                        validator(coordinator, payload)
                    if compatibility:
                        await coordinator.set_compatibility_meter_power_limit(payload)
                    else:
                        await coordinator.set_meter_power_limit(payload)
                    processed = True
                    break

        if not processed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="meter_coordinator_not_found",
                translation_placeholders={"identifiers": ", ".join(sorted(meter_isns))},
            )

    async def set_meter_limit_power(call: ServiceCall) -> None:
        """Configure meter power limit mode (ctrlType=0)."""
        target = _build_target(call)

        limit_type = int(call.data["limitType"])
        payload: MeterPayload = {
            "regulate": 10,
            "ctrlType": 0,
            "abs": int(call.data["abs"]),
            "limitType": limit_type,
            "lostTime": int(call.data["lostTime"]),
            "lostPowerMax": int(call.data["lostPowerMax"]),
            "powerDiff": int(call.data["powerDiff"]),
        }

        if limit_type == 0:
            if call.data.get("target") is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="absolute_power_target_required",
                )
            payload["target"] = int(call.data["target"])
        else:
            if call.data.get("targetPer") is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="percentage_power_target_required",
                )
            payload["targetPer"] = int(call.data["targetPer"])

        def _validate_power_limits(
            coordinator: object, payload: MeterPayload
        ) -> None:
            max_rate = 10000
            if hasattr(coordinator, "get_max_inverter_rate_w"):
                try:
                    max_rate = int(coordinator.get_max_inverter_rate_w())
                except Exception:  # noqa: BLE001
                    max_rate = 10000

            # If using absolute W targets, constrain to inverter rated power.
            if payload.get("limitType") == 0:
                target_w = int(payload.get("target") or 0)
                if target_w > max_rate:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="target_exceeds_rated_power",
                        translation_placeholders={"max_rate": str(max_rate)},
                    )

            lost_w = int(payload.get("lostPowerMax") or 0)
            if lost_w > max_rate:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="lost_power_exceeds_rated_power",
                    translation_placeholders={"max_rate": str(max_rate)},
                )

        await _apply_meter_payload(target, payload, validator=_validate_power_limits)

    async def set_meter_limit_current(call: ServiceCall) -> None:
        """Configure meter current limit mode (ctrlType=1)."""
        target = _build_target(call)

        payload: MeterPayload = {
            "regulate": 10,
            "ctrlType": 1,
            # Keep vendor defaults explicitly in the payload (these were observed in the app).
            "failSafe": 1,
            "usageType": 0,
            "lostTime": int(call.data["lostTime"]),
            "lostCurrMax": int(call.data["lostCurrMax"]),
            "maxOutCurr": int(call.data["maxOutCurr"]),
            "maxInCurr": int(call.data["maxInCurr"]),
            "currDiff": int(call.data["currDiff"]),
        }

        # The inverter expects lostCurrMax in range [0..maxOutCurr].
        if payload["lostCurrMax"] > payload["maxOutCurr"]:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="lost_current_exceeds_output_current",
            )

        await _apply_meter_payload(target, payload)

    async def set_meter_zero_power(call: ServiceCall) -> None:
        """Configure meter zero power mode (ctrlType=2)."""
        target = _build_target(call)

        payload: MeterPayload = {
            "regulate": 10,
            "ctrlType": 2,
            "lostTime": int(call.data["lostTime"]),
        }

        await _apply_meter_payload(target, payload)

    async def disable_meter_power_limit(call: ServiceCall) -> None:
        """Disable meter power limit control (regulate=5)."""
        target = _build_target(call)

        await _apply_meter_payload(target, {"regulate": 5})

    async def set_compatibility_meter_power_limit(call: ServiceCall) -> None:
        """Configure the simpler setmeter power-limit protocol."""
        target = _build_target(call)
        payload: MeterPayload = {
            "enb": 1,
            "mod": 1,
            "regulate": 10 if call.data["enabled"] else 5,
            "power_limit_mode": 0,
            "com_interruption_enable_g100": 1 if call.data["fail_safe"] else 0,
            "billing_method": 0,
            "offset": 0,
            "target": round(float(call.data["target_percentage"]) * 100),
            "abs": 0,
        }
        await _apply_meter_payload(target, payload, compatibility=True)

    # Service schemas stay the same
    hass.services.async_register(
        DOMAIN,
        "replace_schedule_day",
        replace_schedule_day,
        schema=vol.Schema(
            {
                vol.Optional("entity_id"): vol.Any(str, [str]),
                vol.Optional("device_id"): vol.Any(str, [str]),
                vol.Required("day"): vol.In(BatterySchedule.DAYS),
                vol.Required("slots"): vol.All([_REPLACE_SCHEDULE_SLOT_SCHEMA], vol.Length(max=6)),
                vol.Optional("pin"): _SCHEDULE_POWER_SCHEMA,
                vol.Optional("pout"): _SCHEDULE_POWER_SCHEMA,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "set_schedule_slots",
        set_schedule_slots,
        schema=vol.Schema(
            {
                vol.Optional("entity_id"): vol.Any(str, [str]),
                vol.Optional("device_id"): vol.Any(str, [str]),
                vol.Required("day"): vol.In(BatterySchedule.DAYS),
                vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Required("start_minute"): vol.In([0, 30]),
                vol.Required("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
                vol.Required("mode"): vol.In(["charge", "discharge"]),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "clear_schedule",
        clear_schedule,
        schema=vol.Schema(
            {
                vol.Optional("entity_id"): vol.Any(str, [str]),
                vol.Optional("device_id"): vol.Any(str, [str]),
                vol.Required("day"): vol.In(["all"] + BatterySchedule.DAYS),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "set_meter_limit_power",
        set_meter_limit_power,
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.Any(str, [str]),
                vol.Optional("entity_id"): vol.Any(str, [str]),
                # UI selectors in services.yaml send strings; accept both and coerce to int.
                vol.Required("abs"): vol.All(vol.Coerce(int), vol.In([0, 1])),
                vol.Required("limitType"): vol.All(vol.Coerce(int), vol.In([0, 1])),
                # Actual upper bound depends on inverter `rate`; validated in the handler.
                vol.Optional("target"): vol.All(vol.Coerce(int), vol.Range(min=0, max=50000)),
                vol.Optional("targetPer"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Required("powerDiff"): vol.All(vol.Coerce(int), vol.Range(min=-1000, max=1000)),
                vol.Required("lostTime"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                # Actual upper bound depends on inverter `rate`; validated in the handler.
                vol.Required("lostPowerMax"): vol.All(vol.Coerce(int), vol.Range(min=0, max=50000)),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "set_meter_limit_current",
        set_meter_limit_current,
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.Any(str, [str]),
                vol.Optional("entity_id"): vol.Any(str, [str]),
                vol.Required("maxOutCurr"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3000)),
                vol.Required("maxInCurr"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3000)),
                vol.Required("currDiff"): vol.All(vol.Coerce(int), vol.Range(min=-10, max=10)),
                vol.Required("lostTime"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                # Actual upper bound depends on maxOutCurr; validated in the handler.
                vol.Required("lostCurrMax"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3000)),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "set_meter_zero_power",
        set_meter_zero_power,
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.Any(str, [str]),
                vol.Optional("entity_id"): vol.Any(str, [str]),
                vol.Required("lostTime"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "disable_meter_power_limit",
        disable_meter_power_limit,
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.Any(str, [str]),
                vol.Optional("entity_id"): vol.Any(str, [str]),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "set_compatibility_meter_power_limit",
        set_compatibility_meter_power_limit,
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.Any(str, [str]),
                vol.Optional("entity_id"): vol.Any(str, [str]),
                vol.Required("enabled"): bool,
                vol.Required("target_percentage"): vol.All(
                    vol.Coerce(float), vol.Range(min=-1, max=10)
                ),
                vol.Required("fail_safe"): bool,
            }
        ),
    )
