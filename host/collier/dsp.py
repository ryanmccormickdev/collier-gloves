"""
Small, dependency-free DSP building blocks.

These are the vocabulary the conditioning and gesture layers are written in.
Each is a tiny stateful object you `.update(x, dt)` once per tick. Keeping them
here (instead of inlining math everywhere) is most of what makes the higher
layers readable.
"""

from __future__ import annotations

import math
from collections import deque


def tau_to_alpha(tau: float, dt: float) -> float:
    """One-pole smoothing coefficient for a given time constant `tau`."""
    if tau <= 0.0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)


class OnePole:
    """First-order low-pass (exponential smoother)."""

    def __init__(self, tau: float, initial: float = 0.0):
        self.tau = tau
        self.y = initial
        self._init = False

    def update(self, x: float, dt: float) -> float:
        if not self._init:
            self.y = x
            self._init = True
            return self.y
        a = tau_to_alpha(self.tau, dt)
        self.y += a * (x - self.y)
        return self.y


class CircularOnePole:
    """
    One-pole low-pass for an angle in DEGREES, done on the unit circle so it
    never glitches across the ±180° wrap (a plain OnePole on heading jumps when
    the value crosses 180). Smooths sin/cos and reads back with atan2.
    """

    def __init__(self, tau: float):
        self.tau = tau
        self._c = 0.0
        self._s = 0.0
        self._init = False

    def update(self, deg: float, dt: float) -> float:
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        if not self._init:
            self._c, self._s = c, s
            self._init = True
        else:
            a = tau_to_alpha(self.tau, dt)
            self._c += a * (c - self._c)
            self._s += a * (s - self._s)
        return math.degrees(math.atan2(self._s, self._c))


class HighPass:
    """x minus its low-passed version: keeps fast changes, drops slow drift."""

    def __init__(self, tau: float):
        self.lp = OnePole(tau)

    def update(self, x: float, dt: float) -> float:
        return x - self.lp.update(x, dt)


class EnvelopeFollower:
    """Rectified signal -> asymmetric attack/release envelope."""

    def __init__(self, attack_s: float, release_s: float):
        self.attack_s = attack_s
        self.release_s = release_s
        self.env = 0.0

    def update(self, x: float, dt: float) -> float:
        mag = abs(x)
        tau = self.attack_s if mag > self.env else self.release_s
        a = tau_to_alpha(tau, dt)
        self.env += a * (mag - self.env)
        return self.env


class EnvelopeMinMax:
    """
    Drift-adaptive normalizer. Tracks a running min/max that *snaps* to new
    extremes instantly but *relaxes* inward slowly, so the usable range follows
    sensor drift over minutes without storing any history. O(1) per sample.
    """

    def __init__(self, decay_per_s: float, min_span: float):
        self.decay = decay_per_s
        self.min_span = min_span
        self.lo: float | None = None
        self.hi: float | None = None

    def update(self, x: float, dt: float) -> float:
        if self.lo is None:
            self.lo = self.hi = x
            return 0.5
        span = max(self.hi - self.lo, 1e-6)
        # Relax the walls toward the current value (inward) at `decay` of span/s.
        self.lo += self.decay * span * dt
        self.hi -= self.decay * span * dt
        # Snap outward to any new extreme.
        self.lo = min(self.lo, x)
        self.hi = max(self.hi, x)
        span = self.hi - self.lo
        if span < self.min_span:
            return 0.5  # channel too quiet to trust; report neutral
        return _clamp((x - self.lo) / span, 0.0, 1.0)


