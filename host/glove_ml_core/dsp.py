"""Minimal DSP primitives for glove conditioning (trainer v3 parity)."""
from __future__ import annotations

import math


def tau_to_alpha(tau: float, dt: float) -> float:
    if tau <= 0.0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)


def wrap180(deg: float) -> float:
    d = (deg + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class OnePole:
    def __init__(self, tau: float, initial: float = 0.0):
        self.tau = tau
        self.y = initial
        self._init = False

    def update(self, x: float, dt: float) -> float:
        if not self._init:
            self.y = x
            self._init = True
            return self.y
        self.y += tau_to_alpha(self.tau, dt) * (x - self.y)
        return self.y


class CircularOnePole:
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
    def __init__(self, tau: float):
        self.lp = OnePole(tau)

    def update(self, x: float, dt: float) -> float:
        return x - self.lp.update(x, dt)


class EnvelopeFollower:
    def __init__(self, attack_s: float, release_s: float):
        self.attack_s = attack_s
        self.release_s = release_s
        self.env = 0.0

    def update(self, x: float, dt: float) -> float:
        mag = abs(x)
        tau = self.attack_s if mag > self.env else self.release_s
        self.env += tau_to_alpha(tau, dt) * (mag - self.env)
        return self.env


class EnvelopeMinMax:
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
        self.lo += self.decay * span * dt
        self.hi -= self.decay * span * dt
        self.lo = min(self.lo, x)
        self.hi = max(self.hi, x)
        span = self.hi - self.lo
        if span < self.min_span:
            return 0.5
        return clamp((x - self.lo) / span, 0.0, 1.0)


class LeakyVelocity:
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
