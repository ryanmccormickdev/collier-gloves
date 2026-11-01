"""Internal gesture commands emitted after ML post-processing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GestureCommand:
    kind: Literal["spawn", "scale", "chromatic", "volume"]
    regions: tuple[int, ...]
    source_hand: str
    steps: int = 0  # +1 / -1 for pitch commands