class SlidingMinMax:
    """Percentile-based normalizer over the last ``window_s`` seconds.

    Uses the 1st / 99th percentiles of the windowed samples (virtual clock,
    advanced by ``dt``) as the lo/hi reference points. This is much more
    forgiving than strict min/max: a single accidental over-bend during the
    window can't anchor the top of the range for the next minute. Both
    percentiles are recomputed periodically (every ``_RECOMPUTE_S``) and
    cached; the normalization itself runs at full rate against the cache.

    Samples are also subsampled to at most ``_MAX_SAMPLES`` over the window
    so percentile computation stays cheap even at 33+ Hz.
    """

    LO_PCT = 1.0
    HI_PCT = 99.0
    _MAX_SAMPLES = 300
    _RECOMPUTE_S = 0.10

    def __init__(self, window_s: float, min_span: float):
        self.window_s = max(window_s, 1e-3)
        self.min_span = min_span
        self._samples: deque[tuple[float, float]] = deque()
        self._t = 0.0
        self._next_sample_t = 0.0
        self._sample_dt = self.window_s / float(self._MAX_SAMPLES)
        self._last_recompute_t = -1e9
        self._lo: float | None = None
        self._hi: float | None = None

    def _percentile(self, pct: float, sorted_vals: list[float]) -> float:
        n = len(sorted_vals)
        if n == 1:
            return sorted_vals[0]
        # Linear interpolation between closest ranks (numpy "linear" default).
        rank = (pct / 100.0) * (n - 1)
        lo_i = int(rank)
        hi_i = min(lo_i + 1, n - 1)
        frac = rank - lo_i
        return sorted_vals[lo_i] * (1.0 - frac) + sorted_vals[hi_i] * frac

    def update(self, x: float, dt: float) -> float:
        self._t += max(dt, 0.0)

        # Subsample: only append when enough virtual time has passed.
        if self._t >= self._next_sample_t:
            self._samples.append((self._t, x))
            self._next_sample_t = self._t + self._sample_dt

        cutoff = self._t - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if (self._t - self._last_recompute_t) >= self._RECOMPUTE_S or self._lo is None:
            self._last_recompute_t = self._t
            if not self._samples:
                self._lo = self._hi = x
            else:
                vals = sorted(v for _, v in self._samples)
                self._lo = self._percentile(self.LO_PCT, vals)
                self._hi = self._percentile(self.HI_PCT, vals)

        lo, hi = self._lo, self._hi
        # Expand percentile band to include the live sample so a true new
        # extreme normalizes to 0 or 1 immediately rather than waiting for
        # the next recompute tick. (We don't write it back — the percentile
        # itself stays a stable, spike-resistant estimate.)
        if x < lo:
            lo = x
        if x > hi:
            hi = x
        span = hi - lo
        if span < self.min_span:
            return 0.5
        return _clamp((x - lo) / span, 0.0, 1.0)


class Schmitt:
    """Hysteresis comparator: separate on/off thresholds prevent flicker."""

    def __init__(self, on_below: float, off_above: float, state: bool = False):
        # Designed for "engage when value drops below on_below" (e.g. openness).
        self.on_below = on_below
        self.off_above = off_above
        self.state = state

    def update(self, x: float) -> bool:
        if self.state and x > self.off_above:
            self.state = False
        elif (not self.state) and x < self.on_below:
            self.state = True
        return self.state


class Refractory:
    """A gate that, once fired, refuses to fire again for `hold_s` seconds."""

    def __init__(self, hold_s: float):
        self.hold_s = hold_s
        self._cooldown = 0.0

    def tick(self, dt: float) -> None:
        if self._cooldown > 0.0:
            self._cooldown -= dt

    @property
    def ready(self) -> bool:
        return self._cooldown <= 0.0

    def fire(self) -> bool:
        """Returns True and arms the cooldown if ready; else returns False."""
        if self.ready:
            self._cooldown = self.hold_s
            return True
        return False


class RollingSign:
    """Tracks how consistently a stream of values shares one sign (for circles)."""

    def __init__(self, n: int):
        self.buf: deque[float] = deque(maxlen=n)

    def update(self, x: float) -> float:
        self.buf.append(1.0 if x >= 0 else -1.0)
        if not self.buf:
            return 0.0
        return abs(sum(self.buf)) / len(self.buf)  # 0..1 directional agreement


