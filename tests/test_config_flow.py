"""Tests for the Solplanet config and options flows."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    get_schema_suggested_value,
)

import custom_components.solplanet as integration
from custom_components.solplanet.config_flow import (
    CannotConnect,
    STEP_USER_DATA_SCHEMA,
    SolplanetConfigFlow,
    SolplanetOptionsFlow,
    validate_input,
)
from custom_components.solplanet.const import (
    CONF_INTERVAL,
    CONF_MODBUS_TELEMETRY_FALLBACKS,
    DEFAULT_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
)

from tests.helpers import FakeCoordinator, integration_data


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Enable loading this repository's custom integration."""
    yield


@pytest.fixture
def user_input() -> dict:
    """Return representative form input."""
    return {CONF_HOST: "inverter.local", CONF_INTERVAL: 60}


def _entry(
    *,
    host: str = "old.local",
    interval: int = 60,
    unique_id: str = "dongle-serial",
    title: str | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: host, CONF_INTERVAL: interval},
        title=title or host,
        unique_id=unique_id,
    )


def _dhcp_info(
    *,
    ip: str = "192.0.2.20",
    mac: str = "c0482f200001",
) -> DhcpServiceInfo:
    """Return representative Solplanet DHCP discovery data."""
    return DhcpServiceInfo(ip=ip, hostname="aiswei-device", macaddress=mac)


def test_interval_schema_accepts_defaults_and_supported_bounds() -> None:
    """The polling interval is coerced and constrained."""
    assert STEP_USER_DATA_SCHEMA({CONF_HOST: "inverter.local"})[CONF_INTERVAL] == 60
    assert (
        STEP_USER_DATA_SCHEMA({CONF_HOST: "inverter.local", CONF_INTERVAL: "10"})[
            CONF_INTERVAL
        ]
        == 10
    )
    assert (
        STEP_USER_DATA_SCHEMA({CONF_HOST: "inverter.local", CONF_INTERVAL: 3600})[
            CONF_INTERVAL
        ]
        == 3600
    )


@pytest.mark.parametrize("interval", [9, 3601, "not-a-number"])
def test_interval_schema_rejects_unsupported_values(interval) -> None:
    """Values outside the supported polling range are rejected."""
    with pytest.raises(vol.Invalid):
        STEP_USER_DATA_SCHEMA({CONF_HOST: "inverter.local", CONF_INTERVAL: interval})


async def test_validate_input_v2_prefers_dongle_identity(hass, user_input) -> None:
    """V2 configuration uses a stable dongle identity and the entered host as title."""
    api = SimpleNamespace(
        version="v2",
        client=SimpleNamespace(get=AsyncMock(return_value={"psn": "PSN-1"})),
        get_inverter_info=AsyncMock(
            return_value=SimpleNamespace(inv=[SimpleNamespace(isn="INV-1")])
        ),
    )
    with (
        patch("custom_components.solplanet.config_flow.SolplanetClient"),
        patch(
            "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
            AsyncMock(return_value=api),
        ),
    ):
        assert await validate_input(hass, user_input) == {
            "title": "inverter.local",
            "unique_id": "PSN-1",
            "mac_addresses": set(),
            "inverter_count": 1,
        }


@pytest.mark.parametrize(
    ("identity", "expected", "expected_mac"),
    [
        (
            {"ethmac": "AA:BB:CC:DD:EE:FF"},
            "AA:BB:CC:DD:EE:FF",
            "aa:bb:cc:dd:ee:ff",
        ),
        (
            {"wlanmac": "CCDDEEFF0011"},
            "CCDDEEFF0011",
            "cc:dd:ee:ff:00:11",
        ),
    ],
)
async def test_validate_input_v2_identity_fallbacks(
    hass, user_input, identity, expected, expected_mac
) -> None:
    """V2 uses either wired or wireless MAC when PSN is absent."""
    api = SimpleNamespace(
        version="v2",
        client=SimpleNamespace(get=AsyncMock(return_value=identity)),
        get_inverter_info=AsyncMock(return_value=SimpleNamespace(inv=[])),
    )
    with (
        patch("custom_components.solplanet.config_flow.SolplanetClient"),
        patch(
            "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
            AsyncMock(return_value=api),
        ),
    ):
        result = await validate_input(hass, user_input)
    assert result["unique_id"] == expected
    assert result["mac_addresses"] == {expected_mac}


