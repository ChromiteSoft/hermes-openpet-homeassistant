"""
ha_ws.py — Home Assistant WebSocket listener for openpet-ha plugin.

NO LLM. Pure deterministic event subscription. Runs in a daemon thread
inside the gateway.

Uses the `websockets.sync.client` API (stdlib-style blocking calls in a
worker thread) — websockets 15.0.1 ships in the hermes-agent venv.

Protocol (HA WebSocket API):

  1. Server sends hello:
       {"type": "auth_required", "ha_version": "..."}
  2. Client sends auth:
       {"type": "auth", "access_token": "***"}
  3. Server responds:
       {"type": "auth_ok"}                     → success
       {"type": "auth_invalid", "message": "..."} → fail (close)
  4. Client subscribes:
       {"id": 1, "type": "subscribe_events",
        "event": {"event_type": "state_changed"}}
  5. Server acks:
       {"id": 1, "type": "result", "success": true}
  6. Server pushes events:
       {"id": 1, "type": "event",
        "event": {"event_type": "state_changed",
                  "data": {"entity_id": "light.liustra",
                           "new_state": {"state": "on", ...},
                           "old_state": {"state": "off", ...}}}}
  7. Keep-alive: websockets library handles PING/PONG automatically.

Reconnect: exponential backoff (1s → 60s) on any error.

Lifecycle: stop() is idempotent and signal-safe.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import ssl
import threading
import time
import urllib.parse
from typing import Callable, Optional

_log = logging.getLogger("openpet-ha")

# Type alias for the event callback: (entity_id, new_state, old_state) → None
EventCallback = Callable[[str, str, Optional[str]], None]


class HAWebSocketError(Exception):
    pass


class HAWebSocket:
    """Persistent WebSocket client to Home Assistant.

    Threading model:
      - _run_loop() runs in a daemon thread (uses sync websockets API).
      - Events are pushed into _event_q (Queue) for the consumer.
      - The consumer thread drains the queue and dispatches via the callback.

    Consumer side (the plugin) should call:
        ha = HAWebSocket(url, token, on_event)
        ha.start()
        # ... plugin runs ...
        ha.stop()
    """

    PING_INTERVAL = 30.0       # seconds between pings
    BACKOFF_MIN = 1.0
    BACKOFF_MAX = 60.0
    OPEN_TIMEOUT = 10.0        # seconds for initial connection
    CLOSE_TIMEOUT = 5.0        # seconds for graceful close

    def __init__(self, url: str, token: str,
                 on_event: EventCallback,
                 verify_ssl: bool = True,
                 event_type: str = "state_changed",
                 entity_filter: Optional[list[str]] = None,
                 event_q_max: int = 256):
        """
        url:     e.g. "http://YOUR_HA_HOST:8123"  (we'll convert http→ws, https→wss)
        token:   HA long-lived access token
        on_event: callback(entity_id, new_state, old_state)
        verify_ssl: False only for self-signed dev setups
        event_type:  which HA event_type to subscribe to
        entity_filter: optional list of entity_ids (None = all)
        event_q_max:  max queued events before dropping oldest
        """
        self.url = url.rstrip("/")
        self.token = token
        self.on_event = on_event
        self.verify_ssl = verify_ssl
        self.event_type = event_type
        self.entity_filter = entity_filter
        self._event_q: queue.Queue = queue.Queue(maxsize=event_q_max)
        self._ws = None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._msg_id = 0

    # ── Public API ─────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="ha-ws", daemon=True
        )
        self._thread.start()
        self._consumer_thread = threading.Thread(
            target=self._consume_forever, name="ha-ws-consumer", daemon=True
        )
        self._consumer_thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=self.CLOSE_TIMEOUT)
        if self._consumer_thread:
            self._consumer_thread.join(timeout=2.0)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── URL → ws://host:port/api/websocket ─────────────────────────────
    def _ws_url(self) -> str:
        u = urllib.parse.urlparse(self.url)
        scheme = "wss" if u.scheme == "https" else "ws"
        host = u.hostname or "localhost"
        # HA's WS endpoint is on the same port as HTTP(S), path /api/websocket
        port = f":{u.port}" if u.port else ""
        path = (u.path or "").rstrip("/") + "/api/websocket"
        return f"{scheme}://{host}{port}{path}"

    # ── Main reconnect loop ────────────────────────────────────────────
    def _run_forever(self) -> None:
        # Lazy import so the module loads even if websockets is missing
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            _log.error("openpet-ha: websockets not installed: %s", e)
            return

        backoff = self.BACKOFF_MIN
        while not self._stop_evt.is_set():
            try:
                self._run_once(connect)
                backoff = self.BACKOFF_MIN
            except HAWebSocketError as e:
                _log.warning("openpet-ha: ws error: %s (reconnect in %.1fs)",
                             e, backoff)
                self._connected.clear()
                if self._stop_evt.wait(backoff):
                    return
                backoff = min(self.BACKOFF_MAX, backoff * 2 + random.uniform(0, 0.5))
            except Exception as e:
                _log.warning("openpet-ha: ws unexpected: %s (reconnect in %.1fs)",
                             e, backoff)
                self._connected.clear()
                if self._stop_evt.wait(backoff):
                    return
                backoff = min(self.BACKOFF_MAX, backoff * 2 + random.uniform(0, 0.5))

    def _run_once(self, connect_fn) -> None:
        """One full session: connect, auth, subscribe, run until error."""
        url = self._ws_url()
        ssl_ctx = None
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        # Connect (sync; runs in worker thread)
        ws = connect_fn(
            url,
            ssl_context=ssl_ctx,
            open_timeout=self.OPEN_TIMEOUT,
            ping_interval=self.PING_INTERVAL,
            ping_timeout=self.PING_INTERVAL,
        )
        self._ws = ws
        try:
            # 1. Receive hello
            hello_raw = ws.recv(timeout=self.OPEN_TIMEOUT)
            if hello_raw is None:
                raise HAWebSocketError("server closed during hello")
            hello = json.loads(hello_raw)
            if hello.get("type") != "auth_required":
                raise HAWebSocketError(f"unexpected hello: {hello!r}")

            # 2. Send auth
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth_raw = ws.recv(timeout=self.OPEN_TIMEOUT)
            if auth_raw is None:
                raise HAWebSocketError("no auth response")
            auth_resp = json.loads(auth_raw)
            if auth_resp.get("type") == "auth_invalid":
                raise HAWebSocketError(
                    f"auth_invalid: {auth_resp.get('message', '?')}"
                )
            if auth_resp.get("type") != "auth_ok":
                raise HAWebSocketError(f"unexpected auth response: {auth_resp!r}")

            # 3. Subscribe
            # For entity-filtered subscription, HA exposes
            # `subscribe_entities` (newer) and `subscribe_trigger` (with
            # state trigger). The legacy `subscribe_events` accepts
            # `event_type` only, NOT `entity_id_filter` (returns
            # invalid_format if you include it).
            # We use `subscribe_trigger` with a state platform filter so
            # we get the same `state_changed`-shaped events but scoped.
            if self.entity_filter:
                self._msg_id += 1
                sub = {
                    "id": self._msg_id,
                    "type": "subscribe_trigger",
                    "trigger": {
                        "platform": "state",
                        "entity_id": self.entity_filter,
                    },
                }
                ws.send(json.dumps(sub))
                sub_raw = ws.recv(timeout=self.OPEN_TIMEOUT)
                if sub_raw is None:
                    raise HAWebSocketError("no subscribe response")
                sub_resp = json.loads(sub_raw)
                if not sub_resp.get("success"):
                    raise HAWebSocketError(f"subscribe failed: {sub_resp!r}")
            else:
                # No filter: use plain subscribe_events (the only form HA
                # accepts without `entity_id_filter`).
                self._msg_id += 1
                sub = {
                    "id": self._msg_id,
                    "type": "subscribe_events",
                    "event_type": self.event_type,
                }
                ws.send(json.dumps(sub))
                sub_raw = ws.recv(timeout=self.OPEN_TIMEOUT)
                if sub_raw is None:
                    raise HAWebSocketError("no subscribe response")
                sub_resp = json.loads(sub_raw)
                if not sub_resp.get("success"):
                    raise HAWebSocketError(f"subscribe failed: {sub_resp!r}")

            self._connected.set()
            _log.info("openpet-ha: subscribed to %s%s",
                      self.event_type,
                      f" (filter={self.entity_filter})" if self.entity_filter else "")

            # 4. Event loop
            while not self._stop_evt.is_set():
                raw = ws.recv(timeout=self.PING_INTERVAL * 2)
                if raw is None:
                    raise HAWebSocketError("server closed connection")
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "event":
                    self._dispatch_event(msg.get("event", {}))
                # ignore 'pong', 'result' (ack), etc.
        finally:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None
            self._connected.clear()

    # ── Event dispatch ────────────────────────────────────────────────
    def _dispatch_event(self, ev: dict) -> None:
        """Two event shapes arrive depending on subscription method:
        1. subscribe_events → {event_type, data: {entity_id, new_state, old_state}}
        2. subscribe_trigger → {variables: {trigger: {entity_id, to_state, from_state}}}

        We normalize to (entity_id, new_state, old_state) for the consumer.
        """
        try:
            entity_id = ""
            new_state = ""
            old_state = ""

            # Shape 2: subscribe_trigger
            variables = ev.get("variables")
            if isinstance(variables, dict):
                trig = variables.get("trigger") or {}
                entity_id = trig.get("entity_id", "")
                to_s = trig.get("to_state") or {}
                fr_s = trig.get("from_state") or {}
                new_state = to_s.get("state", "") if isinstance(to_s, dict) else ""
                old_state = fr_s.get("state", "") if isinstance(fr_s, dict) else ""
            else:
                # Shape 1: subscribe_events
                if ev.get("event_type") != self.event_type:
                    return
                data = ev.get("data", {}) or {}
                entity_id = data.get("entity_id", "")
                new = data.get("new_state") or {}
                old = data.get("old_state") or {}
                new_state = new.get("state", "") if isinstance(new, dict) else ""
                old_state = old.get("state", "") if isinstance(old, dict) else ""

            if not entity_id:
                return
            # Push to queue for consumer thread (drop oldest on full)
            try:
                self._event_q.put_nowait((entity_id, new_state, old_state))
            except queue.Full:
                try:
                    self._event_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._event_q.put_nowait((entity_id, new_state, old_state))
                except queue.Full:
                    pass
        except Exception as e:
            _log.warning("openpet-ha: dispatch error: %s", e)

    def _consume_forever(self) -> None:
        """Drain event queue and call user callback. Survives callback errors."""
        while not self._stop_evt.is_set():
            try:
                entity_id, new_state, old_state = self._event_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.on_event(entity_id, new_state, old_state)
            except Exception as e:
                _log.warning("openpet-ha: on_event callback raised: %s", e)
