# Troubleshooting

## Integration Cannot Connect

1. Confirm the dongle is powered and still appears in your router's connected-device list.
2. Enter only its IP address or hostname in Home Assistant, without a protocol or port.
3. Confirm Home Assistant can reach TCP port 443 or 8484 on the dongle. Ping is not required.
4. Check firewall rules, guest Wi-Fi isolation, and VLAN routing.
5. Confirm only one Solplanet custom integration is installed.
6. If you moved from the original integration, remove its Marketplace or HACS repository before reinstalling this one.
7. Run the tests in [Advanced Local Access](Advanced-Local-Access#test-the-connection).

A failed HTTPS test does not mean the dongle is unsupported. It may use HTTP on port 8484 instead.

## Dongle Address Or Wi-Fi Is Missing

- Check your router's DHCP client list and create a reservation when you find the dongle.
- Stand close to the dongle if you are looking for its temporary Wi-Fi access point.
- Open the Solplanet app's **Configure Parameters** flow and load a saved QR-code photo if the physical label is difficult to scan.
- Check the app's dongle details for the current IP address.

Some dongles hide their access point after joining your home Wi-Fi. This does not prevent local access through the home network.

## Data Is Slow Or Occasionally Missing

The live data update interval defaults to 60 seconds. Inverter, battery, meter, and dongle-warning data have separate pollers, while device inventory and settings refresh hourly. All of the integration's requests share one queue so the dongle receives only one request at a time. A slow request can therefore delay another poller, and related entities may update at slightly different times.

- Keep the default interval while diagnosing the problem.
- Remove duplicate REST sensors or other software polling the dongle.
- On V2 systems, check **WiFi signal strength** and improve coverage if the signal is weak.
- Check the dongle **Warnings** entity.
- If the **Reboot** button is available, restart the dongle only after allowing a normal update to finish.
- Review **Settings > System > Logs** and search for `solplanet`.

An individual value may show `unknown` while its device is sleeping or that field is absent from an otherwise successful response. If one inverter or battery fails while its peers update, only that device's live entities become unavailable. If an entire endpoint family fails, all entities supplied by that poller become unavailable, while the other pollers continue updating. They recover automatically after a successful response.

After three consecutive full failures, the affected live-data poller waits at least 10 minutes between attempts instead of repeatedly loading an unresponsive dongle. It returns to your configured interval as soon as an update succeeds. Transient all-zero battery responses are ignored so they do not replace the last valid readings. If every battery response is all-zero for three consecutive updates, the battery poller uses the same backoff.

## Energy Now View Is Empty

The `/energy/now` view shows the live power flow between solar, battery, grid, and home. If it is empty while `/energy/electricity` shows data, the live flow cannot be drawn because no power source has a power entity configured.

To diagnose:

1. Open **Settings > Dashboards > Energy** and edit each configured source.
2. Confirm the **Power measurement** field is filled with the power entity for that source (see [Energy Dashboard](Energy-Dashboard) for the recommended entities).
3. Confirm the power entity has a current value in **Settings > Developer tools > States** and its `device_class` is `power`.
4. Confirm the Recorder is not excluding that entity.
5. If you recently added the entity, wait. Home Assistant displays the message "After setting up a new device, it can take up to 2 hours for new data to arrive in your energy dashboard."
6. Check **Settings > Dashboards > Energy > Edit dashboard > Customize cards** and confirm the **Now** section has both the **power-sources-graph** and **power-sankey** cards enabled.

If the energy (kWh) fields are configured but the **Power measurement** fields are empty, the historical `/energy/electricity` view works and the live `/energy/now` view stays empty. This is by design, not a bug in the integration.

## An Action Fails

For battery schedule actions, target the battery's **Schedule Configured** binary sensor.

For power-limit actions, use the **Meter device** field and select the main meter. The action will reject an inverter, battery, or dongle. Do not select a sub-meter because it is not an independent control target and may send the same system-wide setting. Power-limit actions also require a supported V2 system.

## Meter Values Look Wrong

- Compare **Meter power**, or **Grid power** on a V1 system, with the Solplanet app while turning a known appliance on and off.
- Confirm whether your installation treats positive power as import or export before building automations.
- Check for a reversed or incorrectly positioned CT clamp.
- Check whether controlled loads sit inside or outside the meter's measurement point.
- Confirm the integration is using the main meter you expect.
- Remember that V2 sub-meters can appear as devices without live entities.

We do not expose a native house-load sensor. If a calculated house-load value looks wrong, the calculation may not match your system topology or its source entities may have updated at different times.

Meter and CT issues should be checked by a qualified electrician or installer.

## Battery Will Not Charge Or Discharge Fully

- Check **Work mode**, **SOC min**, **SOC max**, and any active schedule.
- Check whether **Power limit control** is active on the meter.
- Review battery **Communication status**, **Battery errors**, and **Battery warnings**.
- Confirm all battery stacks show consistent status lights and are contributing power.
- Check whether the reported charge or discharge current limit is unexpectedly low.
- Ask your installer to check DC cables, combiner-box connections, and RJ45 communication cables.

Stack, cable, or combiner-box faults can reduce the available current without being caused by Home Assistant.

## W177 Battery Communication Error

W177 can be expected while the battery is intentionally powered off. It should clear after the battery is powered on and the inverter detects it again. If it remains, check the communication status and contact your installer.

## Flashing Yellow Battery Light

A flashing yellow light commonly indicates lost battery communication. Check the Home Assistant communication and warning entities, then ask your installer to inspect RJ45 connectors and stack interconnects if the warning persists.

## Power Limit Control Stops Charging

- First confirm whether the limit is optional or is required by the installer or distributor for connection approval.
- For an optional temporary limit, run **Disable power limit control** against the main meter before the charging window and re-test.
- Do not disable an approved site export limit as a workaround. Ask the installer or Solplanet to correct the charging behaviour.
- If disabling fails while using absolute **Limit power**, set **Export power limit setpoint (W)** to 0 without changing the other required fields, then try disabling it again. This workaround does not apply to **Limit current** or **Zero power**.
- If available, check that the meter's **Power limit control** diagnostic entity reads **Disabled**.
- For a temporary tariff limit, restrict the automation to the required export window and add a disable action at the end. Leave a permanent approved limit enabled.
- Re-test after firmware or Solplanet-side changes.

This behaviour varies by inverter and firmware.

## Firmware And Remote Changes

Our integration does not install inverter, battery, or dongle firmware. Depending on your account, the Solplanet app may offer a battery update; inverter updates are normally managed by Solplanet or an installer.

After any firmware or remote configuration change, re-check charging, discharging, work modes, schedules, Power Limit Control, and meter values before relying on your automations again.
