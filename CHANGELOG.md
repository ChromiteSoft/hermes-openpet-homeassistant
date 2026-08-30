# Changelog

All notable changes to this project are documented here.

## 0.2.0 (2026-08-30)

### Changed
- Renamed plugin from `openpet-ha` to `hermes-openpet-homeassistant` for consistency with the broader `hermes-openpet-*` family
- Updated `plugin.yaml` with full metadata (`homepage`, `repository`, `platforms`, `python_dependencies`, `requires_env`)
- README now follows the unified style shared with `hermes-openpet-direct`
- Logger name changed from `openpet-ha` to `hermes-openpet-homeassistant`

### Fixed
- Blueprint `source_url` and OpenPets badge now point to the real `https://github.com/ChromiteSoft/hermes-openpet-homeassistant` repo

## 0.1.0 (2026-08-30)

### Added
- Initial release: real-time WebSocket listener for Home Assistant `state_changed` events
- Persistent TCP client for OpenPets Desktop gateway (default port 18420)
- Zero LLM, fully deterministic event router with debounce
- Official Home Assistant Blueprint supporting both state-based and numeric threshold triggers
- `input_text.openpet_message` helper interface for unified automation authoring
- Fallback event patterns for common entity types (doors, windows, motion, lights, fans, persons)
- 146/146 unit tests passing (event_map: 61, pet_sender: 32, ha_ws: 21, init: 32)
- Replaces the legacy external `bridge-daemon.py` HTTP webhook approach
