"""Tests for Solplanet diagnostics."""

from __future__ import annotations

from datetime import timedelta
import json
from types import SimpleNamespace

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solplanet.client import (
    BatteryWorkMode,
    GetBatteryDataResponse,
    GetBatteryInfoItemResponse,
    GetBatteryInfoResponse,
    GetInverterDataResponse,
    GetInverterInfoItemResponse,
    GetMeterDataResponse,
    GetMeterInfoResponse,
    ScheduleSlot,
)
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    CONF_INTERVAL,
    DOMAIN,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)
from custom_components.solplanet.coordinator import SolplanetRuntimeData
from custom_components.solplanet.diagnostics import async_get_config_entry_diagnostics


def _coordinator(
    *,
    successful: bool,
    interval: timedelta | None,
    failed_device_ids: set[str] | None = None,
) -> SimpleNamespace:
    """Return the coordinator state exposed by diagnostics."""
    return SimpleNamespace(
        last_update_success=successful,
        update_interval=interval,
        failed_device_ids=failed_device_ids or set(),
    )


async def test_config_entry_diagnostics_are_useful_and_redacted(hass) -> None:
    """Diagnostics expose runtime state without leaking network or device identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="inverter.private.example",
        unique_id="DONGLE-SERIAL",
        data={CONF_HOST: "inverter.private.example", CONF_INTERVAL: 60},
    )
    entry.add_to_hass(hass)

    runtime = SolplanetRuntimeData(api=SimpleNamespace(version="v2"))
    runtime.data = {
        DONGLE_IDENTIFIER: {
            "DONGLE-SERIAL": {
                "data": {
                    "nam": "Roof gateway",
                    "brd": "Solplanet",
                    "mod": "WiFi Stick",
                    "psn": "DONGLE-SERIAL",
                    "ethmac": "AA:BB:CC:DD:EE:FF",
                    "hw": "H1",
                    "sw": "S1",
                },
                "network": {
                    "mode": "station",
                    "sid": "Private WiFi",
                    "ip": "192.0.2.2",
                    "gtw": "192.0.2.1",
                    "msk": "255.255.255.0",
                    "srh": -51,
                },
                "warnings": {"code": 7},
            }
        },
        INVERTER_IDENTIFIER: {
            "INV-SERIAL": {
                "info": GetInverterInfoItemResponse(
                    isn="INV-SERIAL",
                    model="ASW5000",
                    rate=5000,
                    msw="M1",
                    ssw="S1",
                    tsw="T1",
                ),
                "data": GetInverterDataResponse(pac=2400, grid_sts=1),
                "more_settings": {"power_on": True},
            }
        },
        BATTERY_IDENTIFIER: {
            "BAT-SERIAL": {
                "info": GetBatteryInfoResponse(
                    type=1,
                    mod_r=2,
                    isn="BAT-SERIAL",
                    muf=5,
                    mod=12,
                    num=2,
                    battery=GetBatteryInfoItemResponse(
                        partno="BAT-PART-NUMBER",
                        model1sn="BAT-MODULE-SERIAL",
                        modeltotal=4,
                        hardwarever="H2",
                        softwarever="S2",
                    ),
                ),
                "data": GetBatteryDataResponse(soc=75, pb=500),
                "work_modes": {
                    "selected": BatteryWorkMode("Self-consumption mode", 2, 1),
                },
                "schedule": {
                    "slots": {
                        "Mon": [ScheduleSlot(1, 30, 1, "charge")],
                    }
                },
            }
        },
        METER_IDENTIFIER: {
            "METER-SERIAL": {
                "info": GetMeterInfoResponse(
                    sn="METER-SERIAL",
                    manufactory="Eastron",
                    name="SDM630",
                ),
                "data": GetMeterDataResponse(pac=300),
            }
        },
    }
    runtime.metadata_coordinator = _coordinator(
        successful=True,
        interval=timedelta(hours=1),
    )
    runtime.inverter_coordinator = _coordinator(
        successful=False,
        interval=timedelta(seconds=60),
        failed_device_ids={"INV-SERIAL"},
    )
    runtime.battery_coordinator = _coordinator(successful=True, interval=None)
    runtime.meter_coordinator = _coordinator(
        successful=True,
        interval=timedelta(seconds=60),
    )
    runtime.dongle_coordinator = None
    entry.runtime_data = runtime

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["protocol_version"] == "v2"
    assert diagnostics["supported_device_types"] == [
        DONGLE_IDENTIFIER,
        INVERTER_IDENTIFIER,
        BATTERY_IDENTIFIER,
        METER_IDENTIFIER,
    ]
    assert diagnostics["device_counts"] == {
        DONGLE_IDENTIFIER: 1,
        INVERTER_IDENTIFIER: 1,
        BATTERY_IDENTIFIER: 1,
        METER_IDENTIFIER: 1,
    }
    assert diagnostics["config_entry"]["title"] == REDACTED
    assert diagnostics["config_entry"]["unique_id"] == REDACTED
    assert diagnostics["config_entry"]["data"][CONF_HOST] == REDACTED
    assert diagnostics["coordinators"] == {
        "metadata": {
            "initialized": True,
            "last_update_success": True,
            "update_interval_seconds": 3600.0,
            "failed_device_count": 0,
        },
        "inverter": {
            "initialized": True,
            "last_update_success": False,
            "update_interval_seconds": 60.0,
            "failed_device_count": 1,
        },
        "battery": {
            "initialized": True,
            "last_update_success": True,
            "update_interval_seconds": None,
            "failed_device_count": 0,
        },
        "meter": {
            "initialized": True,
            "last_update_success": True,
            "update_interval_seconds": 60.0,
            "failed_device_count": 0,
        },
        "dongle": {"initialized": False},
    }

    inverter = diagnostics["devices"][INVERTER_IDENTIFIER][0]
    assert inverter["device_id"] == REDACTED
    assert inverter["serial_number"] == REDACTED
    assert inverter["model"] == "ASW5000"
    assert inverter["rated_power_w"] == 5000
    assert inverter["data_available"] is True

    dongle = diagnostics["devices"][DONGLE_IDENTIFIER][0]
    assert dongle["manufacturer"] == "Solplanet"
    assert dongle["model"] == "WiFi Stick"
    assert dongle["network_mode"] == "station"
    assert dongle["signal_strength"] == -51
    assert dongle["warnings_available"] is True
    assert dongle["warnings_present"] is True

    battery = diagnostics["devices"][BATTERY_IDENTIFIER][0]
    assert battery["manufacturer_code"] == 5
    assert battery["model_code"] == 12
    assert battery["battery_count"] == 2
    assert battery["module_count"] == 4
    assert battery["hardware_version"] == "H2"
    assert battery["schedule_available"] is True

    meter = diagnostics["devices"][METER_IDENTIFIER][0]
    assert meter["manufacturer"] == "Eastron"
    assert meter["model"] == "SDM630"
    assert meter["data_available"] is True

    encoded = json.dumps(diagnostics, sort_keys=True)
    for sensitive_value in (
        "inverter.private.example",
        "DONGLE-SERIAL",
        "INV-SERIAL",
        "BAT-SERIAL",
        "BAT-PART-NUMBER",
        "BAT-MODULE-SERIAL",
        "METER-SERIAL",
        "Roof gateway",
        "Private WiFi",
        "192.0.2.2",
        "192.0.2.1",
        "255.255.255.0",
        "AA:BB:CC:DD:EE:FF",
    ):
        assert sensitive_value not in encoded

    runtime.api.version = "v1"
    v1_diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert v1_diagnostics["supported_device_types"] == [
        INVERTER_IDENTIFIER,
        METER_IDENTIFIER,
    ]
