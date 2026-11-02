"""Tests for HTTP transport, protocol clients, and battery helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.solplanet.client import (
    BatterySchedule,
    BatteryWorkMode,
    BatteryWorkModes,
    GetBatteryInfoItemResponse,
    GetBatteryInfoResponse,
    GetInverterDataResponse,
    GetInverterInfoItemResponse,
    GetInverterInfoResponse,
    GetMeterDataResponse,
    GetMeterInfoResponse,
    ModbusApiMixin,
    ScheduleSlot,
    SetBatteryConfigRequest,
    SetScheduleRequest,
    SolplanetApi,
    SolplanetApiV1,
    SolplanetApiV2,
    SolplanetClient,
)
from custom_components.solplanet.modbus import DataType, ModbusRtuFrameGenerator


class _Response:
    """Small response double implementing what the transport consumes."""

    def __init__(self, body: bytes = b"{}", *, status: int = 200, url: str = "") -> None:
        self.body = body
        self.status = status
        self.request_info = SimpleNamespace(url=url)
        self.raw_headers = ((b"Content-Type", b"application/json"),)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(real_url=self.request_info.url),
                history=(),
                status=self.status,
            )

    async def read(self) -> bytes:
        return self.body

    def get_encoding(self) -> str:
        return "utf-8"


class _RequestContext:
    def __init__(self, result: _Response | Exception) -> None:
        self.result = result

    async def __aenter__(self) -> _Response:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def __aexit__(self, *args: object) -> None:
        return None


class _Session:
    """Queue-based session double that records request options."""

    def __init__(self, *results: _Response | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, dict]] = []

    def _request(self, method: str, url: str, kwargs: dict) -> _RequestContext:
        self.calls.append((method, url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, _Response):
            result.request_info.url = url
        return _RequestContext(result)

    def get(self, url: str, **kwargs: object) -> _RequestContext:
        return self._request("GET", url, kwargs)

    def post(self, url: str, **kwargs: object) -> _RequestContext:
        return self._request("POST", url, kwargs)


def _crc_frame(body: bytes) -> str:
    generator = ModbusRtuFrameGenerator()
    return (body + struct.pack("<H", generator._calculate_crc(body))).hex()


async def test_http_get_parses_json_and_logs_debug(caplog) -> None:
    """GET builds the dongle URL and decodes permissive JSON."""
    caplog.set_level(logging.DEBUG, logger="custom_components.solplanet.client")
    url = "http://1.2.3.4:8484/invinfo.cgi"
    session = _Session(_Response(b'{"num": 1, "label": "line\\nfeed"}'))
    client = SolplanetClient("1.2.3.4", session, request_timeout=2.5)  # type: ignore[arg-type]
    result = await client.get("invinfo.cgi")

    assert result == {"num": 1, "label": "line\nfeed"}
    assert client.get_url("invinfo.cgi") == url
    assert "Received from" in caplog.text
    assert session.calls[0][2]["timeout"].total == 2.5


async def test_https_disables_certificate_verification() -> None:
    """Self-signed HTTPS dongles are called with SSL verification disabled."""
    url = "https://inverter.local:443/getdev.cgi"
    session = _Session(_Response(b'{"ok": true}'))
    client = SolplanetClient(
        "inverter.local", session, scheme="https", port=443  # type: ignore[arg-type]
    )
    assert await client.get("getdev.cgi") == {"ok": True}
    assert session.calls == [
        (
            "GET",
            url,
            {"ssl": False, "timeout": session.calls[0][2]["timeout"]},
        )
    ]


async def test_http_post_serializes_dict() -> None:
    """POST sends ordinary mappings as JSON."""
    url = "http://1.2.3.4:8484/fdbg.cgi"
    session = _Session(_Response(b'{"data": "reply"}'))
    client = SolplanetClient("1.2.3.4", session)  # type: ignore[arg-type]
    assert await client.post("fdbg.cgi", {"data": "frame"}) == {"data": "reply"}
    assert session.calls[0][0:2] == ("POST", url)
    assert session.calls[0][2]["json"] == {"data": "frame"}


async def test_http_post_serializes_dataclass() -> None:
    """Request dataclasses are recursively converted before transport."""
    @dataclass
    class Payload:
        action: str
        value: int

    url = "http://1.2.3.4:8484/setting.cgi"
    session = _Session(_Response(b'{"dat": "ok"}'))
    client = SolplanetClient("1.2.3.4", session)  # type: ignore[arg-type]
    assert await client.post("setting.cgi", Payload("set", 42)) == {"dat": "ok"}
    assert session.calls[0][0:2] == ("POST", url)
    assert session.calls[0][2]["json"] == {"action": "set", "value": 42}


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.TimeoutError(),
        aiohttp.ClientConnectionError("connection reset"),
    ],
)
async def test_http_retries_transient_failures(failure: Exception) -> None:
    """Timeouts and transport errors receive the configured retry."""
    url = "http://1.2.3.4:8484/invinfo.cgi"
    session = _Session(failure, _Response(b'{"ok": true}'))
    client = SolplanetClient("1.2.3.4", session, request_retries=1)  # type: ignore[arg-type]
    with patch("custom_components.solplanet.client.asyncio.sleep", new=AsyncMock()) as sleep:
        assert await client.get("invinfo.cgi") == {"ok": True}
    sleep.assert_awaited_once_with(0.25)
    assert [call[:2] for call in session.calls] == [("GET", url), ("GET", url)]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("timeout", asyncio.TimeoutError),
        ("status", aiohttp.ClientResponseError),
        ("json", json.JSONDecodeError),
    ],
)
async def test_http_exhausts_retries(
    kind: str, expected: type[Exception]
) -> None:
    """The final transport, HTTP, or JSON error is preserved for callers."""
    url = "http://1.2.3.4:8484/invinfo.cgi"
    if kind == "timeout":
        results: tuple[_Response | Exception, ...] = (
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        )
    elif kind == "status":
        results = (_Response(status=503), _Response(status=503))
    else:
        results = (_Response(b"not-json"), _Response(b"not-json"))
    session = _Session(*results)
    client = SolplanetClient("1.2.3.4", session, request_retries=1)  # type: ignore[arg-type]
    with patch("custom_components.solplanet.client.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(expected):
            await client.get("invinfo.cgi")


async def test_http_rejects_unsupported_method() -> None:
    """Only GET and POST are accepted by the internal transport."""
    client = SolplanetClient("1.2.3.4", _Session())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Unsupported method: DELETE"):
        await client._request("DELETE", "endpoint")


async def test_http_rejects_negative_retry_budget() -> None:
    """A negative retry budget cannot execute a request."""
    client = SolplanetClient(
        "1.2.3.4", _Session(), request_retries=-1  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="HTTP request failed: None"):
        await client.get("endpoint")


class _ModbusApi(ModbusApiMixin):
    def __init__(self, client: MagicMock) -> None:
        self.client = client


async def test_modbus_read_methods_build_frames() -> None:
    """Holding and input reads build their distinct Modbus functions."""
    api = _ModbusApi(MagicMock())
    api._send_modbus = AsyncMock(side_effect=[11, 22])  # type: ignore[method-assign]
    assert await api.modbus_read_holding_registers(DataType.U16, 3, 40201, 2) == 11
    assert await api.modbus_read_input_registers(DataType.U32, 4, 30005, 2) == 22
    holding_frame = api._send_modbus.await_args_list[0].kwargs["frame"]
    input_frame = api._send_modbus.await_args_list[1].kwargs["frame"]
    assert bytes.fromhex(holding_frame)[1:6] == b"\x03\x00\xc8\x00\x02"
    assert bytes.fromhex(input_frame)[1:6] == b"\x04\x00\x04\x00\x02"


async def test_modbus_single_write_dry_run_and_send() -> None:
    """Single writes return frames in dry-run mode and otherwise send them."""
    api = _ModbusApi(MagicMock())
    api._send_modbus = AsyncMock(return_value={"data": 1})  # type: ignore[method-assign]
    frame = await api.modbus_write_single_holding_register(
        DataType.S16, 3, 40201, -2, dry_run=True
    )
    assert isinstance(frame, str)
    api._send_modbus.assert_not_awaited()
    assert await api.modbus_write_single_holding_register(
        DataType.S16, 3, 40201, -2
    ) == {"data": 1}
    api._send_modbus.assert_awaited_once_with(frame=frame, data_type=DataType.S16)


async def test_modbus_multiple_write_dry_run_and_send() -> None:
    """Multiple writes use U16 to decode the acknowledgement."""
    api = _ModbusApi(MagicMock())
    api._send_modbus = AsyncMock(return_value={"quantity": 2})  # type: ignore[method-assign]
    frame = await api.modbus_write_multiple_holding_registers(
        3, 40201, [1, 2], dry_run=True
    )
    assert isinstance(frame, str)
    api._send_modbus.assert_not_awaited()
    assert await api.modbus_write_multiple_holding_registers(3, 40201, [1, 2]) == {
        "quantity": 2
    }
    api._send_modbus.assert_awaited_once_with(frame=frame, data_type=DataType.U16)


async def test_send_modbus_decodes_response() -> None:
    """The fdbg transport validates and decodes its returned RTU frame."""
    body = bytes((3, 3, 2)) + struct.pack(">H", 42)
    client = MagicMock()
    client.post = AsyncMock(return_value={"data": _crc_frame(body)})
    api = _ModbusApi(client)
    frame = ModbusRtuFrameGenerator().generate_read_holding_register_frame(3, 40201, 1)
    assert await api._send_modbus(frame, DataType.U16) == 42
    client.post.assert_awaited_once_with("fdbg.cgi", {"data": frame})


@pytest.mark.parametrize("response", [None, "frame", {}, {"result": "ok"}])
async def test_send_modbus_rejects_unexpected_payload(response: object) -> None:
    """fdbg replies must be mappings containing a data frame."""
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    with pytest.raises(RuntimeError, match="Unexpected Modbus response"):
        await _ModbusApi(client)._send_modbus("frame", DataType.U16)


def test_battery_work_modes_include_known_and_unknown_modes() -> None:
    """Known modes remain stable and unknown firmware values remain selectable."""
    modes = BatteryWorkModes()
    known = modes.get_all_modes(1, 2)
    assert len(known) == 5
    assert modes.get_mode(1, 2) == BatteryWorkMode("Self-consumption mode", 2, 1)

    unknown = modes.get_all_modes(9, 99)
    assert len(unknown) == 6
    assert unknown[-1] == BatteryWorkMode("Unknown (mod_r: 99, type: 9)", 99, 9)
    assert modes.get_mode(9, 99) == unknown[-1]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, None),
        (0x3C02, ScheduleSlot(0, 0, 1, "charge")),
        (0x1E3C03, ScheduleSlot(0, 30, 1, "discharge")),
        (ScheduleSlot(5, 30, 4, "discharge").to_raw(), ScheduleSlot(5, 30, 4, "discharge")),
    ],
)
def test_schedule_slot_from_raw(code: int, expected: ScheduleSlot | None) -> None:
    """Raw schedule bit fields decode into useful slot values."""
    assert ScheduleSlot.from_raw(code) == expected


def test_schedule_slot_from_time_and_dict() -> None:
    """Human time strings and expanded mappings create the same slots."""
    expected = ScheduleSlot(8, 30, 2, "charge")
    assert ScheduleSlot.from_time("08:30", 2, "charge") == expected
    assert ScheduleSlot.from_dict(
        {"start": "08:30", "duration": 2, "mode": "charge"}
    ) == expected
    assert ScheduleSlot.from_dict(
        {"start_hour": 8, "start_minute": 30, "duration": 2, "mode": "charge"}
    ) == expected


@pytest.mark.parametrize(
    ("start", "duration", "mode", "message"),
    [
        ("08:15", 1, "charge", "Minutes must be 0 or 30"),
        ("24:00", 1, "charge", "Hour must be between 0 and 23"),
        ("08:00", 0, "charge", "Duration must be between 1 and 4"),
        ("08:00", 5, "charge", "Duration must be between 1 and 4"),
        ("08:00", 1, "idle", "Mode must be 'charge' or 'discharge'"),
    ],
)
def test_schedule_slot_from_time_validation(
    start: str, duration: int, mode: str, message: str
) -> None:
    """Invalid human schedule fields produce specific validation errors."""
    with pytest.raises(ValueError, match=message):
        ScheduleSlot.from_time(start, duration, mode)


def test_schedule_slot_conversion_and_display() -> None:
    """Slots round trip to raw data, mappings, and display strings."""
    slot = ScheduleSlot(22, 30, 2, "discharge")
    assert ScheduleSlot.from_raw(slot.to_raw()) == slot
    assert slot.to_dict() == {
        "start_hour": 22,
        "start_minute": 30,
        "duration": 2,
        "mode": "discharge",
    }
    assert slot.human_readable() == "22:30 - 00:30 (discharge)"
    assert slot.human_readable("{mode}: {start}/{end}") == "discharge: 22:30/00:30"
    slot.validate_duration()


def test_schedule_slot_to_raw_rejects_invalid_minute() -> None:
    """Programmatically constructed slots still validate raw minute encoding."""
    with pytest.raises(ValueError, match="Minutes must be 0 or 30"):
        ScheduleSlot(8, 15, 1, "charge").to_raw()


def test_schedule_slot_rejects_midnight_crossing() -> None:
    """A slot may end at midnight but cannot continue into the next day."""
    with pytest.raises(ValueError, match="crosses midnight"):
        ScheduleSlot(23, 0, 2, "charge").validate_duration()


def test_schedule_slot_list_validation() -> None:
    """Valid slots may be unsorted but cannot overlap or exceed six per day."""
    ScheduleSlot.validate_slots(
        [ScheduleSlot(10, 30, 1, "charge"), ScheduleSlot(8, 0, 2, "discharge")]
    )
    with pytest.raises(ValueError, match="Maximum 6 slots"):
        ScheduleSlot.validate_slots([ScheduleSlot(hour, 0, 1, "charge") for hour in range(7)])
    with pytest.raises(ValueError, match="overlaps"):
        ScheduleSlot.validate_slots(
            [ScheduleSlot(8, 0, 2, "charge"), ScheduleSlot(9, 30, 1, "discharge")]
        )
    with pytest.raises(ValueError, match="overlaps"):
        ScheduleSlot.validate_slots(
            [ScheduleSlot(8, 30, 1, "charge"), ScheduleSlot(9, 0, 1, "discharge")]
        )


def test_battery_schedule_decode_and_encode() -> None:
    """Weekly schedules decode six slots per day and preserve power limits."""
    first = ScheduleSlot(8, 0, 2, "charge")
    seventh = ScheduleSlot(20, 0, 1, "discharge")
    raw = {
        "Mon": [first.to_raw(), 0, 0, 0, 0, 0, seventh.to_raw()],
        "Pin": 123,
        "Pout": 456,
    }
    decoded = BatterySchedule.decode_schedule(raw)
    assert decoded["Mon"] == [first]
    assert decoded["Tus"] == []
    assert set(decoded) == set(BatterySchedule.DAYS)
    assert BatterySchedule.encode_schedule(
        {"Mon": [first], "Tus": []}, pin=123, pout=456
    ) == {"Mon": [first.to_raw()], "Pin": 123, "Pout": 456}


@pytest.mark.parametrize(
    ("model_type", "serial", "expected"),
    [(11, "INV", True), (20, None, True), (10, "BE123", True), (10, "INV", False), (None, None, False)],
)
def test_inverter_storage_detection(
    model_type: int | None, serial: str | None, expected: bool
) -> None:
    """Storage capability follows model codes and BE serial prefixes."""
    assert GetInverterInfoItemResponse(mty=model_type, isn=serial).isStorage() is expected


@pytest.mark.parametrize(
    ("api_class", "method", "endpoint", "payload", "expected_type"),
    [
        (
            SolplanetApiV1,
            "get_inverter_data",
            "invdata.cgi?sn=INV",
            {"pac": 1, "new": 2},
            GetInverterDataResponse,
        ),
        (SolplanetApiV1, "get_meter_data", "emeter.cgi", {"pac": 2}, GetMeterDataResponse),
        (SolplanetApiV1, "get_meter_info", "pwrlim.cgi", {"sn": "M"}, GetMeterInfoResponse),
        (
            SolplanetApiV2,
            "get_inverter_data",
            "getdevdata.cgi?device=2&sn=INV",
            {"pac": 1},
            GetInverterDataResponse,
        ),
        (SolplanetApiV2, "get_meter_data", "getdevdata.cgi?device=3", {"pac": 2}, GetMeterDataResponse),
        (SolplanetApiV2, "get_meter_info", "getdev.cgi?device=3", {"sn": "M"}, GetMeterInfoResponse),
        (SolplanetApiV2, "get_battery_data", "getdevdata.cgi?device=4&sn=BAT", {"soc": 50}, object),
    ],
)
async def test_api_data_getters(
    api_class: type[SolplanetApiV1] | type[SolplanetApiV2],
    method: str,
    endpoint: str,
    payload: dict,
    expected_type: type,
) -> None:
    """V1 and V2 getters use their protocol endpoints and typed responses."""
    client = MagicMock()
    client.get = AsyncMock(return_value=dict(payload))
    api = api_class(client)
    args = ("INV" if "INV" in endpoint else "BAT",) if "sn=" in endpoint else ()
    result = await getattr(api, method)(*args)
    client.get.assert_awaited_once_with(endpoint)
    if expected_type is object:
        assert result.soc == 50
    else:
        assert isinstance(result, expected_type)
    assert not hasattr(result, "new")


async def test_v2_indexed_meter_data_preserves_three_phase_payload() -> None:
    """Indexed V2 reads retain the reporter's nested three-phase telemetry."""
    payload = {
        "flg": 0,
        "tim": "2026-07-23 23:15:06",
        "pac": 0,
        "itd": 3864,
        "otd": 0,
        "iet": 448,
        "oet": 0,
        "mod": 7,
        "meter_general": {
            "prc": 156,
            "sac": 158,
            "phs": 90,
            "pf": -1,
            "avg_v": 2409,
            "avg_i": 2,
            "iac": 6,
            "fac": 4997,
            "iqet": 0,
            "oqet": 0,
        },
        "vac_phs": [2398, 2409, 2421],
        "iac_phs": [4, 1, 1],
        "vac_line": [4132, 4187, 4199],
        "pac_phs": [0, 0, 0],
        "prc_phs": [103, 24, 28],
        "sac_phs": [104, 25, 29],
        "pf_phs": [-1, -1, 0],
        "ang_phs": [0, 241, 121],
    }
    client = MagicMock()
    client.get = AsyncMock(return_value=payload)

    result = await SolplanetApiV2(client).get_meter_data(1)

    client.get.assert_awaited_once_with("getdevdata.cgi?device=3&submeter=1")
    assert result.pac == 0
    assert result.prc_phs == [103, 24, 28]
    assert result.meter_general == payload["meter_general"]


