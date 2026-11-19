"""Tests for the Solplanet Modbus RTU codec."""

from __future__ import annotations

import struct

import pytest

from custom_components.solplanet.modbus import DataType, ModbusRtuFrameGenerator


@pytest.fixture
def generator() -> ModbusRtuFrameGenerator:
    """Return a frame generator."""
    return ModbusRtuFrameGenerator()


def _with_crc(generator: ModbusRtuFrameGenerator, body: bytes) -> str:
    return (body + struct.pack("<H", generator._calculate_crc(body))).hex()


def _read_response(
    generator: ModbusRtuFrameGenerator,
    values: list[int],
    *,
    function: int = 0x03,
    device: int = 3,
) -> str:
    data = b"".join(struct.pack(">H", value) for value in values)
    return _with_crc(generator, bytes((device, function, len(data))) + data)


@pytest.mark.parametrize(
    ("method", "address", "function", "offset", "value"),
    [
        ("generate_read_holding_register_frame", 40201, 0x03, 200, 2),
        ("generate_read_input_register_frame", 30005, 0x04, 4, 2),
    ],
)
def test_read_frame_generation(
    generator: ModbusRtuFrameGenerator,
    method: str,
    address: int,
    function: int,
    offset: int,
    value: int,
) -> None:
    """Read frames contain the expected address, count, and valid CRC."""
    frame = bytes.fromhex(getattr(generator, method)(3, address, value))
    assert struct.unpack(">B B H H", frame[:6]) == (3, function, offset, value)
    assert struct.unpack("<H", frame[-2:])[0] == generator._calculate_crc(frame[:-2])


def test_write_single_frame_encodes_value(generator: ModbusRtuFrameGenerator) -> None:
    """Single-register writes encode signed values before framing."""
    frame = bytes.fromhex(generator.generate_write_single_holding_register_frame(3, 40201, -2, DataType.S16))
    assert struct.unpack(">B B H H", frame[:6]) == (3, 0x06, 200, 0xFFFE)


def test_write_multiple_frame(generator: ModbusRtuFrameGenerator) -> None:
    """Multiple-register writes include count, byte count, values, and CRC."""
    frame = bytes.fromhex(generator.generate_write_multiple_holding_registers_frame(3, 40201, [1, 0xFFFF]))
    assert struct.unpack(">B B H H B H H", frame[:-2]) == (
        3,
        0x10,
        200,
        2,
        4,
        1,
        0xFFFF,
    )
    assert struct.unpack("<H", frame[-2:])[0] == generator._calculate_crc(frame[:-2])


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((3, 40201, []), "Values must not be empty"),
        ((-1, 40201, [1]), "Invalid device ID"),
        ((256, 40201, [1]), "Invalid device ID"),
        ((3, 40000, [1]), "Invalid register offset"),
        ((3, 105537, [1]), "Invalid register offset"),
        ((3, 40201, [1] * 124), "Invalid register quantity"),
        ((3, 40201, [-1]), "Invalid register value"),
        ((3, 40201, [0x10000]), "Invalid register value"),
    ],
)
def test_write_multiple_validation(
    generator: ModbusRtuFrameGenerator,
    args: tuple[int, int, list[int]],
    message: str,
) -> None:
    """Invalid multiple-write parameters fail before a frame is emitted."""
    with pytest.raises(ValueError, match=message):
        generator.generate_write_multiple_holding_registers_frame(*args)


@pytest.mark.parametrize(
    ("device", "offset", "value", "message"),
    [
        (-1, 0, 1, "Invalid device ID"),
        (256, 0, 1, "Invalid device ID"),
        (1, -1, 1, "Invalid register offset"),
        (1, 0x10000, 1, "Invalid register offset"),
        (1, 0, -1, "Invalid value"),
        (1, 0, 0x10000, "Invalid value"),
    ],
)
def test_generic_frame_validation(
    generator: ModbusRtuFrameGenerator,
    device: int,
    offset: int,
    value: int,
    message: str,
) -> None:
    """The common frame builder validates every packed field."""
    with pytest.raises(ValueError, match=message):
        generator._generate_frame(device, 3, offset, value)


