"""Solplanet sensors platform."""

import logging
from collections import abc
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolplanetConfigEntry
from .client import GetInverterDataResponse
from .const import (
    BATTERY_COMMUNICATION_STATUS,
    BATTERY_ERRORS_1,
    BATTERY_ERRORS_2,
    BATTERY_ERRORS_3,
    BATTERY_ERRORS_4,
    BATTERY_IDENTIFIER,
    BATTERY_STATUS,
    BATTERY_WARNINGS_1,
    BATTERY_WARNINGS_2,
    BATTERY_WARNINGS_3,
    BATTERY_WARNINGS_4,
    DISCOVERY_SIGNAL,
    DONGLE_IDENTIFIER,
    INVERTER_ERROR_CODES,
    INVERTER_IDENTIFIER,
    INVERTER_STATUS,
    METER_IDENTIFIER,
)
from .coordinator import SolplanetDataUpdateCoordinator
from .entity import SolplanetEntity, SolplanetEntityDescription, get_entity_unique_id

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SolplanetSensorEntityDescription(SolplanetEntityDescription, SensorEntityDescription):
    """Describe Solplanet sensor entity."""


class SolplanetSensor(SolplanetEntity, SensorEntity):
    """Representation of a Solplanet sensor."""

    entity_description: SolplanetSensorEntityDescription
    _attr_native_value: float | int | str | None

    def __init__(
        self,
        description: SolplanetSensorEntityDescription,
        isn: str,
        coordinator: SolplanetDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(description=description, isn=isn, coordinator=coordinator)


def _create_mppt_power_mapper(
    index: int,
) -> abc.Callable[[GetInverterDataResponse], float | None]:
    def map_mppt_power(data: GetInverterDataResponse) -> float | None:
        if data.ipv and data.vpv:
            current = data.ipv[index] or 0
            voltage = data.vpv[index] or 0
            return current * voltage
        return None

    return map_mppt_power


def _create_dict_mapper(
    dictionary: abc.Mapping[int, str], default: str = "Unknown (code: {value})"
) -> abc.Callable[[int], str]:
    def map_dict(value: int) -> str:
        return dictionary.get(value, default.replace("{value}", str(value)))

    return map_dict


def _power_limit_int(data: Any, field: str) -> int | None:
    """Return one power-limit field as an integer from a dict or response model."""
    value = data.get(field) if isinstance(data, dict) else getattr(data, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _power_limit_control(data: Any) -> str | None:
    """Map both power-limit protocol variants to one control state."""
    if data is None:
        return None
    if _power_limit_int(data, "regulate") != 10:
        return "Disabled"

    has_ctrl_type = "ctrlType" in data if isinstance(data, dict) else hasattr(data, "ctrlType")
    if not has_ctrl_type:
        return "Limit power"
    ctrl_type = _power_limit_int(data, "ctrlType")
    if ctrl_type is None:
        return "Enabled (unknown type)"
    return {
        0: "Limit power",
        1: "Limit current",
        2: "Zero power",
    }.get(ctrl_type, "Enabled (unknown type)")


def _power_limit_enum(field: str, values: dict[int, str]) -> abc.Callable[[Any], str | None]:
    """Create a mapper for a power-limit enum field."""

    def mapper(data: Any) -> str | None:
        value = _power_limit_int(data, field)
        return values.get(value) if value is not None else None

    return mapper


def _power_limit_value(
    field: str,
    *,
    when: tuple[str, int] | None = None,
) -> abc.Callable[[Any], int | None]:
    """Create a mapper for a value available only in one power-limit mode."""

    def mapper(data: Any) -> int | None:
        if when is not None and _power_limit_int(data, when[0]) != when[1]:
            return None
        return _power_limit_int(data, field)

    return mapper


def _power_limit_sensor(
    isn: str,
    data_type: str,
    suffix: str,
    *,
    path: list[str | int] | None = None,
    mapper: abc.Callable[[Any], Any] | None = None,
    multiplier: float | None = None,
    unit: str | None = None,
    device_class: SensorDeviceClass | None = None,
) -> SolplanetSensorEntityDescription:
    """Create one meter power-limit diagnostic sensor description."""
    return SolplanetSensorEntityDescription(
        key=f"{isn}_{suffix}",
        translation_key=suffix,
        entity_category=EntityCategory.DIAGNOSTIC,
        data_field_device_type=METER_IDENTIFIER,
        data_field_data_type=data_type,
        data_field_path=path or [],
        data_field_value_mapper=mapper,
        data_field_value_multiply=multiplier,
        native_unit_of_measurement=unit,
        device_class=device_class,
        unique_id_suffix=suffix,
    )


def _create_power_limit_entities_description(
    isn: str,
    *,
    data_type: str,
    compatibility: bool,
) -> list[SolplanetSensorEntityDescription]:
    """Create readable diagnostic entities for either meter-limit protocol."""
    phase_mode_mapper = _power_limit_enum("abs", {0: "Phase-balanced", 1: "Phase-specific"})
    entities = [
        _power_limit_sensor(
            isn,
            data_type,
            "power_limit_control",
            mapper=_power_limit_control,
            device_class=SensorDeviceClass.ENUM,
        ),
        _power_limit_sensor(
            isn,
            data_type,
            "power_limit_phase_mode",
            mapper=phase_mode_mapper,
            device_class=SensorDeviceClass.ENUM,
        ),
    ]

    if compatibility:
        entities.append(
            _power_limit_sensor(
                isn,
                data_type,
                "export_power_limit_setpoint_percentage",
                path=["exp_m"],
                multiplier=0.01,
                unit=PERCENTAGE,
            )
        )
        return entities

    entities.extend(
        [
            _power_limit_sensor(
                isn,
                data_type,
                "power_limit_type",
                mapper=_power_limit_enum("limitType", {0: "Absolute power", 1: "Percentage of rated power"}),
                device_class=SensorDeviceClass.ENUM,
            ),
            _power_limit_sensor(
                isn,
                data_type,
                "export_power_limit_setpoint",
                mapper=_power_limit_value("target", when=("limitType", 0)),
                unit=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
            ),
            _power_limit_sensor(
                isn,
                data_type,
                "export_power_limit_setpoint_percentage",
                mapper=_power_limit_value("targetPer", when=("limitType", 1)),
                unit=PERCENTAGE,
            ),
            _power_limit_sensor(
                isn,
                data_type,
                "power_limit_setpoint_offset",
                mapper=_power_limit_value("powerDiff"),
                unit=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
            ),
            _power_limit_sensor(
                isn,
                data_type,
                "communication_loss_timeout",
                path=["lostTime"],
                unit=UnitOfTime.SECONDS,
                device_class=SensorDeviceClass.DURATION,
            ),
            _power_limit_sensor(
                isn,
                data_type,
                "communication_loss_power_limit",
                path=["lostPowerMax"],
                unit=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
            ),
        ]
    )
    return entities


def _create_dict_set_mapper(
    length: int,
    fields: list[str],
    errors: list[dict[int, str]],
    none_value: str,
    default: str = "Unknown (code: {value})",
) -> abc.Callable[[Any], str]:
    def map_set_dict(data: Any) -> str:
        messages: list[str] = []
        for idx, field in enumerate(fields):
            value = getattr(data, field)

            if value is None:
                continue

            binary_str = bin(value)[2:].zfill(length)
            positions: list[str] = [
                errors[idx].get(i, default.replace("{value}", str(i)))
                for i in range(length)
                if binary_str[length - 1 - i] == "0"
            ]

            messages.extend(filter(lambda x: x is not None, positions))

        if not messages:
            return none_value

        result = ", ".join(messages)

        return result if len(result) <= 255 else f"{result[:252]}..."

    return map_set_dict


def create_inverter_entities_description(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetSensorEntityDescription]:
    """Create entities for inverter."""
    sensors = [
        SolplanetSensorEntityDescription(
            key=f"{isn}_flg",
            translation_key="inverter_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["flg"],
            data_field_NaN_value=0xFF,
            device_class=SensorDeviceClass.ENUM,
            data_field_value_mapper=_create_dict_mapper(INVERTER_STATUS),
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_err",
            translation_key="error_code",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["err"],
            data_field_value_mapper=_create_dict_mapper(INVERTER_ERROR_CODES),
            device_class=SensorDeviceClass.ENUM,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_fac",
            translation_key="frequency",
            entity_registry_enabled_default=False,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["fac"],
            data_field_NaN_value=0xFFFF,
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfFrequency.HERTZ,
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_pac",
            translation_key="power",
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["pac"],
            data_field_NaN_value=0xFFFFFFFF,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_sac",
            translation_key="apparent_power",
            entity_registry_enabled_default=False,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["sac"],
            data_field_NaN_value=0xFFFFFFFF,
            native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
            device_class=SensorDeviceClass.APPARENT_POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_qac",
            translation_key="reactive_power",
            entity_registry_enabled_default=False,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["qac"],
            data_field_NaN_value=0x80000000,
            native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
            device_class=SensorDeviceClass.REACTIVE_POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_pf",
            translation_key="power_factor",
            entity_registry_enabled_default=False,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["pf"],
            data_field_value_multiply=0.01,
            device_class=SensorDeviceClass.POWER_FACTOR,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_eto",
            translation_key="energy_produced_total",
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["eto"],  # codespell:ignore eto
            data_field_NaN_value=0xFFFFFFFF,
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_etd",
            translation_key="energy_produced_today",
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["etd"],
            data_field_NaN_value=0xFFFFFFFF,
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_tmp",
            translation_key="temperature",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["tmp"],
            data_field_NaN_value=-32768,
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_hto",
            translation_key="total_working_hours",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=INVERTER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["hto"],
            data_field_NaN_value=0xFFFFFFFF,
            native_unit_of_measurement=UnitOfTime.HOURS,
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
    ]

    for i in range(3):
        sensors.extend(
            [
                SolplanetSensorEntityDescription(
                    key=f"{isn}_pac{i + 1}",
                    translation_key="ac_phase_power",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[f"pac{i + 1}"],
                    native_unit_of_measurement=UnitOfPower.WATT,
                    device_class=SensorDeviceClass.POWER,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_qac{i + 1}",
                    translation_key="ac_phase_reactive_power",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[f"qac{i + 1}"],
                    native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
                    device_class=SensorDeviceClass.REACTIVE_POWER,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
            ]
        )

    data: GetInverterDataResponse | None = coordinator.data[INVERTER_IDENTIFIER][isn]["data"]
    if data is None:
        return sensors

    for i in range(len(data.vac or [])):
        sensors.extend(
            [
                SolplanetSensorEntityDescription(
                    key=f"{isn}_vac_{i}",
                    translation_key="ac_phase_voltage",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=["vac", i],
                    data_field_value_multiply=0.1,
                    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                    device_class=SensorDeviceClass.VOLTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_iac_{i}",
                    translation_key="ac_phase_current",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=["iac", i],
                    data_field_value_multiply=0.1,
                    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                    device_class=SensorDeviceClass.CURRENT,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
            ]
        )

    for i in range(len(data.vpv or [])):
        sensors.extend(
            [
                SolplanetSensorEntityDescription(
                    key=f"{isn}_vpv_{i}",
                    translation_key="mppt_voltage",
                    translation_placeholders={"mppt": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=["vpv", i],
                    data_field_value_multiply=0.1,
                    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                    device_class=SensorDeviceClass.VOLTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_ipv_{i}",
                    translation_key="mppt_current",
                    translation_placeholders={"mppt": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=["ipv", i],
                    data_field_value_multiply=0.01,
                    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                    device_class=SensorDeviceClass.CURRENT,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_mppt_power_{i}",
                    translation_key="mppt_power",
                    translation_placeholders={"mppt": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=INVERTER_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[],
                    data_field_value_mapper=_create_mppt_power_mapper(i),
                    data_field_value_multiply=0.001,
                    unique_id_suffix=f"mppt_{i}_power",
                    native_unit_of_measurement=UnitOfPower.WATT,
                    device_class=SensorDeviceClass.POWER,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
            ]
        )

    return sensors


def create_meter_entities_description(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetSensorEntityDescription]:
    """Create entities for meter."""
    meter_entry = coordinator.data.get(METER_IDENTIFIER, {}).get(isn, {})

    # V2: meters are sourced from `POST /getting.cgi` and stored under `app_data`.
    # V1: meters come from the legacy endpoints and are stored under `data`/`info`.
    if isinstance(meter_entry, dict) and "app_data" in meter_entry:
        app_sensors: list[SolplanetSensorEntityDescription] = [
            SolplanetSensorEntityDescription(
                key=f"{isn}_power",
                translation_key="meter_power",
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["power"],
                native_unit_of_measurement=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="power",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_uv",
                translation_key="line_neutral_voltage",
                entity_registry_enabled_default=False,
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["uv"],
                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                device_class=SensorDeviceClass.VOLTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="uv",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_ui",
                translation_key="line_neutral_current",
                entity_registry_enabled_default=False,
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["ui"],
                native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                device_class=SensorDeviceClass.CURRENT,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="ui",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_up",
                translation_key="line_neutral_active_power",
                entity_registry_enabled_default=False,
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["up"],
                native_unit_of_measurement=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="up",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_upf",
                translation_key="line_neutral_power_factor",
                entity_registry_enabled_default=False,
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["upf"],
                device_class=SensorDeviceClass.POWER_FACTOR,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="upf",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_sac",
                translation_key="total_apparent_power",
                entity_registry_enabled_default=False,
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["sac"],
                native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
                device_class=SensorDeviceClass.APPARENT_POWER,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="sac",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_prc",
                translation_key="total_reactive_power",
                entity_registry_enabled_default=False,
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["prc"],
                native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
                device_class=SensorDeviceClass.REACTIVE_POWER,
                state_class=SensorStateClass.MEASUREMENT,
                unique_id_suffix="prc",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_i_today",
                translation_key="grid_supplied_today",
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["i_today"],
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL_INCREASING,
                unique_id_suffix="i_today",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_o_today",
                translation_key="grid_feed_in_today",
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["o_today"],
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL_INCREASING,
                unique_id_suffix="o_today",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_i_total",
                translation_key="total_grid_supplied",
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["i_total"],
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL,
                unique_id_suffix="i_total",
            ),
            SolplanetSensorEntityDescription(
                key=f"{isn}_o_total",
                translation_key="total_grid_feed_in",
                data_field_device_type=METER_IDENTIFIER,
                data_field_data_type="app_data",
                data_field_path=["o_total"],
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL,
                unique_id_suffix="o_total",
            ),
        ]

        # Always create the diagnostics. A transient configuration read during
        # setup should yield Unknown, not permanently omit the entities.
        app_sensors.extend(
            _create_power_limit_entities_description(
                isn,
                data_type="meter_req",
                compatibility=False,
            )
        )

        return app_sensors

    # V2 sub-meters discovered via app-protocol are represented as devices (via app_info) but may
    # not have any live data yet. Do not create placeholder entities.
    if isinstance(meter_entry, dict) and "app_info" in meter_entry:
        return []

    if isinstance(meter_entry, dict) and meter_entry.get("is_submeter"):
        return _create_submeter_entities_description(isn)

    sensors: list[SolplanetSensorEntityDescription] = [
        SolplanetSensorEntityDescription(
            key=f"{isn}_pac",
            translation_key="grid_power",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["pac"],
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unique_id_suffix="pac",
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_iet",
            translation_key="grid_energy_in_total",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["iet"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_oet",
            translation_key="grid_energy_out_total",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["oet"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_itd",
            translation_key="grid_energy_in_today",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["itd"],
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_otd",
            translation_key="grid_energy_out_today",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["otd"],
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
    ]

    sensors.extend(
        _create_power_limit_entities_description(
            isn,
            data_type="info",
            compatibility=True,
        )
    )

    return sensors


def _create_submeter_entities_description(
    isn: str,
) -> list[SolplanetSensorEntityDescription]:
    """Create neutral import/export sensors for a legacy secondary meter."""
    return [
        SolplanetSensorEntityDescription(
            key=f"{isn}_submeter_power",
            translation_key="submeter_power",
            unique_id_suffix="submeter_power",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["pac"],
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_submeter_imported_today",
            translation_key="submeter_energy_imported_today",
            unique_id_suffix="submeter_energy_imported_today",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["itd"],
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_submeter_imported_total",
            translation_key="submeter_energy_imported_total",
            unique_id_suffix="submeter_energy_imported_total",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["iet"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_submeter_exported_today",
            translation_key="submeter_energy_exported_today",
            unique_id_suffix="submeter_energy_exported_today",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["otd"],
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_submeter_exported_total",
            translation_key="submeter_energy_exported_total",
            unique_id_suffix="submeter_energy_exported_total",
            data_field_device_type=METER_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["oet"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
    ]


def create_dongle_entities_description(
    coordinator: SolplanetDataUpdateCoordinator, dongle_id: str
) -> list[SolplanetSensorEntityDescription]:
    """Create diagnostic entities for the dongle (V2)."""

    def _stringify(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text if len(text) <= 255 else f"{text[:252]}..."

    def _warnings_text(value: Any) -> str:
        # Endpoint behavior observed: it may 404 (no warnings) or otherwise be missing.
        if value is None:
            return "No warnings"
        if isinstance(value, dict) and not value:
            return "No warnings"
        return _stringify(value) or "No warnings"

    return [
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_network_mode",
            translation_key="network_mode",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="network",
            data_field_path=["mode"],
            data_field_value_mapper=_stringify,
        ),
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_network_ssid",
            translation_key="wifi_ssid",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="network",
            data_field_path=["sid"],
            data_field_value_mapper=_stringify,
        ),
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_wifi_rssi",
            translation_key="wifi_signal_strength",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="network",
            data_field_path=["srh"],
            native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_network_ip",
            translation_key="ip_address",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="network",
            data_field_path=["ip"],
            data_field_value_mapper=_stringify,
        ),
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_network_gateway",
            translation_key="gateway",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="network",
            data_field_path=["gtw"],
            data_field_value_mapper=_stringify,
        ),
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_network_netmask",
            translation_key="netmask",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="network",
            data_field_path=["msk"],
            data_field_value_mapper=_stringify,
        ),
        SolplanetSensorEntityDescription(
            key=f"{dongle_id}_warnings",
            translation_key="warnings",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=DONGLE_IDENTIFIER,
            data_field_data_type="warnings",
            data_field_path=[],
            data_field_value_mapper=_warnings_text,
        ),
    ]


def create_battery_entities_description(
    coordinator: SolplanetDataUpdateCoordinator, isn: str
) -> list[SolplanetSensorEntityDescription]:
    """Create entities for battery."""
    sensors = [
        SolplanetSensorEntityDescription(
            key=f"{isn}_cst",
            translation_key="communication_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["cst"],
            data_field_value_mapper=_create_dict_mapper(
                BATTERY_COMMUNICATION_STATUS, "Fault (code: {value})"
            ),
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_bst",
            translation_key="battery_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["bst"],
            data_field_value_mapper=_create_dict_mapper(BATTERY_STATUS),
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_eb1",
            translation_key="battery_errors",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=[],
            data_field_value_mapper=_create_dict_set_mapper(
                16,
                ["eb1", "eb2", "eb3", "eb4"],
                [
                    BATTERY_ERRORS_1,
                    BATTERY_ERRORS_2,
                    BATTERY_ERRORS_3,
                    BATTERY_ERRORS_4,
                ],
                "No errors",
            ),
            unique_id_suffix="eb1",
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_wb1",
            translation_key="battery_warnings",
            entity_category=EntityCategory.DIAGNOSTIC,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=[],
            data_field_value_mapper=_create_dict_set_mapper(
                16,
                ["wb1", "wb2", "wb3", "wb4"],
                [
                    BATTERY_WARNINGS_1,
                    BATTERY_WARNINGS_2,
                    BATTERY_WARNINGS_3,
                    BATTERY_WARNINGS_4,
                ],
                "No warnings",
            ),
            unique_id_suffix="wb1",
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_ppv",
            translation_key="pv_power",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["ppv"],
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_etdpv",
            translation_key="pv_energy_today",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["etdpv"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_etopv",
            translation_key="pv_energy_total",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["etopv"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_vb",
            translation_key="battery_voltage",
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["vb"],
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_cb",
            translation_key="battery_current",
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["cb"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_pb",
            translation_key="battery_power",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["pb"],
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_tb",
            translation_key="battery_temperature",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["tb"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_soc",
            translation_key="battery_state_of_charge",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["soc"],
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_soh",
            translation_key="battery_state_of_health",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["soh"],
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_cli",
            translation_key="charging_current_limit",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["cli"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_clo",
            translation_key="discharging_current_limit",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["clo"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_ebi",
            translation_key="battery_energy_charging",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["ebi"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_ebo",
            translation_key="battery_energy_discharging",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["ebo"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_eaci",
            translation_key="ac_energy_charging",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["eaci"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_eaco",
            translation_key="ac_energy_discharging",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["eaco"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_vesp",
            translation_key="eps_voltage",
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["vesp"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_cesp",
            translation_key="eps_current",
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["cesp"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_fesp",
            translation_key="eps_frequency",
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["fesp"],
            data_field_value_multiply=0.01,
            native_unit_of_measurement=UnitOfFrequency.HERTZ,
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_pesp",
            translation_key="eps_power",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["pesp"],
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_rpesp",
            translation_key="eps_reactive_power",
            entity_registry_enabled_default=False,
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["rpesp"],
            native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
            device_class=SensorDeviceClass.REACTIVE_POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_etdesp",
            translation_key="eps_energy_today",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["etdesp"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SolplanetSensorEntityDescription(
            key=f"{isn}_etoesp",
            translation_key="eps_energy_total",
            data_field_device_type=BATTERY_IDENTIFIER,
            data_field_data_type="data",
            data_field_path=["etoesp"],
            data_field_value_multiply=0.1,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
        ),
    ]

    for i in range(3):
        sensors.extend(
            [
                SolplanetSensorEntityDescription(
                    key=f"{isn}_vl{i + 1}esp",
                    translation_key="eps_phase_voltage",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=BATTERY_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[f"vl{i + 1}esp"],
                    data_field_value_multiply=0.1,
                    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                    device_class=SensorDeviceClass.VOLTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_il{i + 1}esp",
                    translation_key="eps_phase_current",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=BATTERY_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[f"il{i + 1}esp"],
                    data_field_value_multiply=0.1,
                    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                    device_class=SensorDeviceClass.CURRENT,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_pac{i + 1}esp",
                    translation_key="eps_phase_power",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=BATTERY_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[f"pac{i + 1}esp"],
                    native_unit_of_measurement=UnitOfPower.WATT,
                    device_class=SensorDeviceClass.POWER,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
                SolplanetSensorEntityDescription(
                    key=f"{isn}_qac{i + 1}esp",
                    translation_key="eps_phase_reactive_power",
                    translation_placeholders={"phase": str(i + 1)},
                    entity_registry_enabled_default=False,
                    data_field_device_type=BATTERY_IDENTIFIER,
                    data_field_data_type="data",
                    data_field_path=[f"qac{i + 1}esp"],
                    native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
                    device_class=SensorDeviceClass.REACTIVE_POWER,
                    state_class=SensorStateClass.MEASUREMENT,
                ),
            ]
        )

    return sensors


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolplanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for Solplanet Inverter from a config entry."""
    coordinator = entry.runtime_data.coordinator

    description_factories = {
        DONGLE_IDENTIFIER: create_dongle_entities_description,
        INVERTER_IDENTIFIER: create_inverter_entities_description,
        BATTERY_IDENTIFIER: create_battery_entities_description,
        METER_IDENTIFIER: create_meter_entities_description,
    }
    known_unique_ids: set[str] = set()

    def _create_sensors(device_type: str, device_ids: set[str]) -> list[SolplanetSensor]:
        factory = description_factories[device_type]
        new_sensors: list[SolplanetSensor] = []
        for device_id in device_ids:
            for description in factory(coordinator, device_id):
                unique_id = get_entity_unique_id(description, device_id)
                if unique_id in known_unique_ids:
                    continue
                sensor = SolplanetSensor(
                    description=description,
                    isn=device_id,
                    coordinator=coordinator,
                )
                known_unique_ids.add(unique_id)
                new_sensors.append(sensor)
        return new_sensors

    sensors = [
        sensor
        for device_type in description_factories
        for sensor in _create_sensors(device_type, set(coordinator.data[device_type]))
    ]

    @callback
    def _async_add_discovered_sensors(
        config_entry_id: str,
        device_type: str,
        device_ids: set[str],
    ) -> None:
        """Add sensors for devices found by an hourly metadata refresh."""
        if config_entry_id != entry.entry_id or device_type not in description_factories:
            return
        async_add_entities(_create_sensors(device_type, device_ids))

    @callback
    def _async_add_metadata_descriptions() -> None:
        """Add entities for capabilities that appeared after setup."""
        new_sensors = [
            sensor
            for device_type in description_factories
            for sensor in _create_sensors(device_type, set(coordinator.data[device_type]))
        ]
        if new_sensors:
            async_add_entities(new_sensors)

    @callback
    def _async_add_inverter_descriptions() -> None:
        """Add phase and MPPT entities once live data exposes their dimensions."""
        new_sensors = _create_sensors(
            INVERTER_IDENTIFIER,
            set(coordinator.data[INVERTER_IDENTIFIER]),
        )
        if new_sensors:
            async_add_entities(new_sensors)

    entry.async_on_unload(async_dispatcher_connect(hass, DISCOVERY_SIGNAL, _async_add_discovered_sensors))
    entry.async_on_unload(coordinator.async_add_listener(_async_add_metadata_descriptions))
    if inverter_coordinator := entry.runtime_data.inverter_coordinator:
        entry.async_on_unload(inverter_coordinator.async_add_listener(_async_add_inverter_descriptions))

    # Always add entities. If the inverter is slow/sleeping at startup, filtering here would
    # permanently prevent entities from being created.
    async_add_entities(sensors)