@pytest.mark.parametrize(
    ("api_class", "endpoint"),
    [(SolplanetApiV1, "invinfo.cgi"), (SolplanetApiV2, "getdev.cgi?device=2")],
)
async def test_api_inverter_info_nested_mapping(
    api_class: type[SolplanetApiV1] | type[SolplanetApiV2], endpoint: str
) -> None:
    """Nested inverter dictionaries become nested dataclasses."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={"num": 1, "inv": [{"isn": "INV", "mty": 11, "future": 1}], "future": 2}
    )
    result = await api_class(client).get_inverter_info()
    assert result == GetInverterInfoResponse(
        inv=[GetInverterInfoItemResponse(isn="INV", mty=11)], num=1
    )
    client.get.assert_awaited_once_with(endpoint)


@pytest.mark.parametrize("battery", [None, {"bid": 1, "partno": "P", "future": 2}])
async def test_v2_battery_info_nested_mapping(battery: dict | None) -> None:
    """Battery detail is optional and unknown firmware keys are ignored."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={"type": 1, "mod_r": 2, "battery": battery, "future": 3}
    )
    result = await SolplanetApiV2(client).get_battery_info("BAT")
    assert isinstance(result, GetBatteryInfoResponse)
    assert result.battery == (
        None if battery is None else GetBatteryInfoItemResponse(bid=1, partno="P")
    )
    client.get.assert_awaited_once_with("getdev.cgi?device=4&sn=BAT")


