"""
event_map.py — Home Assistant → OpenPets reaction mapping.

NO LLM, NO network. Pure deterministic lookup.

Design philosophy (v0.2):
  The user's primary way to drive the pet is via a single special
  input_text entity in Home Assistant:

      input_text.openpet_message

  Set its value to either:
    1) "reaction|text"  e.g. "celebrating|Готово!"
    2) "text"           e.g. "Готово!"  → reaction defaults to "celebrating"

  Valid reactions (exactly 11, user-verified 2026-08-28):
    idle, thinking, working, editing, running, testing,
    waiting, waving, success, error, celebrating

  This means the user can drive the pet from ANY HA automation, script,
  or dashboard without touching plugin code. New scenarios = 0 lines.

Fallback for popular entities (works out of the box, no HA setup):
  binary_sensor.*door* / *dveri*  → waiting (on) / idle (off)
  binary_sensor.*window* / *vikno* → celebrating (on) / idle (off)
  light.*                          → celebrating (on) / idle (off)
  fan.* / switch.*ventiliator*     → celebrating (on) / idle (off)
  switch.*led* / switch.*strichka* → celebrating (on) / idle (off)
  binary_sensor.*smoke* / *dymu*   → error (on) / success (off)
  person.*                         → waving (home) / waving (not_home)

Pattern matching is case-insensitive and operates on the entity_id.
"""

from __future__ import annotations

import re
import time
import threading
from typing import Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# 1. Whitelist of valid OpenPets reactions (exactly 11)
# ═══════════════════════════════════════════════════════════════════════════
VALID_REACTIONS: frozenset[str] = frozenset({
    "idle", "thinking", "working", "editing", "running", "testing",
    "waiting", "waving", "success", "error", "celebrating",
})

# ═══════════════════════════════════════════════════════════════════════════
# 2. The single special entity that drives the pet from HA
# ═══════════════════════════════════════════════════════════════════════════
# Set this input_text in any HA automation; the plugin will react.
OPENPET_MESSAGE_ENTITY: str = "input_text.openpet_message"
# Default reaction when the value has no "reaction|" prefix.
DEFAULT_REACTION: str = "celebrating"
# Separator between reaction and text. Using "|" because it is
# rarely typed in natural-language messages.
SEPARATOR: str = "|"


