"""Solplanet data coordinator."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast, override

from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api_adapter import SolplanetApiAdapter
from .client import (
    BatterySchedule,
    BatteryWorkMode,
    BatteryWorkModes,
    GetBatteryInfoResponse,
    ScheduleSlot,
)
from .const import (
    BATTERY_IDENTIFIER,
    DISCOVERY_SIGNAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
    METER_MODEL_NAMES,
)
from .modbus import DataType
from .validation import (
    is_zero_filled_battery_payload,
    retain_previous_battery_energy_values,
)

_LOGGER = logging.getLogger(__name__)

METADATA_UPDATE_INTERVAL = timedelta(hours=1)
FAILED_UPDATE_INTERVAL = timedelta(minutes=10)
MAX_FAILED_UPDATES = 3

type SolplanetData = dict[str, dict[str, dict[str, Any]]]


def _empty_data() -> SolplanetData:
    """Return the shared data structure used by all endpoint coordinators."""
    return {
        DONGLE_IDENTIFIER: {},
        INVERTER_IDENTIFIER: {},
        BATTERY_IDENTIFIER: {},
        METER_IDENTIFIER: {},
    }


@dataclass(slots=True)
class SolplanetRuntimeData:
    """Runtime state shared by all coordinators for one Solplanet gateway."""

    api: SolplanetApiAdapter
    data: SolplanetData = field(default_factory=_empty_data)
    coordinator_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    metadata_coordinator: SolplanetDataUpdateCoordinator | None = field(default=None, init=False)
    inverter_coordinator: SolplanetDataUpdateCoordinator | None = field(default=None, init=False)
    battery_coordinator: SolplanetDataUpdateCoordinator | None = field(default=None, init=False)
    meter_coordinator: SolplanetDataUpdateCoordinator | None = field(default=None, init=False)
    dongle_coordinator: SolplanetDataUpdateCoordinator | None = field(default=None, init=False)

    @property
    def coordinator(self) -> SolplanetDataUpdateCoordinator:
        """Return the metadata coordinator used as the integration controller."""
        if self.metadata_coordinator is None:
            raise RuntimeError("Solplanet metadata coordinator is not initialized")
        return self.metadata_coordinator

    def coordinator_for(self, device_type: str, data_type: str) -> SolplanetDataUpdateCoordinator:
        """Return the coordinator responsible for an entity's data endpoint."""
        coordinator: SolplanetDataUpdateCoordinator | None = None

        if device_type == INVERTER_IDENTIFIER and data_type == "data":
            coordinator = self.inverter_coordinator
        elif device_type == BATTERY_IDENTIFIER and data_type == "data":
            coordinator = self.battery_coordinator
        elif device_type == METER_IDENTIFIER and data_type in {"data", "app_data"}:
            coordinator = self.meter_coordinator
        elif device_type == DONGLE_IDENTIFIER and data_type == "warnings":
            coordinator = self.dongle_coordinator

        return coordinator or self.coordinator

    async def async_request_metadata_refresh(self) -> None:
        """Refresh settings and metadata after a write."""
        await self.coordinator.async_request_refresh()


type SolplanetConfigEntry = ConfigEntry[SolplanetRuntimeData]