def _battery_info() -> GetBatteryInfoResponse:
    return GetBatteryInfoResponse(
        type=1,
        mod_r=2,
        discharge_max=10,
        charge_max=90,
        muf=3,
        mod=4,
        num=1,
    )


@pytest.mark.parametrize(
    ("method", "argument", "expected_type", "expected_mode", "expected_min", "expected_max"),
    [
        ("set_battery_work_mode", BatteryWorkMode("Custom", 4, 7), 7, 4, 10, 90),
        ("set_battery_soc_min", 15, 1, 2, 15, 90),
        ("set_battery_soc_max", 95, 1, 2, 10, 95),
    ],
)
async def test_v2_battery_configuration(
    method: str,
    argument: object,
    expected_type: int,
    expected_mode: int,
    expected_min: int,
    expected_max: int,
) -> None:
    """Battery changes preserve all untouched configuration values."""
    client = MagicMock()
    client.post = AsyncMock(return_value={"dat": "ok"})
    api = SolplanetApiV2(client)
    api.get_battery_info = AsyncMock(return_value=_battery_info())  # type: ignore[method-assign]
    await getattr(api, method)("BAT", argument)
    request = client.post.await_args.args[1]
    assert isinstance(request, SetBatteryConfigRequest)
    assert request.value.type == expected_type
    assert request.value.mod_r == expected_mode
    assert request.value.sn == "BAT"
    assert request.value.discharge_max == expected_min
    assert request.value.charge_max == expected_max
    client.post.assert_awaited_once_with("setting.cgi", request)


