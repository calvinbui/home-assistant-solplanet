# Battery Modes and Schedules

Use the battery's **Work mode** entity to choose how it operates. Only select a mode supported by your inverter and battery documentation.

## Choose A Work Mode

| Mode | When to use it |
| --- | --- |
| Self-consumption mode | Use solar and battery energy to reduce normal grid consumption. |
| Reserve power mode | Keep more battery energy available as a reserve. |
| Custom mode | Follow charge and discharge periods that you define. |
| Off-grid mode | Operate according to the hardware's supported off-grid behaviour. Do not select this just to disconnect from the Solplanet cloud. |
| Time of use mode | Use the time-of-use settings configured by your system or Solplanet app. |

Our integration can select all five modes, but your hardware may not support every mode. It manages Custom mode schedule slots directly; it does not expose the Solplanet app's full Time of Use configuration.

## Create A Custom Schedule

Custom mode is useful when you want to charge during a cheap tariff, discharge during an expensive period, or prepare the battery for a planned export window.

For the ASW008K/010K-SH, Solplanet documents that the inverter operates in Self-consumption mode outside configured charge and discharge periods. You do not need an automation to switch modes or set the schedule powers back to 0 at each boundary. Check the inverter manual for other models.

The ASW008K/010K-SH manual also says Custom mode requires the Ai-Dongle to maintain its normal network connection. App-only operation means Home Assistant is not required; it does not mean the dongle can be removed or left disconnected. Requirements for other models may differ.

1. Open the battery device in Home Assistant.
2. Set **Schedule Input Power** to the charging power you want in watts.
3. Set **Schedule Output Power** to the discharging power you want in watts.
4. Set **Work mode** to **Custom mode**.
5. Open **Developer Tools > Actions**.
6. Select **Solplanet: Set Schedule Slots**.
7. Target the battery's **Schedule Configured** binary sensor.
8. Choose the day, start time, duration, and whether the slot should charge or discharge.
9. Perform the action once for each slot you need.

Wait for a successful integration update before changing a schedule, and first confirm that the existing schedule is visible in the Solplanet app. If the dongle's schedule data has not loaded, do not run a schedule action because existing slots may not be retained.

Each **Set Schedule Slots** call adds one slot to the schedule currently loaded by the integration; it does not intentionally replace existing slots. You can add up to six non-overlapping slots per day. A slot can start on the hour or half-hour and last one to four whole hours. Keep every slot within one day and do not use a duration that carries it past midnight.

For automation-managed schedules, use **Replace Schedule Day** to send the complete list of slots for one day in one Home Assistant action. The integration reads the current schedule immediately before writing, keeps the other days, and can include optional Schedule Input Power (`pin`) and Schedule Output Power (`pout`) values in the same API write. Use an empty `slots` list to clear only the selected day. This removes the empty intermediate schedule created by separate clear-and-add actions. The integration makes one device write, but cannot guarantee that inverter firmware applies that write transactionally.

Each replacement slot has `start_hour` (0-23), `start_minute` (0 or 30), `duration` (1-4 whole hours), and `mode` (`charge` or `discharge`). A day can contain at most six non-overlapping slots; no slot may cross midnight. Optional power values must be between 0 and 10000 W in 100 W steps.

For example, the action data below replaces Monday with one charging and one discharging period:

```yaml
action: solplanet.replace_schedule_day
target:
  entity_id: binary_sensor.solplanet_schedule_configured
data:
  day: Mon
  slots:
    - start_hour: 3
      start_minute: 0
      duration: 2
      mode: charge
    - start_hour: 18
      start_minute: 30
      duration: 3
      mode: discharge
```

The current action labels Tuesday as `Tus` and Wednesday as `Wen`. These labels are expected by the dongle schedule format.

The Solplanet system exposes one schedule for the system. Targeting a battery identifies the correct integration, but it does not create a separate schedule for each battery attached to the same inverter.

To remove slots, run **Solplanet: Clear Schedule** against the same **Schedule Configured** entity. Select one day or `all`.

## Create The Same Schedule In The Solplanet App

If you prefer the app:

1. Select `+` and **Configure Parameters**.
2. Scan the dongle QR code, or load a saved photo.
3. Select **Network** or **Point-to-point**, as appropriate, while your phone can reach the dongle.
4. Select the inverter.
5. Open **Energy Storage Settings > Battery Settings > Custom Mode**.
6. Set the charging power and each charge period.
7. Set the discharging power and each discharge period.
8. Save the settings and confirm **Custom Mode** is the active work mode.

Some app versions and account types expose battery settings directly from the plant or inverter. You can use that route when available. Installer or local configuration permissions may be required for some settings, especially E-meter export control.

Saving a schedule does not activate it unless the battery is also in Custom mode.

Once the fixed schedule is saved and Custom mode is active, Home Assistant is not required to run it. Avoid duplicate boundary actions that change **Work mode**, **Schedule Input Power**, or **Schedule Output Power** for the same periods. A separate Power Limit Control automation remains appropriate when the export ceiling is intentionally temporary.

## Choose An App-Only Or Home Assistant Design

- **Fixed schedule only:** Configure charge and discharge periods in the app. Grid export varies with house load because Schedule Output Power is total battery output.
- **Fixed schedule plus permanent export ceiling:** Configure Custom mode in the app and a persistent E-meter export limit. This can hold net export near the target while allowing the battery to cover changing house load.
- **Home Assistant control:** Use it when the E-meter limit must change by time, when you want SOC or price conditions, or when a tested firmware issue requires a permitted temporary workaround.

If a permanent export ceiling works correctly during both grid charging and battery discharge, Home Assistant is not required for a fixed daily schedule.

## Understand Charge, Discharge, And Export

Scheduled battery discharge power is not the same as grid export. Your house usually consumes battery power first, and only the remainder reaches the grid. A changing house load therefore changes export even when the battery discharge setting stays fixed.

If you need to cap export, use [Power Limit Control](Power-Limit-Control) as well as the battery schedule.

When Power Limit Control is active, **Schedule Output Power** can be set high enough to cover both the house and the grid-export target, up to the lowest supported inverter, battery/BMS, and installation rating. Without Power Limit Control, lower the discharge power and tune it from observed meter export if you prefer a simpler approximate result.

## Reserve Battery Capacity

Use **SOC min** to keep part of the battery in reserve. For example, setting it to 45% should stop normal discharge near 45%, subject to the inverter's own safety behaviour. Lower it later when you want to make that capacity available again.

Use **SOC max** to limit the normal charging target. Do not use either value to override battery protection limits.

## If A Schedule Does Not Work

- Confirm **Custom mode** is still selected.
- Confirm the slot was added to the intended day and did not overlap another slot.
- Check **Schedule Input Power** and **Schedule Output Power**.
- Check battery communication, warning, and error entities.
- If an optional temporary Power Limit Control setting appears to block charging, disable it and re-test. Do not remove an approved site export limit without installer or distributor guidance.
- Clear the affected day and recreate its slots if the app and Home Assistant disagree.

## Official References

- [ASW008K/010K-SH User Manual](https://solplanet.net/wp-content/uploads/2025/03/UM0060_ASW008K-010K-SH_EN_V01_0325.pdf)
- [Solplanet App User Manual](https://solplanet.net/wp-content/uploads/2025/11/UM0072_Solplanet-App_EN_V02.pdf)
