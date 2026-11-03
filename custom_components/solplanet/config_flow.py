"""Config flow for Solplanet integration."""

from __future__ import annotations

from ipaddress import ip_address
import logging
from typing import Any, cast, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from . import SolplanetConfigEntry
from .api_adapter import SolplanetApiAdapter
from .client import SolplanetClient
from .const import (
    CONF_INTERVAL,
    CONF_MODBUS_TELEMETRY_FALLBACKS,
    DEFAULT_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    MAX_INTERVAL,
    MIN_INTERVAL,
)
from .validation import normalize_mac_address

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_INTERVAL, default=DEFAULT_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_INTERVAL, max=MAX_INTERVAL)
        ),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    try:
        client = SolplanetClient(data[CONF_HOST], async_get_clientsession(hass))
        api = await SolplanetApiAdapter.create(client)
        _LOGGER.info("Detected Solplanet protocol version: %s", api.version)
        inverter_info = await api.get_inverter_info()
    except Exception as err:
        _LOGGER.debug("Exception occurred during adding device", exc_info=err)
        raise CannotConnect from err

    # Prefer a stable hardware identifier for the config entry unique_id.
    # Using host/IP as the unique_id causes duplicate entries when the IP/DNS changes.
    unique_id: str | None = None
    mac_addresses: set[str] = set()
    # Keep the config entry title user-driven (host/hostname they entered).
    # Use dongle identity only for the backend unique_id.
    title = data[CONF_HOST]

    # V2 exposes dongle details at getdev.cgi (no device parameter), which includes psn/mac.
    if api.version == "v2":
        try:
            dongle = await api.client.get("getdev.cgi")
            ethmac = dongle.get("ethmac")
            wlanmac = dongle.get("wlanmac")
            normalized_ethmac = normalize_mac_address(ethmac)
            normalized_wlanmac = normalize_mac_address(wlanmac)
            unique_id = dongle.get("psn") or (
                ethmac
                if normalized_ethmac is not None
                else wlanmac
                if normalized_wlanmac is not None
                else None
            )
            mac_addresses = {
                mac_address
                for mac_address in (normalized_ethmac, normalized_wlanmac)
                if mac_address is not None
            }
            # Do not override the title with dongle metadata; keep the user-provided host.
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to read dongle identity: %s", err, exc_info=True)

    # Fallback: inverter serial
    if not unique_id:
        try:
            unique_id = inverter_info.inv[0].isn  # stable serial
            title = inverter_info.inv[0].isn or title
        except Exception:  # noqa: BLE001
            unique_id = data[CONF_HOST]

    return {
        "title": title,
        "unique_id": unique_id,
        "mac_addresses": mac_addresses,
        "inverter_count": len(inverter_info.inv),
    }


def _host_is_ip_address(host: Any) -> bool:
    """Return whether a configured host is a literal IP address."""
    try:
        ip_address(host)
    except (TypeError, ValueError):
        return False
    return True


class SolplanetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solplanet."""

    VERSION = 1
    MINOR_VERSION = 2

    _discovered_data: dict[str, Any]
    _discovered_title: str

    def _entry_for_mac(self, mac: str) -> SolplanetConfigEntry | None:
        """Return the configured Solplanet entry linked to a network MAC."""
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(
            connections={(dr.CONNECTION_NETWORK_MAC, mac)}
        )
        if device is None:
            return None
        return cast(
            SolplanetConfigEntry | None,
            next(
                (
                    entry
                    for entry_id in device.config_entries
                    if (entry := self.hass.config_entries.async_get_entry(entry_id))
                    is not None
                    and entry.domain == DOMAIN
                ),
                None,
            ),
        )

    def _entry_owns_identity(self, entry: SolplanetConfigEntry, unique_id: str) -> bool:
        """Return whether the device registry links an identity to an entry."""
        expected_identifiers = {
            (DOMAIN, unique_id),
            (DOMAIN, f"{DONGLE_IDENTIFIER}_{unique_id}"),
        }
        return any(
            not expected_identifiers.isdisjoint(device.identifiers)
            for device in dr.async_entries_for_config_entry(
                dr.async_get(self.hass), entry.entry_id
            )
        )

    @staticmethod
    @override
    def async_get_options_flow(
        config_entry: SolplanetConfigEntry,
    ) -> SolplanetOptionsFlow:
        """Get the options flow for this handler."""
        return SolplanetOptionsFlow(config_entry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of the integration."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during reconfiguration")
                errors["base"] = "unknown"
            else:
                unique_id = info["unique_id"]
                if not isinstance(unique_id, str) or not unique_id.strip():
                    errors["base"] = "cannot_connect"
                else:
                    configured_entry = await self.async_set_unique_id(unique_id)
                    legacy_identity_matches = (
                        entry.unique_id == entry.data[CONF_HOST]
                        and self._entry_owns_identity(entry, unique_id)
                        and configured_entry is None
                    )
                    if not legacy_identity_matches:
                        self._abort_if_unique_id_mismatch()
                    updated_title = (
                        user_input[CONF_HOST]
                        if entry.title == entry.data[CONF_HOST]
                        else entry.title
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=unique_id,
                        title=updated_title,
                        data_updates=user_input,
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA,
                user_input or entry.data,
            ),
            errors=errors,
        )

    @override
    async def async_step_dhcp(
        self,
        discovery_info: DhcpServiceInfo,
    ) -> ConfigFlowResult:
        """Handle DHCP discovery and update a configured device's address."""
        host = discovery_info.ip
        if (mac := normalize_mac_address(discovery_info.macaddress)) is None:
            return self.async_abort(reason="cannot_connect")
        data = {CONF_HOST: host, CONF_INTERVAL: DEFAULT_INTERVAL}

        try:
            info = await validate_input(self.hass, data)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected exception during DHCP discovery")
            return self.async_abort(reason="unknown")

        unique_id = info["unique_id"]
        if (
            not isinstance(unique_id, str)
            or not unique_id.strip()
            or not info["inverter_count"]
            or (
                info["mac_addresses"]
                and mac not in info["mac_addresses"]
            )
        ):
            return self.async_abort(reason="cannot_connect")

        existing_entry = await self.async_set_unique_id(unique_id)
        existing_entry = existing_entry or self._entry_for_mac(mac)
        if existing_entry is not None:
            updates = {CONF_MAC: mac}
            existing_host = existing_entry.data.get(CONF_HOST)
            updated_title = existing_entry.title
            if _host_is_ip_address(existing_host):
                updates[CONF_HOST] = host
                if existing_entry.title == existing_host:
                    updated_title = host
            return self.async_update_reload_and_abort(
                existing_entry,
                unique_id=unique_id,
                title=updated_title,
                data_updates=updates,
                reason="already_configured",
                reload_even_if_entry_is_unchanged=False,
            )

        self._discovered_data = {**data, CONF_MAC: mac}
        self._discovered_title = info["title"]
        self.context["title_placeholders"] = {"name": self._discovered_title}
        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm a DHCP-discovered Solplanet device."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_title,
                data=self._discovered_data,
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="dhcp_confirm",
            description_placeholders={"host": self._discovered_data[CONF_HOST]},
        )

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Use stable hardware identity so changing IP/DNS doesn't create duplicates.
                await self.async_set_unique_id(info["unique_id"])
                # If already configured, update the host and abort instead of creating a new entry.
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)


class SolplanetOptionsFlow(OptionsFlow):
    """Handle options flow for Solplanet."""

    def __init__(self, config_entry: SolplanetConfigEntry) -> None:
        """Initialize options flow."""
        # HA exposes OptionsFlow.config_entry as a read-only property in newer versions.
        # Store the entry on our own attribute.
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Update the config entry data with new interval
            new_data = {
                **self._config_entry.data,
                CONF_INTERVAL: user_input[CONF_INTERVAL],
                CONF_MODBUS_TELEMETRY_FALLBACKS: user_input.get(CONF_MODBUS_TELEMETRY_FALLBACKS, False),
            }
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self._config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        # Show form with current interval value
        current_interval = self._config_entry.data.get(CONF_INTERVAL, DEFAULT_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MODBUS_TELEMETRY_FALLBACKS,
                    default=self._config_entry.data.get(CONF_MODBUS_TELEMETRY_FALLBACKS, False),
                ): bool,
                vol.Required(CONF_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_INTERVAL, max=MAX_INTERVAL)
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
