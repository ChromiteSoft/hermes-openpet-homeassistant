"""openpet-ha — Animate OpenPets pet from Home Assistant state changes via WebSocket.

Plugin loads from ~/.hermes/plugins/openpet-ha/ and is enabled via
``plugins.enabled: [openpet-ha]`` in ~/.hermes/config.yaml.

Pipeline (NO LLM, all deterministic):

  HA state_changed event
        ↓
  ha_ws.HAWebSocket  (background thread, reconnects on error)
        ↓
  on_event(entity_id, new_state, old_state)
        ↓
  event_map.resolve_reaction(entity_id, new_state)  → (reaction, speech) or None
        ↓
  event_map.Debouncer.allow(entity_id, new_state)  → True/False
        ↓
  pet_sender.PetSender.send(reaction, speech)

Reads its config from two places:

- Connection settings (secrets): ~/.hermes/.env
    OPENPET_HA_HOST         (default: YOUR_PET_HOST)
    OPENPET_HA_PORT         (default: 18420)
    OPENPET_HA_CLIENT_ID    (default: hermes-ha)
    OPENPET_HA_TOKEN        (required; silent no-op if unset)
    OPENPET_HA_TIMEOUT      (default: 3.0 seconds)
    HASS_URL                (e.g. http://YOUR_HA_HOST:8123)
    HASS_TOKEN              (required for live subscription)

- Behaviour settings (user-tunable, auto-populated on first run):
    ~/.hermes/config.yaml → plugins.entries.openpet-ha.settings.*
        debounce_seconds       (default: 2.0)
        include_entities       (default: [] — empty = subscribe to ALL)
        say_enabled            (default: True)
        enabled                (default: True) — master switch

If the token is missing the plugin still loads — it becomes a no-op so
the gateway keeps working without OpenPets. Same for HASS_TOKEN.

This plugin lives in ~/.hermes/plugins/ (global), so it runs in every
profile's gateway.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger("openpet-ha")


# ── Defaults (written to config on first run; user-editable thereafter) ──
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "debounce_seconds": 2.0,
    "include_entities": [],   # [] = all entities; else: ["light.liustra", ...]
    "say_enabled": True,
}


# ── Env loading (mirrors the companion OpenPets plugin) ───────────────────────────────────
_ENV_PATH = Path(os.path.expanduser("~/.hermes/.env"))


def _load_env_file(path: Path) -> dict:
    """Minimal .env parser. Hermes' own format."""
    out: dict = {}
    if not path.exists():
        return out
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if (len(value) >= 2 and value[0] == value[-1]
                        and value[0] in ("'", '"')):
                    value = value[1:-1]
                out[key] = value
    except Exception as e:
        _log.warning("openpet-ha: failed to read %s: %s", path, e)
    return out


def _resolve_config() -> dict:
    """Pull all needed config from env + .env file.

    Falls back to OPENPET_REMOTE_* (set by the companion OpenPets plugin) when
    OPENPET_HA_* is not present, so users only configure OpenPets once.
    """
    env = _load_env_file(_ENV_PATH)
    def pick(key_ha: str, key_remote: str, default: str = "") -> str:
        """OPENPET_HA_* wins, else OPENPET_REMOTE_*, else default."""
        return (os.environ.get(key_ha)
                or env.get(key_ha)
                or os.environ.get(key_remote)
                or env.get(key_remote)
                or default)
    return {
        "host": pick("OPENPET_HA_HOST", "OPENPET_REMOTE_HOST", "YOUR_PET_HOST"),
        "port": int(pick("OPENPET_HA_PORT", "OPENPET_REMOTE_PORT", "18420")),
        "client_id": pick("OPENPET_HA_CLIENT_ID", "OPENPET_REMOTE_CLIENT_ID", "hermes-ha"),
        "token": pick("OPENPET_HA_TOKEN", "OPENPET_REMOTE_TOKEN", ""),
        "timeout": float(pick("OPENPET_HA_TIMEOUT", "OPENPET_REMOTE_TIMEOUT", "3.0")),
        "hass_url": (os.environ.get("HASS_URL")
                     or env.get("HASS_URL", "").rstrip("/")),
        "hass_token": (os.environ.get("HASS_TOKEN")
                       or env.get("HASS_TOKEN", "")),
    }


# ── Plugin state (one process) ────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "ws": None,            # HAWebSocket instance
    "sender": None,        # PetSender instance
    "debouncer": None,     # Debouncer instance
    "cfg": None,           # user behaviour config (from config.yaml)
    "started": False,
}


