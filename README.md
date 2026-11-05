# Solplanet / VoltX Inverter Integration

This custom integration connects Home Assistant directly to a local [Solplanet inverter dongle](https://solplanet.net/au/products/ai-dongle), including compatible [VoltX-branded systems](https://voltxenergy.com.au/). It provides inverter, battery, and grid data alongside supported battery and power controls.

This repository is a fork of the upstream integration by [`zbigniewmotyka`](https://github.com/zbigniewmotyka/home-assistant-solplanet), with a primary focus on **V2 devices and firmware**.

> [!IMPORTANT]
> V1 devices have limited support. New features and fixes primarily target V2 systems.

## Features

- Local inverter, battery, meter, and dongle monitoring
- Battery work-mode and state-of-charge controls
- Custom charge and discharge schedules
- V2 grid power-limit controls
- Single-phase and three-phase inverter support

## Requirements

- Home Assistant 2026.3.0 or newer
- A compatible Solplanet inverter and network-connected smart dongle
- The dongle IP address or hostname, reachable from Home Assistant

<!-- ha-marketplace:exclude-start -->
## Installation

### With Marketplace or HACS

Home Assistant 2026.11 and newer includes Marketplace under **Settings > Marketplace**. Earlier versions can install the integration through HACS.

[![Open in Marketplace or HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=calvinbui&repository=home-assistant-solplanet&category=integration)

### Manual Installation

1. Copy `custom_components/solplanet` into `config/custom_components/solplanet` in your Home Assistant configuration directory.
2. Restart Home Assistant.
<!-- ha-marketplace:exclude-end -->

## Documentation

- [Complete Wiki](https://github.com/calvinbui/home-assistant-solplanet/wiki)
- [Installation and Setup](https://github.com/calvinbui/home-assistant-solplanet/wiki/Installation-and-Setup)
- [Quick Start: Charge or Export on a Schedule](https://github.com/calvinbui/home-assistant-solplanet/wiki/Quick-Start)
- [Energy Dashboard](https://github.com/calvinbui/home-assistant-solplanet/wiki/Energy-Dashboard)
- [Troubleshooting](https://github.com/calvinbui/home-assistant-solplanet/wiki/Troubleshooting)

## Safety

Some controls can change inverter, battery, grid import, and grid export behaviour. Read the Wiki guidance and confirm your installation requirements before using them.