async def test_validate_input_falls_back_to_inverter_or_host(hass, user_input) -> None:
    """An unavailable dongle lookup falls back through inverter serial to host."""
    api = SimpleNamespace(
        version="v2",
        client=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("offline"))),
        get_inverter_info=AsyncMock(
            return_value=SimpleNamespace(inv=[SimpleNamespace(isn="INV-1")])
        ),
    )
    with (
        patch("custom_components.solplanet.config_flow.SolplanetClient"),
        patch(
            "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
            AsyncMock(return_value=api),
        ),
    ):
        result = await validate_input(hass, user_input)
    assert result == {
        "title": "INV-1",
        "unique_id": "INV-1",
        "mac_addresses": set(),
        "inverter_count": 1,
    }

    api.version = "v1"
    api.get_inverter_info.return_value = SimpleNamespace(inv=[])
    with (
        patch("custom_components.solplanet.config_flow.SolplanetClient"),
        patch(
            "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
            AsyncMock(return_value=api),
        ),
    ):
        result = await validate_input(hass, user_input)
    assert result == {
        "title": "inverter.local",
        "unique_id": "inverter.local",
        "mac_addresses": set(),
        "inverter_count": 0,
    }


async def test_validate_input_wraps_connection_errors(hass, user_input) -> None:
    """Probe errors are exposed to the flow as CannotConnect."""
    with (
        patch("custom_components.solplanet.config_flow.SolplanetClient"),
        patch(
            "custom_components.solplanet.config_flow.SolplanetApiAdapter.create",
            AsyncMock(side_effect=RuntimeError("offline")),
        ),
        pytest.raises(CannotConnect),
    ):
        await validate_input(hass, user_input)


async def test_user_flow_success_and_duplicate_update(hass, user_input) -> None:
    """The user flow creates one entry and updates the host on rediscovery."""
    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(return_value={"title": "inverter.local", "unique_id": "PSN-1"}),
    ):
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert form["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "inverter.local"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "PSN-1"

    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(return_value={"title": "new.local", "unique_id": "PSN-1"}),
    ):
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_HOST: "new.local", CONF_INTERVAL: 120},
        )
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "new.local"


@pytest.mark.parametrize(
    ("error", "reason"),
    [(CannotConnect(), "cannot_connect"), (RuntimeError("surprise"), "unknown")],
)
async def test_user_flow_reports_validation_errors(
    hass, user_input, error, reason
) -> None:
    """Expected and unexpected validation errors return the appropriate form error."""
    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=user_input,
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}


def test_manifest_enables_verified_dhcp_discovery_and_registered_updates() -> None:
    """The manifest supports current AISWEI hardware and configured MACs."""
    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components"
            / "solplanet"
            / "manifest.json"
        ).read_text()
    )
    assert manifest["dhcp"] == [
        {"macaddress": "C0482F2*"},
        {"registered_devices": True},
    ]


