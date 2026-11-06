"""Tests for modern entity metadata, translations and registry defaults."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from homeassistant.helpers.entity import EntityCategory

from custom_components.solplanet import binary_sensor, button, number, select, sensor, switch
from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)
from custom_components.solplanet.entity import SolplanetEntity, get_entity_unique_id

from tests.helpers import FakeCoordinator

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "solplanet"


def _catalog() -> dict[str, list]:
    """Return descriptions for every static and dynamic entity family."""
    coordinator = FakeCoordinator()
    legacy_meter = "legacy-meter"
    coordinator.data[METER_IDENTIFIER][legacy_meter] = {
        "data": SimpleNamespace(pac=1, iet=2, oet=3, itd=4, otd=5),
        "info": {},
    }
    legacy_submeter = "legacy-submeter"
    coordinator.data[METER_IDENTIFIER][legacy_submeter] = {
        "data": SimpleNamespace(pac=1, iet=2, oet=3, itd=4, otd=5),
        "info": {},
        "is_submeter": True,
    }
    return {
        "binary_sensor": [
            *binary_sensor.create_battery_binary_sensors(coordinator, "bat-1"),
            *binary_sensor.create_inverter_binary_sensors(coordinator, "inv-1"),
        ],
        "button": button.create_dongle_entities_description(coordinator, "dongle-1"),
        "number": number.create_battery_entities_description(coordinator, "bat-1"),
        "select": select.create_battery_entities_description(coordinator, "bat-1"),
        "sensor": [
            *sensor.create_inverter_entities_description(coordinator, "inv-1"),
            *sensor.create_meter_entities_description(coordinator, "meter-1"),
            *sensor.create_meter_entities_description(coordinator, legacy_meter),
            *sensor.create_meter_entities_description(coordinator, legacy_submeter),
            *sensor.create_dongle_entities_description(coordinator, "dongle-1"),
            *sensor.create_battery_entities_description(coordinator, "bat-1"),
        ],
        "switch": [
            *switch.create_inverter_switches("inv-1"),
            *switch.create_battery_switches("bat-1"),
        ],
    }


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_entities_use_translated_entity_names() -> None:
    """Every entity uses modern device-prefixed translated naming."""
    coordinator = FakeCoordinator()
    description = sensor.create_inverter_entities_description(coordinator, "inv-1")[0]
    assert SolplanetEntity(description, "inv-1", coordinator).has_entity_name is True

    for descriptions in _catalog().values():
        for description in descriptions:
            assert description.translation_key
            assert not isinstance(description.name, str)


def test_translation_catalogs_are_complete_and_placeholders_match() -> None:
    """Runtime locales and the Core source catalog cover every description."""
    catalogs = {
        "en": _load_json(COMPONENT_DIR / "translations" / "en.json")["entity"],
        "strings": _load_json(COMPONENT_DIR / "strings.json")["entity"],
    }
    assert "entity" not in _load_json(COMPONENT_DIR / "translations" / "pl.json")

    for domain, descriptions in _catalog().items():
        expected_keys = {description.translation_key for description in descriptions}
        for catalog in catalogs.values():
            assert expected_keys == set(catalog[domain])

        for description in descriptions:
            placeholders = set(description.translation_placeholders or {})
            for catalog in catalogs.values():
                name = catalog[domain][description.translation_key]["name"]
                assert set(re.findall(r"\{([^{}]+)\}", name)) == placeholders


def test_exception_translation_catalogs_are_complete() -> None:
    """Every translated runtime exception is present in both English catalogs."""
    strings = _load_json(COMPONENT_DIR / "strings.json")["exceptions"]
    english = _load_json(COMPONENT_DIR / "translations" / "en.json")["exceptions"]

    assert english == strings
    assert set(strings) == {
        "absolute_power_target_required",
        "battery_coordinator_not_found",
        "battery_operation_failed",
        "battery_operation_unsupported_v1",
        "communication_error",
        "dongle_operation_unsupported_v1",
        "invalid_schedule",
        "inverter_power_failed",
        "lost_current_exceeds_output_current",
        "lost_power_exceeds_rated_power",
        "meter_coordinator_not_found",
        "meter_power_limit_unavailable",
        "meter_power_limit_unsupported_v1",
        "modbus_unsupported_v1",
        "no_valid_battery_targets",
        "no_valid_meter_targets",
        "percentage_power_target_required",
        "protocol_detection_failed",
        "reboot_dongle_failed",
        "schedule_slot_limit",
        "set_meter_power_limit_failed",
        "sync_time_failed",
        "target_exceeds_rated_power",
        "unexpected_meter_response",
        "update_failed",
    }
    assert all(set(entry) == {"message"} for entry in strings.values())


def test_icon_translation_keys_reference_translated_entities() -> None:
    """Central icon mappings only reference valid entity translation keys."""
    icons = _load_json(COMPONENT_DIR / "icons.json")["entity"]
    english = _load_json(COMPONENT_DIR / "translations" / "en.json")["entity"]

    for domain, mappings in icons.items():
        assert set(mappings) <= set(english[domain])
        assert all(value["default"].startswith("mdi:") for value in mappings.values())

    assert set(icons["button"]) == {"sync_time", "reboot"}
    assert set(icons["number"]) == {
        "schedule_input_power",
        "schedule_output_power",
        "led_brightness",
    }
    assert set(icons["select"]) == {"led_color"}
    assert set(icons["sensor"]) == {
        "wifi_ssid",
        "wifi_signal_strength",
        "warnings",
    }
    assert set(icons["switch"]) == {
        "inverter_power",
        "battery_power",
        "battery_sleep",
    }
    assert icons["number"]["schedule_input_power"]["default"] == "mdi:flash-triangle"
    assert icons["sensor"]["warnings"]["default"] == "mdi:alert-circle-outline"
    assert icons["switch"]["battery_sleep"]["default"] == "mdi:sleep"


def test_categories_and_default_enablement_follow_entity_policy() -> None:
    """Controls stay available while secondary telemetry defaults to disabled."""
    catalog = _catalog()

    for domain in ("button", "number", "select", "switch"):
        for description in catalog[domain]:
            assert description.entity_category is EntityCategory.CONFIG
            assert description.entity_registry_enabled_default

    by_key = {
        description.translation_key: description for description in catalog["sensor"]
    }
    disabled_measurements = {
        "frequency",
        "apparent_power",
        "reactive_power",
        "power_factor",
        "ac_phase_power",
        "ac_phase_reactive_power",
        "ac_phase_voltage",
        "ac_phase_current",
        "mppt_voltage",
        "mppt_current",
        "mppt_power",
        "line_neutral_voltage",
        "line_neutral_current",
        "line_neutral_active_power",
        "line_neutral_power_factor",
        "total_apparent_power",
        "total_reactive_power",
        "battery_voltage",
        "battery_current",
        "eps_voltage",
        "eps_current",
        "eps_frequency",
        "eps_reactive_power",
        "eps_phase_voltage",
        "eps_phase_current",
        "eps_phase_power",
        "eps_phase_reactive_power",
    }
    for key in disabled_measurements:
        assert not by_key[key].entity_registry_enabled_default
        assert by_key[key].entity_category is None

    disabled_diagnostics = {
        "temperature",
        "total_working_hours",
        "battery_temperature",
        "network_mode",
        "wifi_ssid",
        "wifi_signal_strength",
        "ip_address",
        "gateway",
        "netmask",
    }
    for key in disabled_diagnostics:
        assert by_key[key].entity_category is EntityCategory.DIAGNOSTIC
        assert not by_key[key].entity_registry_enabled_default

    enabled_actionable = {
        "inverter_status",
        "error_code",
        "warnings",
        "communication_status",
        "battery_status",
        "battery_errors",
        "battery_warnings",
        "power_limit_control",
    }
    for key in enabled_actionable:
        assert by_key[key].entity_category is EntityCategory.DIAGNOSTIC
        assert by_key[key].entity_registry_enabled_default

    enabled_primary = {
        "power",
        "energy_produced_total",
        "meter_power",
        "grid_power",
        "pv_power",
        "battery_power",
        "battery_state_of_charge",
        "eps_power",
        "eps_energy_total",
    }
    assert all(by_key[key].entity_registry_enabled_default for key in enabled_primary)


def test_translation_metadata_does_not_change_unique_ids() -> None:
    """Translation metadata remains independent from stable entity identity."""
    device_ids = {
        INVERTER_IDENTIFIER: "inv-1",
        BATTERY_IDENTIFIER: "bat-1",
        METER_IDENTIFIER: "meter-1",
        DONGLE_IDENTIFIER: "dongle-1",
    }

    def _device_id(description) -> str:
        if description.data_field_device_type == METER_IDENTIFIER:
            for meter_id in ("meter-1", "legacy-meter", "legacy-submeter"):
                if description.key.startswith(f"{meter_id}_"):
                    return meter_id
        return device_ids[description.data_field_device_type]

    for domain, descriptions in _catalog().items():
        unique_ids = {
            get_entity_unique_id(
                description,
                _device_id(description),
            )
            for description in descriptions
        }
        assert len(unique_ids) == len(descriptions), domain

    inverter_power = next(
        item for item in _catalog()["sensor"] if item.key == "inv-1_pac"
    )
    battery_soc = next(
        item for item in _catalog()["sensor"] if item.key == "bat-1_soc"
    )
    assert get_entity_unique_id(inverter_power, "inv-1") == "solplanet_inv-1_pac"
    assert get_entity_unique_id(battery_soc, "bat-1") == "solplanet_battery_bat-1_soc"