class LeakyVelocity:
    """
    Integrate linear acceleration into a velocity estimate, with slow bias
    removal so the estimate self-zeros between moves instead of drifting away.

    We subtract a slow low-pass of the acceleration (removes DC bias / gravity
    leakage) before integrating, so the resulting velocity has no DC term and
    bleeds back toward zero on its own. `zero=True` hard-resets when the limb is
    detected truly still, killing any residue before the next gesture.
    """

    def __init__(self, bias_tau: float):
        self._bias = OnePole(bias_tau)
        self.v = 0.0

    def update(self, accel: float, dt: float, *, zero: bool = False) -> float:
        bias = self._bias.update(accel, dt)
        if zero:
            self.v = 0.0
            return 0.0
        self.v += (accel - bias) * dt
        return self.v


class SegmentStepper:
    """
    Turn a 1-D motion *rate* stream into discrete, in-sync steps.

    The rate (velocity in m/s for translation, deg/s for a tilt) is integrated
    into a per-segment displacement. A segment ends when the motion **rests**
    (|rate| < stop_rate for stop_time_s) or **reverses** (|rate| > reversal_rate
    in the opposite direction). On end it emits sign·N steps, where
    N = round(|displacement| / unit_per_step) clamped to [1, max_steps] — but
    only if the displacement cleared `min_unit` (otherwise it was noise).

    Why this design: every coherent directional segment emits exactly once, so a
    move *and its reverse* both register (raise→lower = +1 then −1), pitch can
    never silently desync, and a single grand gesture that covers 2× the distance
    emits 2 steps. Slow-and-long works (displacement accumulates regardless of
    speed); quick-and-small is rejected (never clears min_unit).
    """

    def __init__(self, *, move_rate: float, stop_rate: float, stop_time_s: float,
                 reversal_rate: float, unit_per_step: float, min_unit: float,
                 max_steps: int):
        self.move_rate = move_rate
        self.stop_rate = stop_rate
        self.stop_time_s = stop_time_s
        self.reversal_rate = reversal_rate
        self.unit_per_step = unit_per_step
        self.min_unit = min_unit
        self.max_steps = max_steps
        self.reset()

    def reset(self) -> None:
        self._sign = 0          # 0 = idle, ±1 = direction of current segment
        self._disp = 0.0
        self._stopped_t = 0.0

    @property
    def displacement(self) -> float:
        return self._disp

    def _emit(self, disp: float) -> int:
        # One emit per coherent directional segment. `min_unit` is the noise
        # guard; there is deliberately NO time-refractory here — a fast raise→
        # lower must fire +N then −N, so blocking the reverse for a fixed window
        # would silently drop it (the desync bug this whole design exists to fix).
        mag = abs(disp)
        if mag < self.min_unit:
            return 0
        n = int(round(mag / self.unit_per_step))
        n = max(1, min(self.max_steps, n))
        return n if disp > 0 else -n

    def update(self, rate: float, dt: float, *, enabled: bool = True) -> int:
        if not enabled:
            self.reset()
            return 0

        if self._sign == 0:
            if abs(rate) >= self.move_rate:
                self._sign = 1 if rate > 0 else -1
                self._disp = rate * dt
                self._stopped_t = 0.0
            return 0

        self._disp += rate * dt

        # Reversal: rate has built up in the opposite direction.
        if rate * self._sign < 0 and abs(rate) >= self.reversal_rate:
            out = self._emit(self._disp)
            self._sign = 1 if rate > 0 else -1
            self._disp = rate * dt
            self._stopped_t = 0.0
            return out

        # Rest: motion has fallen near zero for long enough.
        if abs(rate) < self.stop_rate:
            self._stopped_t += dt
            if self._stopped_t >= self.stop_time_s:
                out = self._emit(self._disp)
                self.reset()
                return out
        else:
            self._stopped_t = 0.0
        return 0


# --- angle helpers ---------------------------------------------------------

def wrap180(deg: float) -> float:
    """Wrap an angle to (-180, 180]."""
    d = (deg + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def circular_mean_deg(a: float, b: float) -> float:
    """Mean of two angles that respects wraparound."""
    x = math.cos(math.radians(a)) + math.cos(math.radians(b))
    y = math.sin(math.radians(a)) + math.sin(math.radians(b))
    return math.degrees(math.atan2(y, x))


def angle_delta_deg(curr: float, prev: float) -> float:
    """Shortest signed change from prev to curr, in degrees."""
    return wrap180(curr - prev)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