@pytest.mark.parametrize(
    ("data_type", "values", "expected"),
    [
        (DataType.B16, [0x1234], 0x1234),
        (DataType.E16, [7], 7),
        (DataType.S16, [0xFFFE], -2),
        (DataType.U16, [1234], 1234),
        (DataType.B32, [0x1234, 0x5678], 0x12345678),
        (DataType.S32, [0xFFFF, 0xFFFE], -2),
        (DataType.U32, [0x0001, 0x0000], 65536),
        (DataType.STRING, [0x4142], "AB"),
        (DataType.STRING, [0x4100], "A"),
        (DataType.STRING, [0x0042], "B"),
    ],
)
def test_decode_register_types(
    generator: ModbusRtuFrameGenerator,
    data_type: DataType,
    values: list[int],
    expected: int | str,
) -> None:
    """Register replies decode every supported scalar representation."""
    assert generator.decode_response(_read_response(generator, values), data_type) == expected


@pytest.mark.parametrize(
    "data_type",
    [
        DataType.B16,
        DataType.B32,
        DataType.S16,
        DataType.U16,
        DataType.S32,
        DataType.U32,
        DataType.E16,
        DataType.STRING,
    ],
)
def test_decode_nan_sentinels(generator: ModbusRtuFrameGenerator, data_type: DataType) -> None:
    """Protocol NaN sentinels become ``None``."""
    raw = generator.NAN_VALUES[data_type]
    values = [raw >> 16, raw & 0xFFFF] if raw > 0xFFFF else [raw]
    assert generator.decode_response(_read_response(generator, values), data_type) is None


def test_decode_u16_block(generator: ModbusRtuFrameGenerator) -> None:
    """A U16 block preserves each register and its NaN value."""
    response = _read_response(generator, [1, 0xFFFF, 3], function=0x04)
    assert generator.decode_response(response, DataType.U16) == [1, None, 3]


@pytest.mark.parametrize("data_type", [DataType.B32, DataType.S32, DataType.U32])
def test_decode_rejects_short_32_bit_values(generator: ModbusRtuFrameGenerator, data_type: DataType) -> None:
    """A 32-bit value requires two registers."""
    with pytest.raises(ValueError, match="Insufficient data"):
        generator.decode_response(_read_response(generator, [1]), data_type)


def test_decode_rejects_short_16_bit_value(generator: ModbusRtuFrameGenerator) -> None:
    """A scalar 16-bit value requires one full register."""
    response = _with_crc(generator, bytes((1, 3, 1, 0x12)))
    with pytest.raises(ValueError, match="Insufficient data"):
        generator.decode_response(response, DataType.S16)


def test_decode_write_acknowledgements(generator: ModbusRtuFrameGenerator) -> None:
    """Single and multiple-write acknowledgements expose their fields."""
    single = _with_crc(generator, struct.pack(">B B H H", 3, 0x06, 200, 1))
    multiple = _with_crc(generator, struct.pack(">B B H H", 3, 0x10, 200, 2))
    assert generator.decode_response(single, DataType.U16) == {
        "device_id": 3,
        "function_code": 6,
        "register_address": 200,
        "data": 1,
    }
    assert generator.decode_response(multiple, DataType.U16) == {
        "device_id": 3,
        "function_code": 16,
        "register_address": 200,
        "quantity": 2,
    }


