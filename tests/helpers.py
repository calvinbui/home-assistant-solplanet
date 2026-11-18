"""Small test doubles shared by the Solplanet platform tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.solplanet.client import GetInverterDataResponse, ScheduleSlot
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)


def integration_data(*, led_battery: bool = True) -> dict[str, dict]:
    """Return representative data for every supported device type."""
    inverter = GetInverterDataResponse(
        flg=1,
        err=0,
        fac=5000,
        pac=2400,
        vac=[2300, 2310, 2290],
        iac=[10, 11, 12],
        vpv=[3500, 3600],
        ipv=[210, 220],
        grid_sts=1,
    )
    battery_data = SimpleNamespace(
        cst=1,
        bst=2,
        eb1=0xFFFF,
        eb2=0xFFFF,
        eb3=0xFFFF,
        eb4=0xFFFF,
        wb1=0xFFFF,
        wb2=0xFFFF,
        wb3=0xFFFF,
        wb4=0xFFFF,
        soc=75,
        pb=500,
    )
    battery_info = SimpleNamespace(
        muf=5 if led_battery else 1,
        mod=12 if led_battery else 1,
        charge_max=90,
        discharge_max=15,
    )
    slot = ScheduleSlot(start_hour=1, start_minute=0, duration=1, mode="charge")
    return {
        INVERTER_IDENTIFIER: {
            "inv-1": {
                "data": inverter,
                "more_settings": {"power_on": True},
            }
        },
        BATTERY_IDENTIFIER: {
            "bat-1": {
                "data": battery_data,
                "info": battery_info,
                "schedule": {
                    "Pin": 1000,
                    "Pout": 900,
                    "raw": {"Mon": [1]},
                    "slots": {"Mon": [slot]},
                },
                "work_modes": {
                    "selected": SimpleNamespace(name="Self-consumption"),
                    "all": [
                        SimpleNamespace(name="Self-consumption"),
                        SimpleNamespace(name="Backup"),
                    ],
                },
                "more_settings": {
                    "power_on": True,
                    "sleep_enabled": False,
                    "led_color_index": 2,
                    "led_brightness": 50,
                },
            }
        },
        METER_IDENTIFIER: {
            "meter-1": {
                "app_data": {
                    "power": 300,
                    "uv": 230,
                    "ui": 1.3,
                    "up": 300,
                    "upf": 0.98,
                    "sac": 320,
                    "prc": 20,
                    "i_today": 2,
                    "o_today": 3,
                    "i_total": 20,
                    "o_total": 30,
                },
                "meter_req": {
                    "regulate": 10,
                    "ctrlType": 0,
                    "abs": 0,
                    "limitType": 0,
                    "target": 3000,
                    "powerDiff": -100,
                    "lostTime": 60,
                    "lostPowerMax": 0,
                },
            }
        },
        DONGLE_IDENTIFIER: {
            "dongle-1": {
                "data": {},
                "network": {
                    "mode": "station",
                    "sid": "Solar",
                    "srh": -50,
                    "ip": "192.0.2.2",
                    "gtw": "192.0.2.1",
                    "msk": "255.255.255.0",
                },
                "warnings": {},
            }
        },
    }


class FakeCoordinator:
    """Coordinator-shaped object used by platform and entity tests."""

    def __init__(self, data: dict[str, dict] | None = None) -> None:
        self.data = data or integration_data()
        self.runtime = self
        self.last_update_success = True
        self.failed_device_ids: set[str] = set()
        self.listeners: list = []

        for method in (
            "dongle_sync_time",
            "dongle_reboot",
            "set_battery_soc_max",
            "set_battery_soc_min",
            "set_battery_schedule_pin",
            "set_battery_schedule_pout",
            "set_battery_led_brightness",
            "set_battery_work_mode",
            "set_battery_led_color_index",
            "set_inverter_power",
            "set_battery_power",
            "set_battery_sleep_enabled",
            "set_battery_schedule_slots",
            "replace_battery_schedule_day",
            "set_meter_power_limit",
            "set_compatibility_meter_power_limit",
        ):
            setattr(self, method, AsyncMock())

    def coordinator_for(self, _device_type: str, _data_type: str) -> FakeCoordinator:
        """Return the endpoint coordinator (the double is both root and endpoint)."""
        return self

    def async_add_listener(self, listener, *args, **kwargs):
        """Register a coordinator listener."""
        self.listeners.append(listener)
        return lambda: None

    def get_max_inverter_rate_w(self) -> int:
        """Return a representative inverter power rating."""
        return 5000


class FakeEntry:
    """Minimal typed-config-entry surface used by platform setup."""

    def __init__(self, coordinator: FakeCoordinator, *, version: str = "v2") -> None:
        self.entry_id = "entry-1"
        self.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            api=SimpleNamespace(version=version),
            inverter_coordinator=coordinator,
        )
        self.unloads: list = []

    def async_on_unload(self, callback) -> None:
        """Record unload callbacks."""
        self.unloads.append(callback)