async def test_dhcp_discovery_requires_confirmation_and_creates_entry(hass) -> None:
    """A verified unconfigured DHCP device is offered for confirmation."""
    discovery_info = _dhcp_info()
    normalized_mac = "c0:48:2f:20:00:01"

    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(
            return_value={
                "title": discovery_info.ip,
                "unique_id": "PSN-1",
                "mac_addresses": {normalized_mac},
                "inverter_count": 1,
            }
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=discovery_info,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"
    assert result["description_placeholders"] == {"host": discovery_info.ip}

    with patch.object(
        integration,
        "async_setup_entry",
        AsyncMock(return_value=True),
    ):
        created = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["title"] == discovery_info.ip
    assert created["data"] == {
        CONF_HOST: discovery_info.ip,
        CONF_INTERVAL: DEFAULT_INTERVAL,
        CONF_MAC: normalized_mac,
    }
    assert created["result"].unique_id == "PSN-1"


@pytest.mark.parametrize(
    ("configured_host", "configured_title", "expected_host", "expected_title"),
    [
        ("192.0.2.10", "192.0.2.10", "192.0.2.20", "192.0.2.20"),
        ("192.0.2.10", "Roof inverter", "192.0.2.20", "Roof inverter"),
        ("inverter.local", "inverter.local", "inverter.local", "inverter.local"),
    ],
)
async def test_dhcp_updates_only_literal_ip_hosts_for_known_device(
    hass,
    configured_host: str,
    configured_title: str,
    expected_host: str,
    expected_title: str,
) -> None:
    """Rediscovery updates IPs while preserving hostnames and custom titles."""
    entry = _entry(
        host=configured_host,
        interval=90,
        unique_id="PSN-1",
        title=configured_title,
    )
    entry.add_to_hass(hass)
    discovery_info = _dhcp_info()
    normalized_mac = "c0:48:2f:20:00:01"

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(
                return_value={
                    "title": discovery_info.ip,
                    "unique_id": "PSN-1",
                    "mac_addresses": {normalized_mac},
                    "inverter_count": 1,
                }
            ),
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            Mock(),
        ) as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=discovery_info,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == {
        CONF_HOST: expected_host,
        CONF_INTERVAL: 90,
        CONF_MAC: normalized_mac,
    }
    assert entry.title == expected_title
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_dhcp_uses_registered_mac_to_upgrade_legacy_unique_id(hass) -> None:
    """An exact registry MAC safely links entries created with a legacy ID."""
    entry = _entry(host="192.0.2.10", interval=90, unique_id="192.0.2.10")
    entry.add_to_hass(hass)
    discovery_info = _dhcp_info()
    normalized_mac = "c0:48:2f:20:00:01"
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, normalized_mac)},
        identifiers={(DOMAIN, "INV-1")},
    )

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(
                return_value={
                    "title": discovery_info.ip,
                    "unique_id": "PSN-1",
                    "mac_addresses": set(),
                    "inverter_count": 1,
                }
            ),
        ),
        patch.object(hass.config_entries, "async_schedule_reload", Mock()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=discovery_info,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "PSN-1"
    assert entry.title == discovery_info.ip
    assert entry.data == {
        CONF_HOST: discovery_info.ip,
        CONF_INTERVAL: 90,
        CONF_MAC: normalized_mac,
    }


@pytest.mark.parametrize(
    ("validated", "reason"),
    [
        (
            {
                "title": "192.0.2.20",
                "unique_id": "PSN-1",
                "mac_addresses": {"00:11:22:33:44:55"},
                "inverter_count": 1,
            },
            "cannot_connect",
        ),
        (
            {
                "title": "192.0.2.20",
                "unique_id": "PSN-1",
                "mac_addresses": set(),
                "inverter_count": 0,
            },
            "cannot_connect",
        ),
        (
            {
                "title": "192.0.2.20",
                "unique_id": "",
                "mac_addresses": set(),
                "inverter_count": 1,
            },
            "cannot_connect",
        ),
    ],
)
async def test_dhcp_rejects_mismatched_or_non_inverter_devices(
    hass,
    validated: dict,
    reason: str,
) -> None:
    """The broad vendor matcher cannot configure an unverified product."""
    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(return_value=validated),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=_dhcp_info(),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CannotConnect(), "cannot_connect"),
        (RuntimeError("surprise"), "unknown"),
    ],
)
async def test_dhcp_aborts_when_probe_fails(hass, error: Exception, reason: str) -> None:
    """Failed DHCP probes do not change configuration."""
    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=_dhcp_info(),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_dhcp_rejects_placeholder_mac_before_probing(hass) -> None:
    """Placeholder interface MACs cannot identify a discovered device."""
    with patch(
        "custom_components.solplanet.config_flow.validate_input",
        AsyncMock(),
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=_dhcp_info(mac="000000000000"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    validate.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_options_flow_shows_current_value_and_updates_entry(hass, enabled: bool) -> None:
    """The options flow persists the selected interval and reloads the entry."""
    entry = _entry(interval=30)
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as reload:
        form = await hass.config_entries.options.async_init(entry.entry_id)
        assert form["type"] is FlowResultType.FORM
        assert form["data_schema"]({})[CONF_INTERVAL] == 30
        assert form["data_schema"]({})[CONF_MODBUS_TELEMETRY_FALLBACKS] is False
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {CONF_INTERVAL: 120, CONF_MODBUS_TELEMETRY_FALLBACKS: enabled}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_INTERVAL] == 120
    assert entry.data[CONF_MODBUS_TELEMETRY_FALLBACKS] is enabled
    flow = SolplanetOptionsFlow(entry)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["data_schema"]({})[CONF_MODBUS_TELEMETRY_FALLBACKS] is enabled
    reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize(
    ("title", "expected_title"),
    [
        (None, "new.local"),
        ("Roof inverter", "Roof inverter"),
    ],
)
async def test_reconfigure_validates_and_updates_existing_entry(
    hass,
    title: str | None,
    expected_title: str,
) -> None:
    """Reconfigure validates the host and preserves entry-only data."""
    entry = _entry(interval=45, unique_id="PSN-1", title=title)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_MAC: "c0:48:2f:20:00:01"},
    )
    new_data = {CONF_HOST: "new.local", CONF_INTERVAL: 90}

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(return_value={"title": "new.local", "unique_id": "PSN-1"}),
        ) as validate,
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            Mock(),
        ) as schedule_reload,
    ):
        form = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert form["type"] is FlowResultType.FORM
        assert (
            get_schema_suggested_value(form["data_schema"].schema, CONF_HOST)
            == "old.local"
        )
        assert (
            get_schema_suggested_value(form["data_schema"].schema, CONF_INTERVAL)
            == 45
        )
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], new_data
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        **new_data,
        CONF_MAC: "c0:48:2f:20:00:01",
    }
    assert entry.title == expected_title
    assert entry.unique_id == "PSN-1"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    validate.assert_awaited_once_with(hass, new_data)
    schedule_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize(
    ("first_result", "reason"),
    [
        (CannotConnect(), "cannot_connect"),
        (RuntimeError("surprise"), "unknown"),
        ({"title": "new.local", "unique_id": ""}, "cannot_connect"),
    ],
)
async def test_reconfigure_recovers_from_validation_errors(
    hass,
    first_result: object,
    reason: str,
) -> None:
    """A failed probe leaves the entry unchanged and can be retried."""
    entry = _entry(interval=45, unique_id="PSN-1")
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    new_data = {CONF_HOST: "new.local", CONF_INTERVAL: 90}

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(
                side_effect=[
                    first_result,
                    {"title": "new.local", "unique_id": "PSN-1"},
                ]
            ),
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            Mock(),
        ) as schedule_reload,
    ):
        form = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        failed = await hass.config_entries.flow.async_configure(
            form["flow_id"], new_data
        )

        assert failed["type"] is FlowResultType.FORM
        assert failed["errors"] == {"base": reason}
        assert (
            get_schema_suggested_value(failed["data_schema"].schema, CONF_HOST)
            == new_data[CONF_HOST]
        )
        assert (
            get_schema_suggested_value(failed["data_schema"].schema, CONF_INTERVAL)
            == new_data[CONF_INTERVAL]
        )
        assert entry.data == original_data
        schedule_reload.assert_not_called()

        result = await hass.config_entries.flow.async_configure(
            failed["flow_id"], new_data
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == new_data
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_reconfigure_upgrades_a_verified_legacy_identity(hass) -> None:
    """A registry-backed legacy host ID is safely replaced by the hardware ID."""
    entry = _entry(host="old.local", interval=45, unique_id="old.local")
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{DONGLE_IDENTIFIER}_PSN-1")},
    )
    new_data = {CONF_HOST: "new.local", CONF_INTERVAL: 90}

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(return_value={"title": "new.local", "unique_id": "PSN-1"}),
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            Mock(),
        ) as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
            data=new_data,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == new_data
    assert entry.unique_id == "PSN-1"
    schedule_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("entry_unique_id", ["PSN-1", "old.local"])