class SolplanetDataUpdateCoordinator(DataUpdateCoordinator[SolplanetData]):
    """Base coordinator for one Solplanet endpoint family."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SolplanetRuntimeData,
        config_entry: SolplanetConfigEntry,
        name: str,
        update_interval: timedelta,
        error_interval: timedelta = FAILED_UPDATE_INTERVAL,
    ) -> None:
        """Initialize an endpoint coordinator."""
        self.runtime = runtime
        self.__api = runtime.api
        self.config_entry_id = config_entry.entry_id
        self._default_interval = update_interval
        self._error_interval = max(update_interval, error_interval)
        self._failed_update_count = 0
        self._source_name = name
        self.failed_device_ids: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{name}",
            update_interval=update_interval,
        )
        # All endpoint coordinators expose the same merged cache to entities.
        self.data = runtime.data

    @property
    def api(self) -> SolplanetApiAdapter:
        """Return the API adapter for this gateway."""
        return self.__api

    def get_max_inverter_rate_w(self) -> int:
        """Return the maximum inverter rated power (W) for this config entry.

        This is sourced from `getdev.cgi?device=2` -> `inv[].rate`.
        Some features (export limits, schedule power) should scale to the inverter rating
        rather than using hard-coded defaults.
        """
        rates: list[int] = []
        invs = self.data.get(INVERTER_IDENTIFIER, {}) if isinstance(self.data, dict) else {}
        for inv_entry in invs.values():
            info = inv_entry.get("info") if isinstance(inv_entry, dict) else None
            rate = getattr(info, "rate", None)
            if isinstance(rate, int) and rate > 0:
                rates.append(rate)

        return max(rates) if rates else 10000

    @override
    async def _async_update_data(self) -> SolplanetData:
        """Fetch one endpoint family while serializing access to the gateway."""
        async with self.runtime.coordinator_lock:
            try:
                await self._async_update_runtime_data()
            except Exception as err:
                self._failed_update_count += 1
                if self._failed_update_count == MAX_FAILED_UPDATES:
                    self.update_interval = self._error_interval
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="update_failed",
                    translation_placeholders={
                        "source": self._source_name,
                        "error": str(err),
                    },
                ) from err

        if self._failed_update_count:
            self._failed_update_count = 0
            self.update_interval = self._default_interval

        return self.runtime.data

    async def _async_update_runtime_data(self) -> None:
        """Update this endpoint family's section of the shared cache."""
        raise NotImplementedError

    async def set_inverter_power(self, on: bool) -> None:
        """Set inverter power (offset 200). 1=on, 0=off."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.modbus_write_single_holding_register(
                    data_type=DataType.U16,
                    device_address=3,
                    register_address=40201,  # 40001 + 200
                    value=1 if on else 0,
                    dry_run=False,
                )
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="modbus_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="inverter_power_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def dongle_sync_time(self) -> None:
        """Sync dongle time (device=1, action=settime) using Home Assistant local time."""
        if self.__api.version != "v2":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dongle_operation_unsupported_v1",
            )

        now = dt_util.now()
        payload = {
            "device": 1,
            "action": "settime",
            "value": {"time": now.strftime("%Y%m%d%H%M%S")},
        }

        try:
            async with self.runtime.coordinator_lock:
                await self.__api.client.post("setting.cgi", payload)
            await self.runtime.async_request_metadata_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sync_time_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def dongle_reboot(self) -> None:
        """Reboot dongle (device=1, action=operation, reboot=1)."""
        if self.__api.version != "v2":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dongle_operation_unsupported_v1",
            )

        payload = {
            "device": 1,
            "action": "operation",
            "value": {"reboot": 1},
        }

        try:
            async with self.runtime.coordinator_lock:
                await self.__api.client.post("setting.cgi", payload)
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reboot_dongle_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_meter_power_limit(self, payload: dict) -> None:
        """Set meter power limit / zero export configuration (V2 app-protocol).

        This is a generic wrapper around:
          POST setting.cgi {"cmd":"set_meter_req","payload":{...}}
        """
        if self.__api.version != "v2":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="meter_power_limit_unsupported_v1",
            )

        try:
            async with self.runtime.coordinator_lock:
                rsp = await self.__api.client.post(
                    "setting.cgi",
                    {"cmd": "set_meter_req", "payload": payload},
                )

        except ClientResponseError as err:
            if err.status == 404:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="meter_power_limit_unavailable",
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_meter_power_limit_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_meter_power_limit_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        # Expected success response:
        # {"cmd": "set_meter_rsp", "status": 200}
        if not isinstance(rsp, dict) or rsp.get("status") != 200:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unexpected_meter_response",
                translation_placeholders={"response": str(rsp)},
            )

        self.hass.async_create_task(self.runtime.async_request_metadata_refresh())

    async def set_compatibility_meter_power_limit(self, payload: dict) -> None:
        """Set the simpler meter power-limit configuration used by some V2 firmware."""
        if self.__api.version != "v2":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="meter_power_limit_unsupported_v1",
            )

        try:
            async with self.runtime.coordinator_lock:
                rsp = await self.__api.client.post(
                    "setting.cgi",
                    {"device": 3, "action": "setmeter", "value": payload},
                )
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_meter_power_limit_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        if not isinstance(rsp, dict) or rsp.get("dat") != "ok":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unexpected_meter_response",
                translation_placeholders={"response": str(rsp)},
            )

        self.hass.async_create_task(self.runtime.async_request_metadata_refresh())

    async def _write_battery_more_setting(self, register_offset: int, value: int) -> None:
        """Write a battery "More Settings" register via Modbus (function 0x10)."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.modbus_write_multiple_holding_registers(
                    device_address=3,
                    register_address=40001 + register_offset,
                    values=[value],
                )
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="modbus_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_power(self, on: bool) -> None:
        """Set battery power (offset 1500). 1=on, 0=shutdown."""
        await self._write_battery_more_setting(register_offset=1500, value=1 if on else 0)

    async def set_battery_sleep_enabled(self, enabled: bool) -> None:
        """Set battery sleep enabled flag (offset 1501). 0=enabled, 1=disabled."""
        await self._write_battery_more_setting(register_offset=1501, value=0 if enabled else 1)

    async def set_battery_led_color_index(self, index: int) -> None:
        """Set battery LED color index (offset 1502)."""
        await self._write_battery_more_setting(register_offset=1502, value=int(index))

    async def set_battery_led_brightness(self, brightness: int) -> None:
        """Set battery LED brightness percent (offset 1503)."""
        await self._write_battery_more_setting(register_offset=1503, value=int(brightness))

    async def set_battery_work_mode(self, sn: str, mode: BatteryWorkMode) -> None:
        """Set battery work mode."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.set_battery_work_mode(sn, mode)
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_soc_min(self, sn: str, value: int) -> None:
        """Set battery soc min."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.set_battery_soc_min(sn, value)
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_soc_max(self, sn: str, value: int) -> None:
        """Set battery soc max."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.set_battery_soc_max(sn, value)
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_schedule_slots(self, sn: str, slots: dict[str, list[ScheduleSlot]]) -> None:
        """Set battery schedule slots."""
        try:
            _LOGGER.debug("Setting schedule slots for %s: %s", sn, slots)
            async with self.runtime.coordinator_lock:
                current = await self.__api.get_schedule()
                raw_schedule = BatterySchedule.encode_schedule(
                    slots,
                    pin=current["raw"].get("Pin", 0),
                    pout=current["raw"].get("Pout", 0),
                )
                _LOGGER.debug("Encoded schedule: %s", raw_schedule)
                await self.__api.set_schedule_slots(raw_schedule)
            self.hass.async_create_task(self.runtime.async_request_metadata_refresh())
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_schedule_power(self, pin: int | None = None, pout: int | None = None) -> None:
        """Set battery schedule power settings."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.set_schedule_power(pin, pout)
            self.hass.async_create_task(self.runtime.async_request_metadata_refresh())
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_schedule_pin(self, sn: str, pin: int) -> None:
        """Set battery schedule pin."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.set_schedule_pin(pin)
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def set_battery_schedule_pout(self, sn: str, pout: int) -> None:
        """Set battery schedule pout."""
        try:
            async with self.runtime.coordinator_lock:
                await self.__api.set_schedule_pout(pout)
            await self.runtime.async_request_metadata_refresh()
        except NotImplementedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_unsupported_v1",
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="battery_operation_failed",
                translation_placeholders={"error": str(err)},
            ) from err


def _legacy_meter_payload_looks_valid(meter_data: object) -> bool:
    """Return whether legacy meter data is real rather than a zero-filled stub."""
    if meter_data is None:
        return False

    timestamp = getattr(meter_data, "tim", None)
    if isinstance(timestamp, str) and timestamp.strip():
        return True

    return any(
        isinstance(value := getattr(meter_data, attribute, None), int | float) and value != 0
        for attribute in ("pac", "itd", "otd", "iet", "oet")
    )


def _is_enabled(value: object) -> bool:
    """Return whether a firmware enable flag is set, accepting numeric strings."""
    if isinstance(value, int):
        return value == 1
    if not isinstance(value, str):
        return False
    try:
        return int(value) == 1
    except ValueError:
        return False


class SolplanetMetadataUpdateCoordinator(SolplanetDataUpdateCoordinator):
    """Poll device inventory, settings, and other slowly changing data."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SolplanetRuntimeData,
        config_entry: SolplanetConfigEntry,
    ) -> None:
        """Initialize the hourly metadata coordinator."""
        self._new_device_ids: dict[str, set[str]] = {}
        super().__init__(
            hass,
            runtime,
            config_entry,
            name="metadata",
            update_interval=METADATA_UPDATE_INTERVAL,
            error_interval=METADATA_UPDATE_INTERVAL,
        )

    @override
    async def _async_update_data(self) -> SolplanetData:
        """Refresh metadata, then announce devices first seen by this update."""
        data = await super()._async_update_data()

        coordinators = {
            INVERTER_IDENTIFIER: self.runtime.inverter_coordinator,
            BATTERY_IDENTIFIER: self.runtime.battery_coordinator,
            METER_IDENTIFIER: self.runtime.meter_coordinator,
            DONGLE_IDENTIFIER: self.runtime.dongle_coordinator,
        }
        for device_type, device_ids in self._new_device_ids.items():
            if coordinator := coordinators[device_type]:
                await coordinator.async_refresh()
            async_dispatcher_send(
                self.hass,
                DISCOVERY_SIGNAL,
                self.config_entry_id,
                device_type,
                device_ids,
            )

        self._new_device_ids = {}
        return data

    @override
    async def _async_update_runtime_data(self) -> None:
        """Refresh inventory and configuration data."""
        previous_ids = {device_type: set(devices) for device_type, devices in self.runtime.data.items()}
        inverter_info = await self.api.get_inverter_info()
        if not inverter_info.inv:
            raise RuntimeError("No inverters returned by the inventory endpoint")

        if self.api.version == "v2":
            await self._async_update_dongle_metadata()

        await self._async_update_inverter_metadata(inverter_info.inv)
        await self._async_update_battery_metadata(inverter_info.inv)
        await self._async_update_meter_metadata(inverter_info.inv)
        self._new_device_ids = {
            device_type: set(devices) - previous_ids[device_type]
            for device_type, devices in self.runtime.data.items()
            if set(devices) - previous_ids[device_type]
        }

    async def _async_update_dongle_metadata(self) -> None:
        """Refresh dongle identity and network information."""
        previous = self.runtime.data[DONGLE_IDENTIFIER]
        try:
            dongle_info = await self.api.client.get("getdev.cgi")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed fetching dongle metadata: %s", err, exc_info=True)
            return
        if not isinstance(dongle_info, dict):
            _LOGGER.debug("Ignoring unexpected dongle metadata: %r", dongle_info)
            return

        dongle_id = (
            dongle_info.get("psn") or dongle_info.get("ethmac") or dongle_info.get("wlanmac") or "unknown"
        )
        previous_entry = previous.get(dongle_id, {})

        try:
            network_info = await self.api.client.get("wlanget.cgi?info=2")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed fetching dongle network info: %s", err, exc_info=True)
            network_info = previous_entry.get("network")

        self.runtime.data[DONGLE_IDENTIFIER] = {
            dongle_id: {
                "data": dongle_info,
                "network": network_info,
                "warnings": previous_entry.get("warnings"),
            }
        }

    async def _async_update_inverter_metadata(self, inverter_info: list[Any]) -> None:
        """Refresh inverter inventory and configuration."""
        previous = self.runtime.data[INVERTER_IDENTIFIER]
        power_on: bool | None = None

        try:
            power_register = await self.api.modbus_read_holding_registers(
                data_type=DataType.U16,
                device_address=3,
                register_address=40201,
                register_count=1,
            )
            if isinstance(power_register, list):
                power_register = power_register[0] if power_register else None
            if isinstance(power_register, int):
                power_on = power_register == 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed reading inverter power setting: %s", err, exc_info=True)

        updated: dict[str, dict[str, Any]] = {}
        for info in inverter_info:
            if not info.isn:
                continue
            entry = dict(previous.get(info.isn, {}))
            entry.setdefault("data", None)
            entry["info"] = info
            if power_on is not None:
                entry["more_settings"] = {"power_on": power_on}
            else:
                entry.setdefault("more_settings", {})
            updated[info.isn] = entry

        self.runtime.data[INVERTER_IDENTIFIER] = updated

    async def _async_update_battery_metadata(self, inverter_info: list[Any]) -> None:
        """Refresh battery inventory, schedule, and configuration."""
        if self.api.version != "v2":
            self.runtime.data[BATTERY_IDENTIFIER] = {}
            return

        battery_ids = [info.isn for info in inverter_info if info.isn and info.isStorage()]
        previous = self.runtime.data[BATTERY_IDENTIFIER]
        if not battery_ids:
            self.runtime.data[BATTERY_IDENTIFIER] = {}
            return

        schedule: dict[str, Any] | None = None
        try:
            schedule = await self.api.get_schedule()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed fetching battery schedule: %s", err, exc_info=True)

        more_settings: dict[str, Any] | None = None
        try:
            registers = await self.api.modbus_read_holding_registers(
                data_type=DataType.U16,
                device_address=3,
                register_address=41501,
                register_count=4,
            )
            if isinstance(registers, list) and len(registers) >= 4:
                more_settings = {
                    "power_on": int(registers[0] or 0) == 1,
                    "sleep_enabled": int(registers[1] or 0) == 0,
                    "led_color_index": int(registers[2] or 0),
                    "led_brightness": int(registers[3] or 0),
                }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed reading battery settings: %s", err, exc_info=True)

        updated: dict[str, dict[str, Any]] = {}
        for battery_id in battery_ids:
            previous_entry = previous.get(battery_id, {})
            entry = dict(previous_entry)
            entry.setdefault("data", None)

            info: GetBatteryInfoResponse | None
            try:
                info = await self.api.get_battery_info(battery_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed fetching battery metadata for %s: %s",
                    battery_id,
                    err,
                    exc_info=True,
                )
                info = cast(GetBatteryInfoResponse | None, previous_entry.get("info"))

            entry["info"] = info
            if info is not None:
                entry["work_modes"] = {
                    "all": BatteryWorkModes().get_all_modes(info.type, info.mod_r),
                    "selected": BatteryWorkModes().get_mode(info.type, info.mod_r),
                }
            else:
                entry.setdefault("work_modes", {"all": [], "selected": None})

            if schedule is not None:
                entry["schedule"] = schedule
            else:
                entry.setdefault("schedule", {})

            if more_settings is not None:
                entry["more_settings"] = more_settings
            else:
                entry.setdefault("more_settings", {})

            updated[battery_id] = entry

        self.runtime.data[BATTERY_IDENTIFIER] = updated

    async def _async_update_meter_metadata(self, inverter_info: list[Any]) -> None:
        """Refresh meter inventory and its power-limit configuration."""
        previous = self.runtime.data[METER_IDENTIFIER]

        if self.api.version == "v2":
            app_meters = await self._async_get_app_meter_inventory(previous)
            if app_meters is not None:
                self.runtime.data[METER_IDENTIFIER] = app_meters
                return

        await self._async_update_legacy_meter_metadata(inverter_info, previous)

    async def _async_get_app_meter_inventory(
        self, previous: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]] | None:
        """Return app-protocol meter inventory, or None when unsupported/empty."""
        try:
            response = await self.api.client.post(
                "getting.cgi",
                {"cmd": "get_app_dev_info_req", "payload": {"type": [4]}},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("App meter inventory is unavailable: %s", err, exc_info=True)
            return None

        if not isinstance(response, dict) or response.get("status") != 200:
            return None

        payload = response.get("payload") or {}
        main_meters = payload.get("mainMeter") or []
        sub_meters = payload.get("subMeter") or []
        meter_records = [meter for meter in [*main_meters, *sub_meters] if isinstance(meter, dict)]
        if not meter_records:
            return None

        primary_id = main_meters[0].get("sn") if main_meters and isinstance(main_meters[0], dict) else None
        updated: dict[str, dict[str, Any]] = {}

        for meter in meter_records:
            meter_id = meter.get("sn") or f"addr_{meter.get('address')}"
            entry = dict(previous.get(meter_id, {}))
            entry["app_info"] = meter
            updated[meter_id] = entry

        target_id = primary_id or next(iter(updated))
        updated[target_id].setdefault("app_data", None)

        try:
            response = await self.api.client.post("getting.cgi", {"cmd": "get_meter_req"})
            if isinstance(response, dict) and response.get("status") == 200:
                updated[target_id]["meter_req"] = response.get("payload") or {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed fetching meter configuration: %s", err, exc_info=True)

        return updated

    async def _async_update_legacy_meter_metadata(
        self,
        inverter_info: list[Any],
        previous: dict[str, dict[str, Any]],
    ) -> None:
        """Refresh V1 or fallback V2 meter metadata, including a secondary meter."""
        fallback_id = next((info.isn for info in inverter_info if info.isn), None)
        try:
            info = await self.api.get_meter_info()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed fetching legacy meter metadata: %s", err, exc_info=True)
            return

        meter_id = info.sn or fallback_id
        if meter_id is None:
            return

        main_entry = await self._build_legacy_meter_entry(
            meter_id, info, previous.get(meter_id), submeter_index=None
        )
        if main_entry is None:
            return

        updated: dict[str, dict[str, Any]] = {meter_id: main_entry}
        if self.api.version == "v2" and _is_enabled(getattr(info, "sec_enb", None)):
            sub_id = f"{meter_id}_sub1"
            sub_entry = await self._build_legacy_meter_entry(
                sub_id, info, previous.get(sub_id), submeter_index=1
            )
            if sub_entry is not None:
                sub_entry["is_submeter"] = True
                updated[sub_id] = sub_entry

        self.runtime.data[METER_IDENTIFIER] = updated

    async def _build_legacy_meter_entry(
        self,
        meter_id: str,
        info: Any,
        previous_entry: dict[str, Any] | None,
        *,
        submeter_index: int | None,
    ) -> dict[str, Any] | None:
        """Build one legacy meter entry, probing live data on first discovery."""
        entry = dict(previous_entry or {})
        entry["info"] = info
        model_code = (
            getattr(info, "sec_mod", None)
            if submeter_index is not None
            else getattr(info, "mod", None)
        )
        if isinstance(model_code, int):
            entry["model_name"] = METER_MODEL_NAMES.get(model_code, "")
        if submeter_index is not None:
            entry["submeter_index"] = submeter_index

        if previous_entry is None:
            try:
                data = await self.api.get_meter_data(submeter=submeter_index)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed probing legacy meter data for %s: %s",
                    meter_id,
                    err,
                    exc_info=True,
                )
                return None
            if self.api.version == "v2" and not _legacy_meter_payload_looks_valid(data):
                return None
            entry["data"] = data
        else:
            entry.setdefault("data", None)

        return entry


class SolplanetInverterUpdateCoordinator(SolplanetDataUpdateCoordinator):
    """Poll live inverter telemetry."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SolplanetRuntimeData,
        config_entry: SolplanetConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the inverter coordinator."""
        super().__init__(
            hass,
            runtime,
            config_entry,
            name="inverter",
            update_interval=update_interval,
        )

    @override
    async def _async_update_runtime_data(self) -> None:
        """Refresh live telemetry for all discovered inverters."""
        entries = self.runtime.data[INVERTER_IDENTIFIER]
        successful_updates = 0
        failed_device_ids: set[str] = set()
        last_error: Exception | None = None

        for inverter_id, entry in entries.items():
            try:
                entry["data"] = await self.api.get_inverter_data(inverter_id)
            except Exception as err:  # noqa: BLE001
                failed_device_ids.add(inverter_id)
                last_error = err
                _LOGGER.debug(
                    "Failed fetching inverter data for %s: %s",
                    inverter_id,
                    err,
                    exc_info=True,
                )
            else:
                successful_updates += 1

        self.failed_device_ids = failed_device_ids
        if entries and successful_updates == 0:
            raise last_error or RuntimeError("No inverter data returned")


class SolplanetBatteryUpdateCoordinator(SolplanetDataUpdateCoordinator):
    """Poll live battery telemetry."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SolplanetRuntimeData,
        config_entry: SolplanetConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the battery coordinator."""
        super().__init__(
            hass,
            runtime,
            config_entry,
            name="battery",
            update_interval=update_interval,
        )
        self._zero_filled_update_count = 0

    @override
    async def _async_update_runtime_data(self) -> None:
        """Refresh live telemetry for all discovered batteries."""
        entries = self.runtime.data[BATTERY_IDENTIFIER]
        successful_updates = 0
        zero_filled_updates = 0
        failed_device_ids: set[str] = set()
        last_error: Exception | None = None

        for battery_id, entry in entries.items():
            try:
                data = await self.api.get_battery_data(battery_id)
                if is_zero_filled_battery_payload(data):
                    zero_filled_updates += 1
                    _LOGGER.debug(
                        "Ignoring transient zero-filled battery data for %s",
                        battery_id,
                    )
                    continue
                retained_fields = retain_previous_battery_energy_values(
                    data, entry.get("data")
                )
                if retained_fields:
                    _LOGGER.debug(
                        "Retaining previous battery energy values for %s: %s",
                        battery_id,
                        ", ".join(retained_fields),
                    )
                entry["data"] = data
            except Exception as err:  # noqa: BLE001
                failed_device_ids.add(battery_id)
                last_error = err
                _LOGGER.debug(
                    "Failed fetching battery data for %s: %s",
                    battery_id,
                    err,
                    exc_info=True,
                )
            else:
                successful_updates += 1

        self.failed_device_ids = failed_device_ids
        if successful_updates:
            if self._zero_filled_update_count:
                self._zero_filled_update_count = 0
                self.update_interval = self._default_interval
            return

        if zero_filled_updates:
            self._zero_filled_update_count += 1
            if self._zero_filled_update_count == MAX_FAILED_UPDATES:
                self.update_interval = self._error_interval
            return

        if entries and successful_updates == 0:
            raise last_error or RuntimeError("No battery data returned")


class SolplanetMeterUpdateCoordinator(SolplanetDataUpdateCoordinator):
    """Poll live meter telemetry."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SolplanetRuntimeData,
        config_entry: SolplanetConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the meter coordinator."""
        super().__init__(
            hass,
            runtime,
            config_entry,
            name="meter",
            update_interval=update_interval,
        )

    @override
    async def _async_update_runtime_data(self) -> None:
        """Refresh app-protocol or legacy meter telemetry."""
        entries = self.runtime.data[METER_IDENTIFIER]
        app_meter_ids = [meter_id for meter_id, entry in entries.items() if "app_info" in entry]

        if app_meter_ids:
            response = await self.api.client.post("getting.cgi", {"cmd": "get_meter_data_req"})
            if not isinstance(response, dict) or response.get("status") != 200:
                raise RuntimeError(f"Unexpected get_meter_data response: {response}")
            target_id = next(
                (meter_id for meter_id in app_meter_ids if "app_data" in entries[meter_id]),
                app_meter_ids[0],
            )
            entries[target_id]["app_data"] = response.get("payload") or {}
            return

        if not entries:
            return

        successful_updates = 0
        failed_device_ids: set[str] = set()
        last_error: Exception | None = None
        for meter_id, entry in entries.items():
            try:
                data = await self.api.get_meter_data(submeter=entry.get("submeter_index"))
                if self.api.version == "v2" and not _legacy_meter_payload_looks_valid(data):
                    raise RuntimeError("legacy meter returned an empty or zero-filled payload")
            except Exception as err:  # noqa: BLE001
                failed_device_ids.add(meter_id)
                last_error = err
                _LOGGER.debug("Failed fetching meter data for %s: %s", meter_id, err, exc_info=True)
            else:
                entry["data"] = data
                successful_updates += 1

        self.failed_device_ids = failed_device_ids
        if successful_updates == 0:
            raise last_error or RuntimeError("No meter data returned")


class SolplanetDongleUpdateCoordinator(SolplanetDataUpdateCoordinator):
    """Poll changing dongle diagnostics separately from static metadata."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SolplanetRuntimeData,
        config_entry: SolplanetConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the dongle diagnostics coordinator."""
        super().__init__(
            hass,
            runtime,
            config_entry,
            name="dongle",
            update_interval=update_interval,
        )

    @override
    async def _async_update_runtime_data(self) -> None:
        """Refresh dongle warnings; HTTP 404 means there are no warnings."""
        entries = self.runtime.data[DONGLE_IDENTIFIER]
        if not entries:
            return

        try:
            warnings = await self.api.client.get("getdevdata.cgi?device=1")
        except ClientResponseError as err:
            if err.status != 404:
                raise
            warnings = {}

        next(iter(entries.values()))["warnings"] = warnings
