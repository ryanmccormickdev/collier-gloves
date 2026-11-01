"""Conducting-gloves instrument: gesture -> choir, streamed to Max/MSP over OSC.

In this copy, ``collier`` is the signal layer that ``collier_live`` builds on
(BLE transport, conditioning, calibration, region selection, scale logic).
The first heuristic app that used to live here (``app.py`` / ``run.py``) is
not included, so the package no longer imports it.
"""

from .config import Config
from .sinks import ConsoleSink, MultiSink, OscSink, SoundSink

__all__ = ["Config", "SoundSink", "OscSink", "ConsoleSink", "MultiSink"]