# ═══════════════════════════════════════════════════════════════════════════
# 3. parse_message_value() — pure parser
# ═══════════════════════════════════════════════════════════════════════════
def parse_message_value(value: str) -> Optional[Tuple[str, str]]:
    """Parse an input_text value into (reaction, speech).

    Accepts:
      "celebrating|Готово!"   → ("celebrating", "Готово!")
      "celebrating|Hi 👋"      → ("celebrating", "Hi 👋")
      "Готово!"                → ("celebrating", "Готово!")   # default reaction
      "   "                    → None  (whitespace-only → skip)
      ""                       → None  (empty → skip)
      "|Готово!"               → ("celebrating", "Готово!")   # empty reaction → default
      "invalid|Hi"             → None  (invalid reaction → skip)
      "celebrating|"           → ("celebrating", "")           # empty speech OK
      "celebrating"            → ("celebrating", "")           # just reaction

    Returns None when the value should be skipped entirely (empty,
    whitespace-only, or invalid reaction).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if SEPARATOR in s:
        reaction_raw, _, text = s.partition(SEPARATOR)
        reaction = reaction_raw.strip().lower()
        text = text.strip()
        if not reaction:
            reaction = DEFAULT_REACTION
        elif reaction not in VALID_REACTIONS:
            return None
        return reaction, text
    # No separator:
    #   - If the whole value is a valid reaction → (reaction, "")
    #   - Otherwise → (default_reaction, value)
    if s.lower() in VALID_REACTIONS:
        return s.lower(), ""
    return DEFAULT_REACTION, s


# ═══════════════════════════════════════════════════════════════════════════
# 4. Pattern-based fallback for popular entities (no HA setup needed)
# ═══════════════════════════════════════════════════════════════════════════
# Order matters: first matching pattern wins.
PATTERN_MAP: list[Tuple[re.Pattern, dict]] = [
    # Door (binary_sensor with "door" or "dveri" in id)
    (re.compile(r"binary_sensor\..*d(?:veri|oor)", re.IGNORECASE), {
        "on": ("waiting", "🚪 Knock-knock! Someone is here!"),
        "off": ("idle", "🔒 Tap-tap… door is closed ☕"),
    }),
    # Window (binary_sensor with "window" or "vikno")
    (re.compile(r"binary_sensor\..*(?:vikno|window)", re.IGNORECASE), {
        "on": ("celebrating", "🪟 The window is open — fresh air!"),
        "off": ("idle", "🪟 The window is closed, cozy~"),
    }),
    # Smoke (binary_sensor with "smoke" or "dymu" or "dimu")
    (re.compile(r"binary_sensor\..*(?:dymu|dimu|smoke)", re.IGNORECASE), {
        "on": ("error", "🔥 Panic! Smoke detected — check the kitchen!"),
        "off": ("success", "💨 Phew… smoke cleared, sigh of relief"),
    }),
    # Motion (binary_sensor with "motion" or "rukh")
    (re.compile(r"binary_sensor\..*(?:motion|rukh|pyh)", re.IGNORECASE), {
        "on": ("waiting", "👀 Someone there? I'm on alert!"),
        "off": ("idle", None),
    }),
    # Light
    (re.compile(r"light\..*", re.IGNORECASE), {
        "on": ("celebrating", "✨ Sparkles in the air! Light is on~"),
        "off": ("idle", "🌙 Cozy shadows hug the room…"),
    }),
    # Fan (switch.ventiliator or fan.*)
    (re.compile(r"(?:switch\..*ventiliator|switch\..*fan|fan\.).*", re.IGNORECASE), {
        "on": ("celebrating", "🌀 A breeze tousled the fringe, like a showoff!"),
        "off": ("idle", "🌿 Breeze has calmed… peace is back"),
    }),
    # LED strip
    (re.compile(r"(?:switch\..*strichka|switch\..*led|light\..*strichka).*", re.IGNORECASE), {
        "on": ("celebrating", "🌈 LED strip is shining rainbow — dance music on!"),
        "off": ("idle", "🌑 Rainbow faded… just a shimmering dream left"),
    }),
    # Person
    (re.compile(r"person\..*", re.IGNORECASE), {
        "home": ("waving", "Hello! You are home 🐾"),
        "not_home": ("waving", "Bye! 🐾"),
    }),
    # Device tracker
    (re.compile(r"device_tracker\..*", re.IGNORECASE), {
        "home": ("waving", "Someone arrived! 🐾"),
        "not_home": ("waving", "Someone left… 🐾"),
    }),
]


# ═══════════════════════════════════════════════════════════════════════════
# 5. Severity filter
# ═══════════════════════════════════════════════════════════════════════════
_NOISY_STATES = frozenset({"unavailable", "unknown", "none", "", None})


def should_skip_state(state) -> bool:
    """Drop noisy/invalid states before lookup."""
    if state is None:
        return True
    s = str(state).strip().lower()
    return s in _NOISY_STATES


# ═══════════════════════════════════════════════════════════════════════════
# 6. resolve_reaction() — main entry point
# ═══════════════════════════════════════════════════════════════════════════
def resolve_reaction(entity_id: str, to_state: str) -> Optional[Tuple[str, Optional[str]]]:
    """Return (reaction, speech) or None.

    Precedence:
      1. Special entity `input_text.openpet_message` → parse_message_value(to_state)
      2. Pattern fallback (door, light, fan, etc.)
      3. None
    """
    if should_skip_state(to_state):
        return None

    # 1. Special entity: value IS the message
    if entity_id == OPENPET_MESSAGE_ENTITY:
        parsed = parse_message_value(to_state)
        if parsed is None:
            return None
        reaction, text = parsed
        return reaction, (text or None)

    # 2. Pattern fallback
    state_l = str(to_state).strip().lower()
    for pattern, state_map in PATTERN_MAP:
        if pattern.search(entity_id):
            if state_l in state_map:
                return state_map[state_l]
            # State not in this pattern's map → no match
            break

    return None


# ═══════════════════════════════════════════════════════════════════════════
# 7. Debouncer — per (entity_id, to_state) within window
# ═══════════════════════════════════════════════════════════════════════════
class Debouncer:
    """Thread-safe debouncer keyed by (entity_id, to_state)."""

    def __init__(self, window_seconds: float = 2.0):
        self.window = window_seconds
        self._last: dict = {}
        self._lock = threading.Lock()

    def allow(self, entity_id: str, to_state: str) -> bool:
        """Return True if this (entity, state) may fire, False if within window."""
        key = (entity_id, str(to_state))
        now = time.monotonic()
        with self._lock:
            prev = self._last.get(key)
            if prev is not None and (now - prev) < self.window:
                return False
            self._last[key] = now
            return True

    def reset(self):
        with self._lock:
            self._last.clear()