async def test_reconfigure_rejects_a_different_device(
    hass, entry_unique_id: str
) -> None:
    """Reconfigure cannot redirect an entry to another Solplanet device."""
    entry = _entry(host="old.local", interval=45, unique_id=entry_unique_id)
    other_entry = _entry(host="other.local", interval=60, unique_id="PSN-2")
    entry.add_to_hass(hass)
    other_entry.add_to_hass(hass)
    original_data = dict(entry.data)

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(return_value={"title": "other.local", "unique_id": "PSN-2"}),
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            Mock(),
        ) as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
            data={CONF_HOST: "other.local", CONF_INTERVAL: 90},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data == original_data
    assert other_entry.data == {CONF_HOST: "other.local", CONF_INTERVAL: 60}
    schedule_reload.assert_not_called()


async def test_reconfigure_does_not_duplicate_a_stable_identity(hass) -> None:
    """A verified legacy identity cannot replace an existing stable entry."""
    legacy_entry = _entry(host="old.local", interval=45, unique_id="old.local")
    stable_entry = _entry(host="other.local", interval=60, unique_id="PSN-2")
    legacy_entry.add_to_hass(hass)
    stable_entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=legacy_entry.entry_id,
        identifiers={(DOMAIN, f"{DONGLE_IDENTIFIER}_PSN-2")},
    )

    with (
        patch(
            "custom_components.solplanet.config_flow.validate_input",
            AsyncMock(return_value={"title": "other.local", "unique_id": "PSN-2"}),
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            Mock(),
        ) as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": legacy_entry.entry_id,
            },
            data={CONF_HOST: "other.local", CONF_INTERVAL: 90},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert legacy_entry.unique_id == "old.local"
    assert stable_entry.unique_id == "PSN-2"
    schedule_reload.assert_not_called()


