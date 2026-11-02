# Entities and Actions

We organise your system into Home Assistant devices. Open **Settings > Devices & services > Solplanet** to see the devices and their entities.

## What You Will See

Your hardware and firmware determine which entities are available. Home Assistant may also generate different entity IDs if names already exist or you rename an entity.

Primary power, energy, state-of-charge, control, warning, and error entities are enabled by default. Detailed phase, MPPT, line, EPS, network, and other high-frequency diagnostic entities are created disabled on new installations to reduce dashboard and recorder noise. To use one, open the device's **Entities** list, include disabled entities in the filter, and enable it. Existing entity-registry choices are preserved when upgrading.

### Inverter

Use the inverter device to monitor power, production, frequency, temperatures, working hours, errors, and phase or MPPT values. Supported systems also have a **Power** configuration switch.

Turning off **Power** can stop the inverter. Keep this switch away from general-purpose dashboards and automations unless you deliberately need it.

On V2 systems, **Grid status** uses the inverter's `grid_sts` JSON field when
provided. If it is missing, the integration reads input register **31391**
(function 04, offset 1390) at the inverter address from device discovery, using
the normal inverter polling interval and shared gateway queue. Values 1 and 0
mean connected and disconnected. Failed, unsupported or invalid reads leave
Grid status unknown without marking other telemetry as failed; no settings are
written. Explicit unsupported-function or unsupported-register replies are
retried after the existing 10-minute failure interval; temporary read errors
are retried on the next normal poll. A failed request can still hold the shared
queue until the configured request timeout and retries finish.

This describes the inverter's grid connection, which may also change during
deliberate Off-grid operation. It is not a verified indication of physical
utility voltage on every model. Events shorter than the polling interval may
be missed. If using this integrated fallback, remove any separate package
polling the same register to avoid duplicate requests.

### Battery

Use the battery device to monitor state of charge, state of health, charging or discharging power, current limits, communication status, warnings, and errors.

Common controls include:

- **Work mode**
- **SOC min** and **SOC max**
- **Schedule Input Power** and **Schedule Output Power**
- **Power** and **Sleep**
- **Schedule Configured**

Supported ASW5120-LB-G3 and RV5120-LB-G3 batteries also expose LED colour and brightness controls.

Changing **Power**, **Sleep**, or state-of-charge settings affects battery operation. Test changes while you can observe the system.

### Meter

Use the main meter device to monitor grid power, voltage, current, power factor, and imported or exported energy. On supported V2 systems, the diagnostic **Power limit control** entity shows whether control is **Disabled**, **Limit power**, **Limit current**, or **Zero power**.

A discovered sub-meter may appear as a device without entities. We currently expose live V2 readings and power-limit controls only for the main meter.

We do not create a house-load entity. Calculating house load from inverter, battery, and meter values can be inaccurate when readings arrive at different times or the installation has a different meter topology.

### Dongle

Supported V2 dongles expose network details such as IP address, Wi-Fi signal, and warnings. They can also provide **Sync time** and **Reboot** buttons.

## Actions

We use Home Assistant actions for battery schedules and meter power-limit control. To try one:

1. Open **Developer Tools > Actions**.
2. Search for **Solplanet**.
3. Select an action and complete its fields.
4. Select **Perform action**.

Available actions are:

| Action name | Action ID | Use |
| --- | --- | --- |
| Set Schedule Slots | `solplanet.set_schedule_slots` | Add one charge or discharge period |
| Clear Schedule | `solplanet.clear_schedule` | Clear one day or the full schedule |
| Enable power limit control (Limit power) | `solplanet.set_meter_limit_power` | Limit grid export by watts or percentage |
| Enable power limit control (Limit current) | `solplanet.set_meter_limit_current` | Limit inverter grid import and export current |
| Enable power limit control (Zero power) | `solplanet.set_meter_zero_power` | Ask the inverter to avoid grid export |
| Disable power limit control | `solplanet.disable_meter_power_limit` | Return power-limit control to disabled |
| Configure power limit control (404 compatibility) | `solplanet.set_compatibility_meter_power_limit` | A separate action for simpler percentage-based firmware; use only when the standard action returns HTTP 404 |

For schedule actions, target the battery's **Schedule Configured** entity. For every power-limit action, select the **main meter device** in the **Meter device** field. An inverter, battery, or dongle will be rejected. A sub-meter is not an independent control target and may send the same system-wide setting, so do not select one. **Configure power limit control (404 compatibility)** is a separate Home Assistant action; run it only when the standard **Limit power** action fails with HTTP 404.

See [Battery Modes and Schedules](Battery-Modes-and-Schedules) and [Power Limit Control](Power-Limit-Control) before using these actions in an automation.

## Dashboards And Automations

Start with a simple dashboard showing inverter power, battery state of charge, battery power, meter power, and any warning or error entities. Observe the sign and behaviour of your own meter values before using them as automation triggers.

Once a manual action works, add the same action to a Home Assistant script or automation. Include an explicit cleanup action when appropriate, such as disabling a temporary export limit at the end of an export window.
