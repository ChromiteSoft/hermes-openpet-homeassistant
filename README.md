# hermes-openpet-homeassistant

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Plugin-blue)](https://hermes-agent.nousresearch.com/docs)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Compatible-41BDF5)](https://www.home-assistant.io/)
[![OpenPets](https://img.shields.io/badge/OpenPets-Desktop-ff6b9d)](https://github.com/ChromiteSoft/hermes-openpet-homeassistant)
[![Companion: hermes-openpet-direct](https://img.shields.io/badge/Companion-hermes--openpet--direct-ff6b9d)](https://github.com/ChromiteSoft/hermes-openpet-direct)

A native **Hermes Agent** plugin that bridges **Home Assistant** events to your **OpenPets Desktop** companion in real time.

It listens to Home Assistant's WebSocket event stream and sends animation + speech frames to OpenPets via persistent TCP. **Zero polling, zero LLM overhead, sub-second latency** — fully deterministic, fully self-contained.

Pairs naturally with the [hermes-openpet-direct](https://github.com/ChromiteSoft/hermes-openpet-direct) plugin for agent activity → pet reactions. Configure OpenPets once — both plugins share the same token and connection.

---

## 🌟 Features

- 🚀 **Real-time WebSocket** — subscribes to HA's `state_changed` (no polling)
- 🧠 **Zero LLM** — pure deterministic mapping, ~50 ms reaction time
- 🧩 **Single HA interface** — set `input_text.openpet_message` from any automation
- 🛠 **Official HA Blueprint** — easy UI-driven setup for any sensor (states or numeric thresholds)
- 📦 **Self-contained** — replaces legacy webhook daemons, runs inside the Hermes gateway

---

## ⚙️ Requirements

- **Hermes Agent** ≥ 0.3 with plugin support
- **Home Assistant** with a long-lived access token
- **OpenPets Desktop** with the remote-control gateway running (default TCP `18420`)
- Python package: `websockets >= 13.0` (already in the Hermes venv)

---

## 📦 Installation

### 1. One-line install (recommended)

```bash
hermes plugins install ChromiteSoft/hermes-openpet-homeassistant --enable
```

After install, set `HASS_TOKEN` and `OPENPET_HA_TOKEN` in `~/.hermes/.env`, then restart the gateway:

```bash
systemctl --user restart hermes-gateway-<profile>.service
```

### 2. Manual install

```bash
git clone https://github.com/ChromiteSoft/hermes-openpet-homeassistant
mkdir -p ~/.hermes/plugins/
cp -r hermes-openpet-homeassistant ~/.hermes/plugins/hermes-openpet-homeassistant
hermes plugins enable hermes-openpet-homeassistant
```

### 3. Enable in `~/.hermes/config.yaml`

```yaml
plugins:
  enabled:
    - hermes-openpet-homeassistant
```

### 4. Configure secrets in `~/.hermes/.env`

```env
# Home Assistant
HASS_URL="http://YOUR_HA_HOST:8123"
HASS_TOKEN="YOUR_LONG_LIVED_ACCESS_TOKEN"

# OpenPets Desktop
OPENPET_HA_HOST="YOUR_PET_HOST_IP"
OPENPET_HA_PORT=18420
OPENPET_HA_CLIENT_ID="hermes-ha"
OPENPET_HA_TOKEN="YOUR_OPENPETS_TOKEN"
```

If you also use the companion `hermes-openpet-direct` plugin, the `OPENPET_HA_*` variables automatically fall back to the `OPENPET_REMOTE_*` ones, so you configure OpenPets only once.

### 5. Install the Home Assistant helper

In **Settings → Devices & Services → Helpers → Create Helper → Text**:
- **Name:** `OpenPet Message`
- **Entity ID:** `input_text.openpet_message`

### 6. Install the Blueprint (optional but recommended)

Copy `blueprints/automation/openpet_message.yaml` into your HA config:

```
/config/blueprints/automation/openpet_message.yaml
```

Then restart Home Assistant. The blueprint will appear under **Settings → Automations & Scenes → Blueprints** as **"OpenPets — Pet Reaction"**.

---

## 🎮 Usage

### From the Blueprint (easiest)

1. **Settings → Automations & Scenes → Create Automation → Use Blueprint**
2. Pick **"OpenPets — Pet Reaction"**
3. Fill in the form:
   - **Device or sensor** — any HA entity
   - **When should the pet react?** — pick `States` or `Numeric thresholds`
   - Pick the matching value (e.g. `on`, or `Above 1000`)
   - Pick an **Animation** and (optionally) a **Speech bubble**
4. Save.

### From any HA automation

Anywhere in your HA setup (automation, script, dashboard button, voice command), call:

```yaml
service: input_text.set_value
target:
  entity_id: input_text.openpet_message
data:
  value: "celebrating|Laundry is done! 👕"
```

The plugin parses the value as **`reaction|speech`** (or just plain text — reaction defaults to `celebrating`).

---

## 🧠 Supported reactions

`idle`, `thinking`, `working`, `editing`, `running`, `testing`, `waiting`, `waving`, `success`, `error`, `celebrating`.

---

## 🛠 Fallback patterns (no HA setup needed)

If the event is not for `input_text.openpet_message`, the plugin still reacts to common entity patterns out of the box:

| Pattern                          | On                | Off                |
|----------------------------------|-------------------|--------------------|
| `binary_sensor.*door*`           | `waiting`         | `idle`             |
| `binary_sensor.*window*`         | `celebrating`     | `idle`             |
| `binary_sensor.*smoke*`          | `error`           | `success`          |
| `binary_sensor.*motion*`         | `waiting`         | `idle`             |
| `light.*`                        | `celebrating`     | `idle`             |
| `switch.*ventiliator*` / `fan.*` | `celebrating`     | `idle`             |
| `switch.*led*` / `*strichka*`    | `celebrating`     | `idle`             |
| `person.*` / `device_tracker.*`  | `waving`          | `waving`           |

---

## ⚙️ Plugin settings (auto-populated in `config.yaml`)

| Key               | Type    | Default | Description                                           |
|-------------------|---------|---------|-------------------------------------------------------|
| `enabled`         | bool    | `true`  | Master switch                                        |
| `debounce_seconds`| float   | `2.0`   | Skip duplicate (entity, state) events within window  |
| `include_entities`| list    | `[]`    | Empty = subscribe to all; otherwise filter list      |
| `say_enabled`     | bool    | `true`  | Send speech bubbles (set `false` for animations only) |

---

## 🔁 How it works

```
HA state_changed event
        ↓
ha_ws.HAWebSocket  (background thread, reconnects on error)
        ↓
event_map.resolve_reaction(entity_id, new_state)  → (reaction, speech) or None
        ↓
event_map.Debouncer.allow(entity_id, new_state)  → True/False
        ↓
pet_sender.PetSender.send(reaction, speech)
        ↓
OpenPets Desktop → animation + speech bubble
```

The pipeline is **fully deterministic** — no LLM is involved at any step.

---

## 📂 Repository structure

```text
hermes-openpet-homeassistant/
├── README.md
├── LICENSE
├── CHANGELOG.md
├── .gitignore
├── plugin.yaml                # Plugin manifest
├── __init__.py                # Plugin entry point
├── ha_ws.py                   # Home Assistant WebSocket client
├── pet_sender.py              # OpenPets TCP client
├── event_map.py               # Event router and debouncer
└── blueprints/
    └── automation/
        └── openpet_message.yaml   # HA Blueprint (UI) — State or Numeric
```

---

## 🔗 Companion plugin

This plugin shares OpenPets connection settings with **[hermes-openpet-direct](https://github.com/ChromiteSoft/hermes-openpet-direct)**, which animates the pet from Hermes agent activity. Install both to get:

- `hermes-openpet-direct` → pet reacts to **agent activity**
- `hermes-openpet-homeassistant` → pet reacts to **HA sensors** (doors, lights, motion, etc.)

The `OPENPET_HA_*` env vars auto-fall back to `OPENPET_REMOTE_*`, so you configure OpenPets only once.

---

## 🛡️ License

MIT — see [LICENSE](LICENSE).

## 🙏 Credits

- **Hermes Agent** by Nous Research
- **OpenPets** for the desktop companion
- **Home Assistant** for the open home-automation platform