def _on_ha_event(entity_id: str, new_state: str, old_state: str) -> None:
    """Callback wired to HAWebSocket. Runs in the consumer thread.

    Pipeline:
      1. resolve_reaction(entity_id, new_state) → (reaction, speech) or None
      2. debouncer.allow(entity_id, new_state) → True if within window
      3. pet_sender.send(reaction, speech)
    """
    with _lock:
        cfg = _state["cfg"]
        debouncer = _state["debouncer"]
        sender = _state["sender"]
    if not cfg or not debouncer or not sender:
        return
    if not cfg.get("enabled", True):
        return

    # Lazy import to avoid module-level load cost
    import sys as _sys
    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    _em = _sys.modules.get("event_map")
    if _em is None:
        import importlib
        _em = importlib.import_module("event_map")
    resolve_reaction = _em.resolve_reaction

    result = resolve_reaction(entity_id, new_state)
    if result is None:
        return
    reaction, speech = result

    if not debouncer.allow(entity_id, new_state):
        _log.debug("openpet-ha: debounced %s→%s", entity_id, new_state)
        return

    if not cfg.get("say_enabled", True):
        speech = None  # user disabled speech bubbles
    sender.send(reaction, speech)
    _log.info("openpet-ha: %s %s→%s → %s%s",
              entity_id, old_state, new_state, reaction,
              f' "{speech}"' if speech else "")


# ── register() — called by Hermes when plugin loads ────────────────────────
def register(ctx) -> None:
    """Plugin entry point. Wires together ha_ws + event_map + pet_sender."""
    # 1) Populate defaults so user has a starting point in config.yaml
    for key, value in DEFAULTS.items():
        existing = ctx.get_config(key, default=None)
        if existing is None:
            try:
                ctx.set_config(key, value)
                _log.info("openpet-ha: wrote default %s to config.yaml", key)
            except PermissionError as e:
                _log.debug("openpet-ha: managed install, using in-memory default for %s: %s",
                           key, e)
            except Exception as e:
                _log.warning("openpet-ha: could not write default %s: %s", key, e)

    # 2) Read behaviour config
    cfg = {}
    for key, default in DEFAULTS.items():
        val = ctx.get_config(key, default=default)
        cfg[key] = val if val is not None else default
    with _lock:
        _state["cfg"] = cfg

    # 3) Read connection config
    conn = _resolve_config()

    if not conn["token"]:
        _log.info("openpet-ha: OPENPET_HA_TOKEN not set — plugin loaded as no-op. "
                  "Add it to ~/.hermes/.env and restart the gateway to enable reactions.")
    if not conn["hass_token"] or not conn["hass_url"]:
        _log.info("openpet-ha: HASS_URL/HASS_TOKEN not set — WebSocket listener disabled. "
                  "Add them to ~/.hermes/.env and restart the gateway to subscribe.")
        return

    if not conn["token"]:
        return  # can't send reactions even if we listen

    # 4) Lazy imports. The plugin's dir may not be on sys.path when the
    # gateway loads us, so we ensure it is before importing siblings.
    import importlib
    import sys as _sys
    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    _pet_sender = importlib.import_module("pet_sender")
    _ha_ws = importlib.import_module("ha_ws")
    _event_map = importlib.import_module("event_map")
    PetSender = _pet_sender.PetSender
    HAWebSocket = _ha_ws.HAWebSocket
    Debouncer = _event_map.Debouncer

    sender = PetSender(
        host=conn["host"],
        port=conn["port"],
        client_id=conn["client_id"],
        token=conn["token"],
        timeout=conn["timeout"],
    )
    debouncer = Debouncer(window_seconds=float(cfg.get("debounce_seconds", 2.0)))

    entity_filter = cfg.get("include_entities") or None
    if isinstance(entity_filter, str):
        # tolerate single string in config
        entity_filter = [entity_filter]
    if entity_filter is not None and not isinstance(entity_filter, list):
        _log.warning("openpet-ha: include_entities must be a list, got %s — ignoring",
                     type(entity_filter).__name__)
        entity_filter = None

    ws = HAWebSocket(
        url=conn["hass_url"],
        token=conn["hass_token"],
        on_event=_on_ha_event,
        entity_filter=entity_filter,
    )

    with _lock:
        _state["sender"] = sender
        _state["debouncer"] = debouncer
        _state["ws"] = ws

    _log.info("openpet-ha: enabled → %s:%d (clientId=%s, say=%s, filter=%s)",
              conn["host"], conn["port"], conn["client_id"],
              "on" if cfg.get("say_enabled", True) else "off",
              entity_filter or "ALL")

    ws.start()
    with _lock:
        _state["started"] = True


def unregister(ctx) -> None:  # pragma: no cover  (optional, called on disable)
    """Called when plugin is disabled / gateway shuts down."""
    with _lock:
        ws = _state["ws"]
        _state["ws"] = None
        _state["sender"] = None
        _state["debouncer"] = None
        _state["cfg"] = None
        _state["started"] = False
    if ws is not None:
        try:
            ws.stop()
        except Exception as e:
            _log.debug("openpet-ha: ws.stop error: %s", e)
