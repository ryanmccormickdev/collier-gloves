"""OSC output for collier_live."""
from __future__ import annotations

import logging
from typing import Any

try:
    from pythonosc.udp_client import SimpleUDPClient
except ImportError:
    SimpleUDPClient = None  # type: ignore[misc, assignment]

log = logging.getLogger(__name__)


class OscLiveSink:
    """Send /region/<i>/notes, /region/<i>/spawn, /expr/filters."""

    def __init__(self, host: str, port: int):
        if SimpleUDPClient is None:
            raise ImportError("python-osc required: pip install python-osc")
        self._client = SimpleUDPClient(host, port)
        self.host = host
        self.port = port

    def send_notes(self, region: int, value: int | list) -> None:
        addr = f"/region/{region}/notes"
        if value == []:
            self._client.send_message(addr, [])
        else:
            # Max expects a note list (same as collier.sinks.OscSink).
            self._client.send_message(addr, [int(value)])

    def send_spawn(self, region: int, value: int) -> None:
        self._client.send_message(f"/region/{region}/spawn", int(value))

    def send_filters(self, bends: list[float]) -> None:
        self._client.send_message("/expr/filters", [float(x) for x in bends])

    def apply_delta(self, kind: str, region: int, value: Any) -> None:
        if kind == "notes":
            self.send_notes(region, value)
        elif kind == "spawn":
            self.send_spawn(region, int(value))
