"""Note names and frequency helpers for the preview synth."""

from __future__ import annotations

_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def midi_name(midi: int) -> str:
    return f"{_NAMES[midi % 12]}{midi // 12 - 1}"


def midi_to_hz(midi: int) -> float:
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))