def test_options_flow_factory() -> None:
    """The config flow returns its dedicated options handler."""
    entry = _entry()
    assert isinstance(
        SolplanetConfigFlow.async_get_options_flow(entry), SolplanetOptionsFlow
    )


async def test_config_entry_setup_registers_real_platform_entities(hass) -> None:
    """A real config entry forwards platforms and registers their entities in HA."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="inverter.local",
        data={CONF_HOST: "inverter.local", CONF_INTERVAL: 60},
        unique_id="dongle-1",
    )
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})

    legacy_registry_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "solplanet_inv-1_pac",
        suggested_object_id="legacy_inverter_power",
        config_entry=entry,
        has_entity_name=False,
        original_name="Power",
    )
    legacy_entity_id = legacy_registry_entry.entity_id
    legacy_phase_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "solplanet_inv-1_vac_0",
        suggested_object_id="legacy_phase_voltage",
        config_entry=entry,
        has_entity_name=False,
        original_name="AC phase 1 voltage",
    )
    legacy_phase_entity_id = legacy_phase_entry.entity_id

    def metadata_factory(*_args, **kwargs):
        runtime = kwargs["runtime"]
        runtime.data.update(integration_data())
        coordinator = FakeCoordinator(runtime.data)
        coordinator.runtime = runtime
        coordinator.async_config_entry_first_refresh = AsyncMock()
        return coordinator

    def live_factory(_hass, runtime, _entry, _interval):
        coordinator = FakeCoordinator(runtime.data)
        coordinator.runtime = runtime
        coordinator.async_refresh = AsyncMock()
        return coordinator

    with (
        patch.object(integration, "async_get_clientsession", return_value="session"),
        patch.object(integration, "SolplanetClient"),
        patch.object(
            integration.SolplanetApiAdapter,
            "create",
            AsyncMock(return_value=SimpleNamespace(version="v2")),
        ),
        patch.object(
            integration,
            "SolplanetMetadataUpdateCoordinator",
            side_effect=metadata_factory,
        ),
        patch.object(
            integration,
            "SolplanetInverterUpdateCoordinator",
            side_effect=live_factory,
        ),
        patch.object(
            integration,
            "SolplanetBatteryUpdateCoordinator",
            side_effect=live_factory,
        ),
        patch.object(
            integration,
            "SolplanetMeterUpdateCoordinator",
            side_effect=live_factory,
        ),
        patch.object(
            integration,
            "SolplanetDongleUpdateCoordinator",
            side_effect=live_factory,
        ),
        patch.object(integration, "_register_devices"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is config_entries.ConfigEntryState.LOADED

    upgraded_registry_entry = er.async_get(hass).async_get(legacy_entity_id)
    assert upgraded_registry_entry is not None
    assert upgraded_registry_entry.entity_id == legacy_entity_id
    assert upgraded_registry_entry.unique_id == "solplanet_inv-1_pac"
    assert upgraded_registry_entry.has_entity_name
    assert upgraded_registry_entry.translation_key == "power"

    upgraded_phase_entry = er.async_get(hass).async_get(legacy_phase_entity_id)
    assert upgraded_phase_entry is not None
    assert upgraded_phase_entry.entity_id == legacy_phase_entity_id
    assert upgraded_phase_entry.disabled_by is None
    assert upgraded_phase_entry.translation_key == "ac_phase_voltage"
    assert hass.states.get(legacy_phase_entity_id) is not None

    entities = [
        registry_entry
        for registry_entry in er.async_get(hass).entities.values()
        if registry_entry.config_entry_id == entry.entry_id
    ]
    assert len(entities) > 80
    assert {registry_entry.domain for registry_entry in entities} == {
        "binary_sensor",
        "button",
        "number",
        "select",
        "sensor",
        "switch",
    }
    assert all(registry_entry.has_entity_name for registry_entry in entities)
    assert all(registry_entry.translation_key for registry_entry in entities)
    assert all(registry_entry.original_name for registry_entry in entities)
    assert all("{" not in registry_entry.original_name for registry_entry in entities)

    enabled = [registry_entry for registry_entry in entities if registry_entry.disabled_by is None]
    disabled = [
        registry_entry
        for registry_entry in entities
        if registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    ]
    assert enabled
    assert disabled
    assert all(
        hass.states.get(registry_entry.entity_id) is not None
        for registry_entry in enabled
    )
    assert all(
        hass.states.get(registry_entry.entity_id) is None
        for registry_entry in disabled
    )
