"""Tests for automatic protocol detection and API delegation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solplanet.api_adapter import SolplanetApiAdapter
from custom_components.solplanet.client import (
    BatteryWorkMode,
    SolplanetApiV1,
    SolplanetApiV2,
)
from custom_components.solplanet.modbus import DataType


class _ProbeClient:
    """Minimal client that succeeds only for a selected probe."""

    def __init__(self, success: tuple[str, int, str] | None) -> None:
        self.success = success
        self.scheme = "http"
        self.port = 8484
        self.attempts: list[tuple[str, int, str]] = []

    async def get(self, endpoint: str) -> dict:
        attempt = (self.scheme, self.port, endpoint)
        self.attempts.append(attempt)
        if attempt == self.success:
            return {}
        raise ConnectionError("unavailable")


@pytest.mark.parametrize(
    ("success", "version", "attempts"),
    [
        (
            ("https", 443, "getdev.cgi?device=2"),
            "v2",
            [("https", 443, "getdev.cgi?device=2")],
        ),
        (
            ("http", 8484, "getdev.cgi?device=2"),
            "v2",
            [
                ("https", 443, "getdev.cgi?device=2"),
                ("http", 8484, "getdev.cgi?device=2"),
            ],
        ),
        (
            ("http", 8484, "invinfo.cgi"),
            "v1",
            [
                ("https", 443, "getdev.cgi?device=2"),
                ("http", 8484, "getdev.cgi?device=2"),
                ("http", 8484, "invinfo.cgi"),
            ],
        ),
    ],
)
async def test_create_detects_protocol(
    success: tuple[str, int, str],
    version: str,
    attempts: list[tuple[str, int, str]],
) -> None:
    """The adapter probes secure V2, plain V2, then V1 in order."""
    client = _ProbeClient(success)
    adapter = await SolplanetApiAdapter.create(client)  # type: ignore[arg-type]
    assert adapter.version == version
    assert client.attempts == attempts
    if version == "v2":
        assert isinstance(adapter._api, SolplanetApiV2)
    else:
        assert isinstance(adapter._api, SolplanetApiV1)


async def test_create_rejects_unknown_protocol() -> None:
    """Failure of every known probe produces a clear setup failure."""
    client = _ProbeClient(None)
    with pytest.raises(RuntimeError, match="Failed to detect any supported protocol"):
        await SolplanetApiAdapter.create(client)  # type: ignore[arg-type]
    assert client.attempts[-1] == ("http", 8484, "invinfo.cgi")


def _adapter(version: str) -> tuple[SolplanetApiAdapter, SimpleNamespace]:
    client = MagicMock()
    concrete = SolplanetApiV2(client) if version == "v2" else SolplanetApiV1(client)
    adapter = SolplanetApiAdapter(client, concrete)
    fake = SimpleNamespace()
    adapter._api = fake  # type: ignore[assignment]
    return adapter, fake


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_inverter_data", ("INV",)),
        ("get_inverter_info", ()),
        ("get_meter_data", ()),
        ("get_meter_info", ()),
    ],
)
@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_common_operations_delegate(
    version: str, method: str, args: tuple[object, ...]
) -> None:
    """Operations common to both protocols transparently return API results."""
    adapter, api = _adapter(version)
    mock = AsyncMock(return_value={"result": method})
    setattr(api, method, mock)
    assert await getattr(adapter, method)(*args) == {"result": method}
    mock.assert_awaited_once_with(*args)


async def test_indexed_meter_reads_delegate_only_to_v2() -> None:
    """The adapter routes indexed meter reads through the V2 API."""
    adapter, api = _adapter("v2")
    api.get_meter_data = AsyncMock(return_value={"power": 156})

    assert await adapter.get_meter_data(1) == {"power": 156}
    api.get_meter_data.assert_awaited_once_with(1)

    adapter, _api_v1 = _adapter("v1")
    with pytest.raises(NotImplementedError, match="not supported in V1"):
        await adapter.get_meter_data(1)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_battery_data", ("BAT",)),
        ("get_battery_info", ("BAT",)),
        ("set_battery_work_mode", ("BAT", BatteryWorkMode("Custom", 4, 1))),
        ("set_battery_soc_min", ("BAT", 10)),
        ("set_battery_soc_max", ("BAT", 90)),
        ("get_schedule", ()),
        ("get_grid_status", (7,)),
        ("set_schedule_power", (1000, 2000)),
        ("set_schedule_pin", (1000,)),
        ("set_schedule_pout", (2000,)),
        ("set_schedule_slots", ({"Mon": [1]},)),
    ],
)
async def test_v2_battery_operations_delegate(
    method: str, args: tuple[object, ...]
) -> None:
    """Every V2-only operation is delegated with its original arguments."""
    adapter, api = _adapter("v2")
    result = {"result": method}
    mock = AsyncMock(return_value=result)
    setattr(api, method, mock)
    returned = await getattr(adapter, method)(*args)
    returning_methods = {"get_battery_data", "get_battery_info", "get_schedule", "get_grid_status"}
    assert returned == (result if method in returning_methods else None)
    mock.assert_awaited_once_with(*args)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_battery_data", ("BAT",)),
        ("get_battery_info", ("BAT",)),
        ("set_battery_work_mode", ("BAT", BatteryWorkMode("Custom", 4, 1))),
        ("set_battery_soc_min", ("BAT", 10)),
        ("set_battery_soc_max", ("BAT", 90)),
        ("get_schedule", ()),
        ("get_grid_status", (7,)),
        ("set_schedule_power", (1000, 2000)),
        ("set_schedule_pin", (1000,)),
        ("set_schedule_pout", (2000,)),
        ("set_schedule_slots", ({"Mon": [1]},)),
    ],
)
async def test_v1_rejects_battery_operations(
    method: str, args: tuple[object, ...]
) -> None:
    """V1 consistently rejects operations its firmware cannot provide."""
    adapter, _ = _adapter("v1")
    with pytest.raises(NotImplementedError, match="not supported in V1"):
        await getattr(adapter, method)(*args)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("modbus_read_holding_registers", (DataType.U16, 3, 40201, 2)),
        ("modbus_write_single_holding_register", (DataType.S16, 3, 40201, -2, True)),
        ("modbus_read_input_registers", (DataType.U32, 3, 30001, 2)),
    ],
)
async def test_modbus_positional_delegation(
    method: str, args: tuple[object, ...]
) -> None:
    """Modbus read and single-write calls preserve positional parameters."""
    adapter, api = _adapter("v2")
    mock = AsyncMock(return_value=123)
    setattr(api, method, mock)
    assert await getattr(adapter, method)(*args) == 123
    mock.assert_awaited_once_with(*args)


async def test_modbus_multiple_write_uses_named_parameters() -> None:
    """Multiple writes preserve the adapter's explicit argument names."""
    adapter, api = _adapter("v2")
    api.modbus_write_multiple_holding_registers = AsyncMock(return_value="frame")
    assert await adapter.modbus_write_multiple_holding_registers(3, 40201, [1, 2], True) == "frame"
    api.modbus_write_multiple_holding_registers.assert_awaited_once_with(
        device_address=3,
        register_address=40201,
        values=[1, 2],
        dry_run=True,
    )