@pytest.mark.parametrize("function", [0x83, 0x84, 0x86, 0x90])
def test_decode_modbus_error(generator: ModbusRtuFrameGenerator, function: int) -> None:
    """All supported Modbus exception function codes are decoded."""
    response = _with_crc(generator, bytes((3, function, 2)))
    assert generator.decode_response(response, DataType.U16) == {
        "device_id": 3,
        "error_function_code": function,
        "exception_code": 2,
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\x01\x03", "Invalid response length"),
        (b"\x01\x05\x00", "Unsupported function code"),
    ],
)
def test_decode_rejects_invalid_responses(
    generator: ModbusRtuFrameGenerator, body: bytes, message: str
) -> None:
    """Malformed and unknown response types are rejected."""
    response = body.hex() if len(body) < 5 else _with_crc(generator, body)
    if len(body) == 3:
        response = _with_crc(generator, body)
    with pytest.raises(ValueError, match=message):
        generator.decode_response(response, DataType.U16)


@pytest.mark.parametrize(
    ("function", "body", "message"),
    [
        (0x06, bytes((1, 0x06, 0)), "Invalid response length"),
        (0x10, bytes((1, 0x10, 0)), "Invalid response length"),
        (0x83, bytes((1, 0x83, 1, 0)), "Invalid error response length"),
    ],
)
def test_decode_rejects_wrong_ack_length(
    generator: ModbusRtuFrameGenerator,
    function: int,
    body: bytes,
    message: str,
) -> None:
    """Acknowledgements must have their exact protocol length."""
    del function
    response = _with_crc(generator, body)
    with pytest.raises(ValueError, match=message):
        generator.decode_response(response, DataType.U16)


def test_decode_rejects_bad_crc(generator: ModbusRtuFrameGenerator) -> None:
    """CRC corruption is detected before values are returned."""
    response = _read_response(generator, [42])
    corrupted = response[:-2] + ("00" if response[-2:] != "00" else "01")
    with pytest.raises(ValueError, match="CRC error"):
        generator.decode_response(corrupted, DataType.U16)


@pytest.mark.parametrize(
    ("value", "data_type", "expected"),
    [
        (None, DataType.U16, 0xFFFF),
        (None, DataType.S32, 0x80000000),
        (12, DataType.B16, 12),
        (12, DataType.B32, 12),
        (12, DataType.U16, 12),
        (12, DataType.U32, 12),
        (12, DataType.E16, 12),
        (-2, DataType.S16, 0xFFFE),
        (-2, DataType.S32, 0xFFFFFFFE),
        ("A", DataType.STRING, 0x4100),
        ("AB", DataType.STRING, 0x4142),
    ],
)
def test_encode_request_data(
    generator: ModbusRtuFrameGenerator,
    value: object,
    data_type: DataType,
    expected: int,
) -> None:
    """All supported request representations encode to register integers."""
    assert generator.encode_request_data(value, data_type) == expected


@pytest.mark.parametrize(
    ("value", "data_type", "message"),
    [
        (-1, DataType.U16, "must be in range"),
        (0x10000, DataType.U16, "must be in range"),
        (-32769, DataType.S16, "must be in range"),
        (32768, DataType.S16, "must be in range"),
        (1, DataType.STRING, "must be a string"),
        ("ABC", DataType.STRING, "must be a string"),
    ],
)
def test_encode_rejects_invalid_values(
    generator: ModbusRtuFrameGenerator,
    value: object,
    data_type: DataType,
    message: str,
) -> None:
    """Out-of-range numbers and malformed strings are rejected."""
    with pytest.raises(ValueError, match=message):
        generator.encode_request_data(value, data_type)


def test_unsupported_data_type_paths(generator: ModbusRtuFrameGenerator) -> None:
    """Unknown enum-like values cannot be decoded or encoded."""

    class Unknown:
        value = "unknown"

    unknown = Unknown()
    generator.NAN_VALUES[unknown] = -1  # type: ignore[index]
    with pytest.raises(ValueError, match="Unsupported data type"):
        generator._decode_value(1, unknown)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unsupported data type"):
        generator.encode_request_data(1, unknown)  # type: ignore[arg-type]
