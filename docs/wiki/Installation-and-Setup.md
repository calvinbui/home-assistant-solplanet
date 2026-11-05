# Installation and Setup

## Before You Start

You need:

- Home Assistant 2026.3.0 or newer.
- A Solplanet inverter with a network-connected smart dongle.
- The IP address or hostname of the dongle.
- Network access from Home Assistant to the dongle.

You do not need your inverter serial number, a Solplanet cloud account, or installer credentials.

## Install With Marketplace

Home Assistant 2026.11 and newer includes Marketplace:

1. Open **Settings > Marketplace**.
2. Find **Solplanet**, or select **Add from link** and add `https://github.com/calvinbui/home-assistant-solplanet` if it is not listed.
3. Select **Install** and continue in the setup flow. A restart is not required for the first installation.

Updates still require a restart. Marketplace creates a repair with a restart button when one is needed.

## Install With HACS

Home Assistant 2026.10 and earlier can install the integration through HACS:

1. Open HACS.
2. Select the three-dot menu, then **Custom repositories**.
3. Add `https://github.com/calvinbui/home-assistant-solplanet` as an **Integration** repository.
4. Find and install **Solplanet**.
5. Restart Home Assistant when HACS asks you to.

If you are moving from another Solplanet custom integration, remove the old repository and integration first. Keeping both variants installed can cause Home Assistant to load the wrong files.

## Install Manually

1. Copy `custom_components/solplanet` from this repository to `config/custom_components/solplanet` in your Home Assistant configuration directory.
2. Restart Home Assistant.

## Add Your System

Home Assistant can automatically discover newer dongles that use AISWEI's registered network hardware range. If a **Solplanet** discovery card appears, select **Add**, verify the displayed address, and confirm setup. Older dongles may use third-party network hardware and still need manual setup.

To add the integration manually:

1. Open **Settings > Devices & services**.
2. Select **Add integration**.
3. Search for **Solplanet**.
4. Enter the dongle IP address or hostname without `http://`, `https://`, or a port number.
5. Leave the live data update interval at 60 seconds for your first setup.
6. Submit the form and wait for the first update to finish.

We automatically try the supported HTTPS and HTTP connection methods. When setup succeeds, we create devices for the inverter and any batteries, meters, or dongles that the system reports. The integration checks the device inventory hourly, so newly reported devices and newly exposed entities can appear without restarting Home Assistant. Devices confirmed absent from a successful inventory are removed from this config entry; devices omitted by an uncertain dongle or meter response are preserved.

The integration registers the dongle's available network MAC addresses with Home Assistant. Later DHCP discovery can therefore recognize the same configured system: a changed literal IP address is updated and the integration reloads automatically, while a hostname you entered is preserved.

After setup, open the new Solplanet devices and confirm that power and state-of-charge values update. Some values from sleeping hardware may show `unknown`; see [Troubleshooting](Troubleshooting) if the integration itself is unavailable.

## Find The Dongle Address

The easiest method is usually your router's connected-device or DHCP page. Look for a Solplanet/Aiswei device. A DHCP reservation remains useful, especially for older dongles that cannot be identified automatically.

You can also use the Solplanet app:

1. Open the app and select `+`.
2. Select **Configure Parameters**.
3. Scan the dongle QR code, or load a saved photo of it.
4. Select **Network** while your phone can reach the dongle.
5. Open the dongle details and note its IP address.

## Network Requirements

Home Assistant must be able to reach TCP port 443 or 8484 on the dongle. The devices can be on different VLANs if your firewall allows that traffic. Ping is not required and may be blocked even when the integration works.

The ASW-WLAN-G1 has been used successfully with this integration, but compatibility ultimately depends on the API exposed by its firmware. Our setup process detects the supported protocol rather than relying on the dongle model name.

The dongle may continue connecting to Solplanet cloud services while we use its local API. Blocking its internet access is optional and does not necessarily make local updates faster or more reliable. See [Advanced Local Access](Advanced-Local-Access) before running it offline.

## Change The Dongle Address Or Update Interval

Open **Settings > Devices & services > Solplanet** and select **Reconfigure** to change the dongle IP address or hostname. The integration connects to the new address and confirms that it belongs to the same Solplanet system before saving it.

The reconfigure form also accepts a new live-data interval. The allowed range is 10 to 3600 seconds, and the default is 60 seconds. You can change only the interval through **Configure** as well.

This interval controls live inverter, battery, meter, and dongle-warning data. Device inventory, identity, network details, schedules, and other settings refresh separately every hour. Supported setting changes also request a metadata refresh so their entities can update without waiting for the next hourly cycle.

The integration serializes all requests to the dongle, so its live-data pollers and writes do not overlap. Short intervals still add load and can cause slow or failed updates. After three consecutive full failures, the affected live-data poller waits at least 10 minutes between attempts; it returns to your configured interval after a successful update.

## Remove The Integration

1. Open **Settings > Devices & services**.
2. Open the **Solplanet** integration menu and select **Delete**.
3. If you installed it with Marketplace or HACS, remove the custom repository. If you installed it manually, delete `custom_components/solplanet` from the Home Assistant configuration directory.
4. Restart Home Assistant.
