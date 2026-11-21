"""The Solplanet integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.device_registry as dr
from homeassistant.const import CONF_HOST, CONF_MAC, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_adapter import SolplanetApiAdapter
from .client import SolplanetClient
from .const import (
    BATTERY_IDENTIFIER,
    BATTERY_MANUFACTURER_NAMES,
    BATTERY_MODEL_NAMES,
    CONF_INTERVAL,
    DEFAULT_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    MANUFACTURER,
    MAX_INTERVAL,
    METER_IDENTIFIER,
    METER_MODEL_NAMES,
    MIN_INTERVAL,
)
from .coordinator import (
    SolplanetBatteryUpdateCoordinator,
    SolplanetConfigEntry,
    SolplanetDongleUpdateCoordinator,
    SolplanetInverterUpdateCoordinator,
    SolplanetMetadataUpdateCoordinator,
    SolplanetMeterUpdateCoordinator,
    SolplanetRuntimeData,
    error_detail,
)
from .services import async_setup_services
from .validation import normalize_mac_address

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


def _network_mac_connections(*mac_addresses: str | None) -> set[tuple[str, str]]:
    """Return normalized device-registry connections for available MAC addresses."""
    return {
        (dr.CONNECTION_NETWORK_MAC, normalized_mac)
        for mac_address in mac_addresses
        if (normalized_mac := normalize_mac_address(mac_address)) is not None
    }


def _current_device_identifiers(
    entry: SolplanetConfigEntry,
) -> dict[str, set[tuple[str, str]]]:
    """Return registry identifiers represented by the current inventory."""
    data = entry.runtime_data.data
    return {
        DONGLE_IDENTIFIER: {
            (DOMAIN, f"{DONGLE_IDENTIFIER}_{dongle_id}") for dongle_id in data[DONGLE_IDENTIFIER]
        },
        INVERTER_IDENTIFIER: {(DOMAIN, inverter_id) for inverter_id in data[INVERTER_IDENTIFIER]},
        BATTERY_IDENTIFIER: {
            (DOMAIN, f"{BATTERY_IDENTIFIER}_{battery_id}") for battery_id in data[BATTERY_IDENTIFIER]
        },
        METER_IDENTIFIER: {(DOMAIN, f"{METER_IDENTIFIER}_{meter_id}") for meter_id in data[METER_IDENTIFIER]},
    }


def _device_type_from_identifier(identifier: tuple[str, str]) -> str:
    """Return the Solplanet device type encoded in a registry identifier."""
    value = identifier[1]
    if value.startswith(f"{DONGLE_IDENTIFIER}_"):
        return DONGLE_IDENTIFIER
    if value.startswith(f"{BATTERY_IDENTIFIER}_"):
        return BATTERY_IDENTIFIER
    if value.startswith(f"{METER_IDENTIFIER}_"):
        return METER_IDENTIFIER
    return INVERTER_IDENTIFIER


@callback
def _remove_stale_devices(
    device_registry: dr.DeviceRegistry,
    entry: SolplanetConfigEntry,
    current_identifiers: dict[str, set[tuple[str, str]]],
) -> None:
    """Detach devices absent from an authoritative inventory snapshot."""
    data = entry.runtime_data.data
    authoritative_device_types = {INVERTER_IDENTIFIER, BATTERY_IDENTIFIER}

    # Dongle and meter endpoint failures preserve their previous cache. An
    # empty cache is therefore not sufficient proof that the device vanished.
    if data[DONGLE_IDENTIFIER]:
        authoritative_device_types.add(DONGLE_IDENTIFIER)
    if data[METER_IDENTIFIER]:
        authoritative_device_types.add(METER_IDENTIFIER)

    all_current_identifiers = set().union(*current_identifiers.values())
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        solplanet_identifiers = {identifier for identifier in device.identifiers if identifier[0] == DOMAIN}
        if not solplanet_identifiers:
            continue
        if any(
            _device_type_from_identifier(identifier) not in authoritative_device_types
            for identifier in solplanet_identifiers
        ):
            continue
        if not solplanet_identifiers.isdisjoint(all_current_identifiers):
            continue

        _LOGGER.debug("Removing stale Solplanet device %s", device.id)
        device_registry.async_remove_device(device.id)


@callback
def _register_devices(
    device_registry: dr.DeviceRegistry,
    entry: SolplanetConfigEntry,
) -> None:
    """Create or update device-registry entries from the metadata cache."""
    data = entry.runtime_data.data
    current_identifiers = _current_device_identifiers(entry)
    configured_mac = getattr(entry, "data", {}).get(CONF_MAC)
    first_dongle_id = next(iter(data[DONGLE_IDENTIFIER]), None)
    first_inverter_id = next(iter(data[INVERTER_IDENTIFIER]), None)

    for dongle_id, dongle_entry in data[DONGLE_IDENTIFIER].items():
        dongle = dongle_entry.get("data", {}) or {}
        connections = _network_mac_connections(
            dongle.get("ethmac"),
            dongle.get("wlanmac"),
            configured_mac if dongle_id == first_dongle_id else None,
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections=connections,
            identifiers={(DOMAIN, f"{DONGLE_IDENTIFIER}_{dongle_id}")},
            name=dongle.get("nam") or "Solplanet Dongle",
            manufacturer=dongle.get("brd") or dongle.get("muf") or MANUFACTURER,
            model=dongle.get("mod") or dongle.get("hw") or "Dongle",
            serial_number=dongle.get("psn") or dongle_id,
            hw_version=dongle.get("hw") or "",
            sw_version=dongle.get("sw") or "",
        )

    for inverter_id, inverter_entry in data[INVERTER_IDENTIFIER].items():
        inverter_info = inverter_entry["info"]
        connections = _network_mac_connections(
            configured_mac if first_dongle_id is None and inverter_id == first_inverter_id else None
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections=connections,
            identifiers={(DOMAIN, inverter_id)},
            name=inverter_info.model,
            model=inverter_info.model,
            manufacturer=MANUFACTURER,
            serial_number=inverter_info.isn,
            sw_version=(
                f"Master: {inverter_info.msw}, Slave: {inverter_info.ssw}, Security: {inverter_info.tsw}"
            ),
        )

    for battery_id, battery_entry in data[BATTERY_IDENTIFIER].items():
        battery_info = battery_entry.get("info")
        if battery_info is None:
            continue

        battery_serial = (
            battery_info.battery.partno
            if battery_info.battery and battery_info.battery.partno
            else battery_info.isn
        )
        battery_manufacturer = (
            BATTERY_MANUFACTURER_NAMES.get(battery_info.muf) if battery_info.muf is not None else None
        )
        battery_model = (
            BATTERY_MODEL_NAMES.get((battery_info.muf, battery_info.mod))
            if battery_info.muf is not None and battery_info.mod is not None
            else None
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{BATTERY_IDENTIFIER}_{battery_id}")},
            name=battery_model or "Battery",
            manufacturer=battery_manufacturer,
            model=battery_model,
            serial_number=battery_serial,
            sw_version=battery_info.battery.softwarever if battery_info.battery else "",
            hw_version=battery_info.battery.hardwarever if battery_info.battery else "",
        )

    for meter_id, meter_entry in data[METER_IDENTIFIER].items():
        if meter_entry.get("is_submeter"):
            model_name = meter_entry.get("model_name") or ""
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{METER_IDENTIFIER}_{meter_id}")},
                name=model_name or "Sub-meter",
                serial_number=meter_id,
                manufacturer=MANUFACTURER,
                model=model_name,
            )
            continue
        meter_info = meter_entry.get("info")
        app_info = meter_entry.get("app_info")

        if isinstance(app_info, dict):
            equip_model_raw = app_info.get("equipModel")
            equip_model = (
                int(equip_model_raw)
                if isinstance(equip_model_raw, int | str) and str(equip_model_raw).isdigit()
                else None
            )
            model_name = (
                METER_MODEL_NAMES.get(equip_model) if equip_model is not None and equip_model != 255 else None
            )
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{METER_IDENTIFIER}_{meter_id or ''}")},
                name=model_name or "Meter",
                serial_number=app_info.get("sn") or meter_id,
                manufacturer=MANUFACTURER,
                model=model_name or "",
            )
        elif meter_info is not None:
            model_name = meter_entry.get("model_name") or meter_info.name
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{METER_IDENTIFIER}_{meter_id or ''}")},
                name="Energy meter",
                serial_number=meter_info.sn,
                manufacturer=meter_info.manufactory,
                model=model_name,
            )

    _remove_stale_devices(device_registry, entry, current_identifiers)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow manual removal only when a device is absent from the inventory."""
    current_identifiers = set().union(*_current_device_identifiers(entry).values())
    solplanet_identifiers = {identifier for identifier in device.identifiers if identifier[0] == DOMAIN}
    return bool(solplanet_identifiers) and solplanet_identifiers.isdisjoint(current_identifiers)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Solplanet integration (services only).

    Config entries are set up in `async_setup_entry`.
    """
    hass.data.setdefault(DOMAIN, {})

    # Register services once for the integration domain.
    await async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolplanetConfigEntry) -> bool:
    """Set up Solplanet from a config entry."""
    client = SolplanetClient(entry.data[CONF_HOST], async_get_clientsession(hass))
    try:
        api = await SolplanetApiAdapter.create(client)
    except RuntimeError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="protocol_detection_failed",
            translation_placeholders={"error": error_detail(err)},
        ) from err

    _LOGGER.info("Using Solplanet protocol version: %s", api.version)

    device_registry = dr.async_get(hass)
    runtime = SolplanetRuntimeData(api)
    coordinator = SolplanetMetadataUpdateCoordinator(
        hass=hass,
        runtime=runtime,
        config_entry=entry,
    )
    runtime.metadata_coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()

    configured_interval = int(entry.data.get(CONF_INTERVAL, DEFAULT_INTERVAL))
    live_interval = timedelta(seconds=max(MIN_INTERVAL, min(configured_interval, MAX_INTERVAL)))

    runtime.inverter_coordinator = SolplanetInverterUpdateCoordinator(
        hass,
        runtime,
        entry,
        live_interval,
    )
    await runtime.inverter_coordinator.async_refresh()

    runtime.battery_coordinator = SolplanetBatteryUpdateCoordinator(
        hass,
        runtime,
        entry,
        live_interval,
    )
    await runtime.battery_coordinator.async_refresh()

    runtime.meter_coordinator = SolplanetMeterUpdateCoordinator(
        hass,
        runtime,
        entry,
        live_interval,
    )
    await runtime.meter_coordinator.async_refresh()

    runtime.dongle_coordinator = SolplanetDongleUpdateCoordinator(
        hass,
        runtime,
        entry,
        live_interval,
    )
    await runtime.dongle_coordinator.async_refresh()

    entry.runtime_data = runtime
    hass.data[DOMAIN][entry.entry_id] = runtime

    @callback
    def _async_register_devices() -> None:
        if not coordinator.last_update_success:
            return
        _register_devices(device_registry, entry)

    _async_register_devices()
    entry.async_on_unload(coordinator.async_add_listener(_async_register_devices))

    # Do not block setup if the inverter is sleeping or temporarily unreachable.
    # Entities are added regardless and will show `unknown` state until data is available.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolplanetConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: SolplanetConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    if config_entry.version > 1:
        # This means the user has downgraded from a future version
        return False

    if config_entry.version == 1 and config_entry.minor_version < 2:
        # 1.1 → 1.2: CONF_INTERVAL was added; inject the default for existing entries.
        new_data = {**config_entry.data, CONF_INTERVAL: DEFAULT_INTERVAL}
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=1, minor_version=2)
        _LOGGER.info("Entry %s migrated to version 1.2.", config_entry.entry_id)

    return True
