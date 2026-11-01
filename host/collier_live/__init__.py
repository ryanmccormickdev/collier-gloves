"""Live glove conductor with OSC output (the version used for the performance).

Default engine: the heuristic pick engine (``pick_engine.py``). The TCN from the
ML attempt is still loaded for the GUI and is only used with ``--engine ml``.
"""

from .config import LiveConfig

__all__ = ["LiveConfig"]
