# Solplanet / VoltX Inverter Integration

This integration polls a local [Solplanet inverter dongle](https://solplanet.net/au/products/ai-dongle) (including [VoltX-branded devices](https://voltxenergy.com.au/)) and exposes Inverter, Battery and Smart Meter information for Home Assistant.

This repository is a fork of the upstream integration by `zbigniewmotyka`, with a primary focus on **V2 devices/firmware**.

> [!Important]
> This fork does not aim to add new features or fixes for **V1**.

## Features

- Supports single-phase and three-phase inverters
- Sensors for inverter, battery and smart meter/s
- Battery mode control
- Battery schedule management (set/clear schedule slots)
- V2 meter “power limit control” (Limit power / Limit current / Zero power)
- Modbus RTU-over-HTTP (advanced service for writing holding registers)
- Designed to reduce device “flapping” (serialized polling + graceful degradation on timeouts)

## Installation

#### With HACS

[![Open in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=calvinbui&repository=home-assistant-solplanet&category=integration)

#### Manual installation

1. Place `solplanet` directory inside `config/custom_components` directory
2. Restart Home Assistant.

## Setting Up

1. Add Solplanet from the Integration page.
2. Enter the IP address or hostname of your Solplanet dongle
3. The integration will do the rest.