async def test_v2_get_schedule() -> None:
    """Schedule retrieval returns raw data, decoded slots, and power defaults."""
    slot = ScheduleSlot(8, 0, 1, "charge")
    client = MagicMock()
    client.get = AsyncMock(return_value={"Mon": [slot.to_raw()]})
    result = await SolplanetApiV2(client).get_schedule()
    assert result["raw"] == {"Mon": [slot.to_raw()]}
    assert result["slots"]["Mon"] == [slot]
    assert result["Pin"] == 0
    assert result["Pout"] == 0


@pytest.mark.parametrize(
    ("response", "raises"),
    [
        ({"dat": "ok"}, False),
        ({"status": 200}, False),
        ("ok", False),
        ({"status": 500}, True),
        ({"dat": "failed"}, True),
    ],
)
async def test_v2_set_schedule_power_response_validation(
    response: object, raises: bool
) -> None:
    """Explicit schedule failures raise while known and opaque successes pass."""
    current = {
        "slots": {day: [] for day in BatterySchedule.DAYS},
        "Pin": 100,
        "Pout": 200,
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    api = SolplanetApiV2(client)
    api.get_schedule = AsyncMock(return_value=current)  # type: ignore[method-assign]
    call = api.set_schedule_power(pin=300)
    if raises:
        with pytest.raises(RuntimeError, match="Schedule update failed"):
            await call
    else:
        await call
        request = client.post.await_args.args[1]
        assert isinstance(request, SetScheduleRequest)
        assert request.value["Pin"] == 300
        assert request.value["Pout"] == 200


async def test_v2_schedule_power_keeps_unspecified_values() -> None:
    """Unspecified pin and pout values are retained from the inverter."""
    current = {
        "slots": {day: [] for day in BatterySchedule.DAYS},
        "Pin": 100,
        "Pout": 200,
    }
    client = MagicMock()
    client.post = AsyncMock(return_value={"dat": "ok"})
    api = SolplanetApiV2(client)
    api.get_schedule = AsyncMock(return_value=current)  # type: ignore[method-assign]
    await api.set_schedule_power()
    request = client.post.await_args.args[1]
    assert request.value["Pin"] == 100
    assert request.value["Pout"] == 200


async def test_v2_schedule_pin_and_pout_helpers() -> None:
    """Convenience helpers target only their respective power parameter."""
    api = SolplanetApiV2(MagicMock())
    api.set_schedule_power = AsyncMock()  # type: ignore[method-assign]
    await api.set_schedule_pin(123)
    await api.set_schedule_pout(456)
    assert api.set_schedule_power.await_args_list[0].kwargs == {"pin": 123}
    assert api.set_schedule_power.await_args_list[1].kwargs == {"pout": 456}


@pytest.mark.parametrize(
    ("response", "raises"),
    [
        ({"dat": "ok"}, False),
        ({"status": 200}, False),
        (None, False),
        ({"status": 400}, True),
        ({"dat": "no"}, True),
    ],
)
async def test_v2_set_schedule_slots_response_validation(
    response: object, raises: bool
) -> None:
    """Raw slot writes use the same explicit-failure rules as power writes."""
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    api = SolplanetApiV2(client)
    call = api.set_schedule_slots({"Pin": 1})
    if raises:
        with pytest.raises(RuntimeError, match="Schedule update failed"):
            await call
    else:
        await call
        request = client.post.await_args.args[1]
        assert request == SetScheduleRequest(value={"Pin": 1})


def test_backward_compatible_api_alias() -> None:
    """The historic API class name continues to select V2."""
    assert SolplanetApi is SolplanetApiV2


@pytest.mark.parametrize("status, reply", [(1, "03040200010130"), (0, "0304020000c0f0")])
async def test_grid_register_read_uses_validated_function04_frame(status: int, reply: str) -> None:
    """Read the documented input register through the existing configured client."""
    client = MagicMock()
    client.post = AsyncMock(return_value={"dat": "ok", "data": reply})
    assert await _ModbusApi(client).modbus_read_input_registers(DataType.U16, 3, 31391) == status
    client.post.assert_awaited_once_with("fdbg.cgi", {"data": "0304056e00015139"})


@pytest.mark.parametrize("body", [
    b"", b"\x04\x04\x02\x00\x01", b"\x03\x03\x02\x00\x01",
    b"\x03\x04\x00\x00\x01", b"\x03\x04\x04\x00\x01\x00\x00",
])
async def test_grid_register_rejects_mismatched_response(body: bytes) -> None:
    """Valid checksums do not make a wrong slave, function or byte count acceptable."""
    client = MagicMock()
    client.post = AsyncMock(return_value={"data": _crc_frame(body)})
    with pytest.raises(ValueError, match="Modbus read response"):
        await _ModbusApi(client).modbus_read_input_registers(DataType.U16, 3, 31391)


async def test_grid_register_crc_failure_and_exception_response() -> None:
    client = MagicMock()
    client.post = AsyncMock(side_effect=[
        {"data": "03040200010131"},
        {"data": _crc_frame(b"\x03\x84\x02")},
    ])
    api = _ModbusApi(client)
    with pytest.raises(ValueError, match="CRC"):
        await api.modbus_read_input_registers(DataType.U16, 3, 31391)
    assert await api.modbus_read_input_registers(DataType.U16, 3, 31391) == {
        "device_id": 3, "error_function_code": 0x84, "exception_code": 2,
    }


async def test_modbus_read_validation_preserves_write_acknowledgements() -> None:
    """Read-response validation leaves the existing write acknowledgement path intact."""
    client = MagicMock()
    frame = ModbusRtuFrameGenerator().generate_write_single_holding_register_frame(
        7, 40201, 1, DataType.U16
    )
    client.post = AsyncMock(return_value={"data": frame})
    assert await _ModbusApi(client).modbus_write_single_holding_register(
        DataType.U16, 7, 40201, 1
    ) == {"device_id": 7, "function_code": 6, "register_address": 200, "data": 1}


@pytest.mark.parametrize("scheme, port", [("http", 18084), ("https", 9443)])
async def test_grid_register_uses_configured_connection(scheme: str, port: int) -> None:
    """The input-register read reuses the configured host, port and transport."""
    session = _Session(_Response(json.dumps({"data": _crc_frame(b"\x07\x04\x02\x00\x01")}).encode()))
    client = SolplanetClient("configured-dongle.local", session, scheme=scheme, port=port)
    assert await SolplanetApiV2(client).modbus_read_input_registers(DataType.U16, 7, 31391) == 1
    method, url, options = session.calls[0]
    assert method == "POST"
    assert url == f"{scheme}://configured-dongle.local:{port}/fdbg.cgi"
    assert options["json"]["data"].startswith("0704056e0001")
