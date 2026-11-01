"""Live flex min/max trackers for ML feature normalization."""
from __future__ import annotations

from collections import deque

from .config import CondCfg


class SessionFlexTracker:
    """
    Per-channel running min/max from the moment this hand starts sending packets.

    Matches trainer ``EnvelopeMinMax`` drift semantics (slow inward relax, snap to
    new extremes) so open/closed distinction is available for calibration picks
    without waiting on a time window.
    """

    def __init__(
        self,
        n_channels: int,
        min_span: float = 80.0,
        decay_per_s: float | None = None,
    ):
        c = CondCfg()
        self.min_span = min_span
        self.decay_per_s = c.flex_env_decay_per_s if decay_per_s is None else decay_per_s
        self._lo: list[float | None] = [None] * n_channels
        self._hi: list[float | None] = [None] * n_channels
        self._samples = 0

    def reset(self) -> None:
        self._lo = [None] * len(self._lo)
        self._hi = [None] * len(self._hi)
        self._samples = 0

    def update(
        self,
        raw_flex: tuple[int, ...],
        dt: float,
    ) -> dict[str, list[float]] | None:
        if len(raw_flex) != len(self._lo):
            return None

        for i, raw in enumerate(raw_flex):
            x = float(raw)
            if self._lo[i] is None:
                self._lo[i] = self._hi[i] = x
            else:
                span = max(self._hi[i] - self._lo[i], 1e-6)
                self._lo[i] += self.decay_per_s * span * dt
                self._hi[i] -= self.decay_per_s * span * dt
                self._lo[i] = min(self._lo[i], x)
                self._hi[i] = max(self._hi[i], x)
            self._samples += 1

        return self._range_dict()

    def _range_dict(self) -> dict[str, list[float]] | None:
        lo, hi = [], []
        for i in range(len(self._lo)):
            lo_v, hi_v = self._lo[i], self._hi[i]
            if lo_v is None or hi_v is None:
                return None
            if hi_v - lo_v < self.min_span:
                mid = 0.5 * (lo_v + hi_v)
                lo_v = mid - 0.5 * self.min_span
                hi_v = mid + 0.5 * self.min_span
            lo.append(lo_v)
            hi.append(hi_v)
        return {"lo": lo, "hi": hi}

    @property
    def ready(self) -> bool:
        return self._samples > 0 and all(v is not None for v in self._lo)

    def channel_spans(self) -> list[float]:
        spans = []
        for lo, hi in zip(self._lo, self._hi):
            if lo is None or hi is None:
                spans.append(0.0)
            else:
                spans.append(hi - lo)
        return spans


class RollingFlexTracker:
    """Per-channel min/max over a sliding time window (default: past 60 s)."""

    def __init__(
        self,
        n_channels: int,
        min_span: float = 80.0,
        window_s: float = 60.0,
    ):
        self.min_span = min_span
        self.window_s = max(window_s, 1e-3)
        self._hist: list[deque[tuple[float, float]]] = [deque() for _ in range(n_channels)]
        self._t = 0.0
        self._samples = 0

    def reset(self) -> None:
        for h in self._hist:
            h.clear()
        self._t = 0.0
        self._samples = 0

    def update(
        self,
        raw_flex: tuple[int, ...],
        dt: float,
    ) -> dict[str, list[float]] | None:
        if len(raw_flex) != len(self._hist):
            return None

        self._t += max(dt, 0.0)
        cutoff = self._t - self.window_s
        for i, raw in enumerate(raw_flex):
            self._hist[i].append((self._t, float(raw)))
            while self._hist[i] and self._hist[i][0][0] < cutoff:
                self._hist[i].popleft()
            self._samples += 1

        return self._range_dict()

    def _range_dict(self) -> dict[str, list[float]] | None:
        lo, hi = [], []
        for hist in self._hist:
            if not hist:
                return None
            lo_v = min(v for _, v in hist)
            hi_v = max(v for _, v in hist)
            if hi_v - lo_v < self.min_span:
                mid = 0.5 * (lo_v + hi_v)
                lo_v = mid - 0.5 * self.min_span
                hi_v = mid + 0.5 * self.min_span
            lo.append(lo_v)
            hi.append(hi_v)
        return {"lo": lo, "hi": hi}

    @property
    def ready(self) -> bool:
        return self._samples > 0 and all(len(h) > 0 for h in self._hist)

    def channel_spans(self) -> list[float]:
        out: list[float] = []
        for hist in self._hist:
            if not hist:
                out.append(0.0)
                continue
            lo_v = min(v for _, v in hist)
            hi_v = max(v for _, v in hist)
            out.append(hi_v - lo_v)
        return out
