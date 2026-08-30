"""
pet_sender.py — TCP client for OpenPets remote control v1.

NO LLM, NO async. Pure stdlib (socket, json, threading, time).

Based on the proven _Sender class from the companion OpenPets plugin, adapted for
home-assistant events:

  - Different client_id prefix ("hermes-ha" instead of "hermes-plugin")
  - Different request id prefix ("opha-N")
  - say() accepts an optional `reaction` parameter to combine animation+speech
    in one call (mirrors bridge-daemon's "no double bubble" pattern:
    when speech is present, ONLY call pet.say(reaction, text), never
    pet.react() first — OpenPets auto-appends a default caption for some
    reactions like "celebrating" → "Done", which would show two bubbles.)

Frame format (OpenPets remote control protocol v1):
  {"id": "opha-1", "protocol": "OpenPets remote control protocol", "version": 1,
   "clientId": "hermes-ha", "token": "***", "method": "pet.react",
   "params": {"reaction": "waiting"}}
  + "\n"  (newline-delimited JSON)

Silent no-op when token is empty (lets the plugin load without OpenPets).
All socket errors are caught and logged at debug level — never propagate.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional

try:
    import logging
    _log = logging.getLogger("hermes-openpet-homeassistant")
except Exception:  # pragma: no cover
    _log = None


def _log_debug(msg: str) -> None:
    if _log is not None:
        _log.debug(msg)


def _log_warning(msg: str) -> None:
    if _log is not None:
        _log.warning(msg)


class PetSender:
    """Stateless per-call TCP sender for OpenPets remote control.

    Per-call socket (not persistent) — see the companion OpenPets plugin for rationale:
    LAN latency is ~1-2ms, persistent adds broken-pipe failure modes
    we don't need.
    """

    def __init__(
        self,
        host: str = "YOUR_PET_HOST",
        port: int = 18420,
        client_id: str = "hermes-ha",
        token: str = "",
        timeout: float = 3.0,
    ):
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.token = token
        self.timeout = float(timeout)
        self._id_counter = 0
        self._lock = threading.Lock()

    # ── Core send ──────────────────────────────────────────────────────
    def _send_raw(self, method: str, params: Optional[dict] = None) -> bool:
        """Send one frame. Return True on success, False on any error.

        Silent no-op when token is empty.
        """
        if not self.token:
            return False  # not configured

        with self._lock:
            self._id_counter += 1
            req_id = f"opha-{self._id_counter}"
            payload = {
                "id": req_id,
                "protocol": "OpenPets remote control protocol",
                "version": 1,
                "clientId": self.client_id,
                "token": self.token,
                "method": method,
            }
            if params is not None:
                payload["params"] = params

            try:
                raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                with socket.create_connection(
                    (self.host, self.port), timeout=self.timeout
                ) as s:
                    s.sendall(raw)
                    s.settimeout(self.timeout)
                    try:
                        buf = b""
                        while b"\n" not in buf:
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                    except socket.timeout:
                        pass
                return True
            except (socket.error, OSError) as e:
                _log_debug(f"hermes-openpet-homeassistant: {method} socket error: {e}")
                return False
            except Exception as e:
                _log_warning(f"hermes-openpet-homeassistant: {method} unexpected: {e}")
                return False

    # ── High-level helpers ─────────────────────────────────────────────
    def react(self, reaction: str) -> bool:
        """Animate pet with a reaction, no speech bubble.

        Use this ONLY when speech is None. Otherwise use say() with
        reaction to avoid the double-bubble problem.
        """
        if not reaction:
            return False
        return self._send_raw("pet.react", {"reaction": reaction})

    def say(self, text: str, reaction: Optional[str] = None) -> bool:
        """Show a speech bubble. If `reaction` is given, also trigger that
        animation in the same call (one bubble, not two).
        """
        if not text:
            return False
        params = {"message": text}
        if reaction:
            params["reaction"] = reaction
        return self._send_raw("pet.say", params)

    def send(self, reaction: str, speech: Optional[str] = None) -> bool:
        """One-call helper: speech→say(reaction,text); else→react(reaction).

        Mirrors bridge-daemon's _send() pattern exactly.
        """
        if speech:
            return self.say(speech, reaction)
        return self.react(reaction)

    def status(self) -> bool:
        """Probe gateway liveness (no params)."""
        return self._send_raw("status")
