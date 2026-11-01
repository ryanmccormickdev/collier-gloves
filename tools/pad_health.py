#!/usr/bin/env python3
"""Live per-pad health monitor for the flex sensors. Each finger has TWO
ADC channels (proximal + distal pad). This tool watches all 10 channels per
hand and reports:

  RANGE   — total ADC swing seen since the channel was first observed
  NOW     — current raw value
  VAR     — variance over the last 1.0 s (high = noisy pad)
  STILL   — how long the channel has been frozen (no change >= NOISE_TOL)
  HEALTH  — heuristic classification

Heuristic per pad:
  DEAD       — RANGE < 8 over the session AND no change in last 5 s
               (signal is essentially flat; sensor likely disconnected/saturated)
  STUCK      — last 3 s show no change but session range was healthy
               (pad currently jammed; might recover with a finger wiggle)
  NOISY      — VAR over the last second is high while the user reports stillness
               (intermittent contact; can poison features)
  WEAK       — session RANGE > 8 but < 35; sensor responds but only slightly
               (will calibrate to a narrow band, leading to compressed bend values)
  OK         — RANGE >= 35 and recent activity present

Run:
  python tools/pad_health.py [--secs 0 = forever, otherwise N s]

Workflow:
  1. Power on both gloves.
  2. The script prints a refreshing table; let it run for 5-10 s of stillness
     first so STILL counters and noise baselines settle.
  3. Then bend EACH FINGER one at a time, fully open to fully closed, twice.
     Channels that don't move during their finger's wiggle are dead/stuck.
  4. Press Ctrl+C when done; a summary table prints with per-pad verdicts.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host"  # collier / glove_ml_core live under host/ in this repo
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from collier.config import Config
from collier.frames import RawFrame
from collier.transport import GloveTransport
from glove_ml_core.config import FINGER_CHANNELS_L, FINGER_CHANNELS_R, FINGER_NAMES


# A change smaller than this between consecutive samples doesn't reset the
# STILL counter (suppresses ADC LSB noise).
NOISE_TOL = 2

# Recent-window for VAR / STILL — must be enough to span fast wiggles.
RECENT_S = 1.0

# Thresholds for classification.
RANGE_DEAD = 8
RANGE_WEAK = 35
STUCK_S = 3.0
DEAD_STILL_S = 5.0
NOISY_VAR = 8.0  # ADC^2; tuned against typical sensor floor


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _channel_finger_pad(side: str) -> dict[int, tuple[str, str]]:
    """Map ADC channel index -> (finger_name, "prox"/"dist")."""
    src = FINGER_CHANNELS_L if side == "L" else FINGER_CHANNELS_R
    out: dict[int, tuple[str, str]] = {}
    for finger, (a, b) in src.items():
        # We don't actually know which physical pad is which from the mapping
        # alone; label them as pad0/pad1 for that finger.
        out[a] = (finger, "pad0")
        out[b] = (finger, "pad1")
    return out


class ChannelStats:
    __slots__ = ("hist", "samples", "lo", "hi", "last_change_t", "last", "first_t")

    def __init__(self) -> None:
        self.hist: deque[tuple[float, int]] = deque()  # (t, raw)
        self.samples = 0
        self.lo: int | None = None
        self.hi: int | None = None
        self.last_change_t: float = 0.0
        self.last: int | None = None
        self.first_t: float | None = None

    def update(self, t: float, raw: int) -> None:
        if self.first_t is None:
            self.first_t = t
            self.last_change_t = t
        self.hist.append((t, raw))
        cutoff = t - RECENT_S
        while self.hist and self.hist[0][0] < cutoff:
            self.hist.popleft()
        self.samples += 1
        if self.lo is None or raw < self.lo:
            self.lo = raw
        if self.hi is None or raw > self.hi:
            self.hi = raw
        if self.last is None or abs(raw - self.last) > NOISE_TOL:
            self.last_change_t = t
        self.last = raw

    @property
    def range(self) -> int:
        if self.lo is None or self.hi is None:
            return 0
        return int(self.hi - self.lo)

    @property
    def session_s(self) -> float:
        if self.first_t is None or self.last is None:
            return 0.0
        return (self.hist[-1][0] - self.first_t) if self.hist else 0.0

    def var_recent(self) -> float:
        if len(self.hist) < 4:
            return 0.0
        vals = [v for _, v in self.hist]
        mu = sum(vals) / len(vals)
        return sum((v - mu) ** 2 for v in vals) / len(vals)

    def still_for(self, now: float) -> float:
        if self.last is None:
            return 0.0
        return now - self.last_change_t

    def health(self, now: float) -> str:
        if self.samples == 0:
            return "—"
        rng = self.range
        still = self.still_for(now)
        var = self.var_recent()
        if rng < RANGE_DEAD and still > DEAD_STILL_S:
            return "DEAD"
        if rng >= RANGE_WEAK and still > STUCK_S:
            return "STUCK"
        if var > NOISY_VAR and still < 0.5:
            return "NOISY"
        if rng < RANGE_DEAD:
            return "QUIET"  # not yet dead, but no movement
        if rng < RANGE_WEAK:
            return "WEAK"
        return "OK"


HEALTH_COLOR = {
    "DEAD": "\x1b[91;1m",
    "STUCK": "\x1b[33;1m",
    "NOISY": "\x1b[35;1m",
    "WEAK": "\x1b[33m",
    "QUIET": "\x1b[36m",
    "OK": "\x1b[32m",
    "—": "\x1b[90m",
}
RESET = "\x1b[0m"


class PadHealth:
    def __init__(self, secs: float):
        self.cfg = Config()
        self.secs = secs
        self.n_ch = self.cfg.packet.flex_count
        self.stats: dict[str, list[ChannelStats]] = {
            "L": [ChannelStats() for _ in range(self.n_ch)],
            "R": [ChannelStats() for _ in range(self.n_ch)],
        }
        self.label = {s: _channel_finger_pad(s) for s in ("L", "R")}
        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(self.cfg.ble, self.cfg.packet, self._on_frame)
        self._t0 = 0.0

    def _on_frame(self, frame: RawFrame) -> None:
        self._latest[frame.side] = frame
        now = time.monotonic()
        for ch_idx, raw in enumerate(frame.flex):
            if ch_idx < self.n_ch:
                self.stats[frame.side][ch_idx].update(now, int(raw))

    async def run(self) -> None:
        await asyncio.gather(self._transport.run(), self._render_loop())

    async def _render_loop(self) -> None:
        print("Waiting for gloves to connect…")
        while self._latest["L"] is None and self._latest["R"] is None:
            await asyncio.sleep(0.1)
        self._t0 = time.monotonic()
        while True:
            now = time.monotonic()
            self._render(now)
            if self.secs > 0 and (now - self._t0) >= self.secs:
                self._summary()
                return
            await asyncio.sleep(0.25)

    def _render(self, now: float) -> None:
        _clear()
        elapsed = now - self._t0
        print(f"Pad health  ·  elapsed {elapsed:5.1f}s  ·  Ctrl+C for summary")
        print(f"  NOISE_TOL={NOISE_TOL}  RANGE_DEAD<{RANGE_DEAD}  RANGE_WEAK<{RANGE_WEAK}  STUCK_S>{STUCK_S}  DEAD_STILL_S>{DEAD_STILL_S}")
        print()
        for side in ("L", "R"):
            print(f"  ===== Glove-{side} =====")
            print(f"    {'ch':>3} {'finger':<7} {'pad':<5} {'now':>6} {'lo':>6} {'hi':>6} {'range':>6} {'var1s':>7} {'still':>6} {'health'}")
            for ch in range(self.n_ch):
                st = self.stats[side][ch]
                if ch not in self.label[side]:
                    finger, pad = "—", "—"
                else:
                    finger, pad = self.label[side][ch]
                now_v = st.last if st.last is not None else 0
                lo_v = st.lo if st.lo is not None else 0
                hi_v = st.hi if st.hi is not None else 0
                rng = st.range
                var = st.var_recent()
                still = st.still_for(now)
                h = st.health(now)
                col = HEALTH_COLOR.get(h, "")
                print(f"    {ch:>3} {finger:<7} {pad:<5} {now_v:>6} {lo_v:>6} {hi_v:>6} {rng:>6} "
                      f"{var:>7.1f} {still:>5.1f}s  {col}{h}{RESET}")
            print()
        print("  Tip: after a still baseline (5-10 s), bend EACH FINGER one at a time fully")
        print("       open <-> closed twice. Pads that stay in DEAD/STUCK after their finger")
        print("       wiggle are the bad ones.")

    def _summary(self) -> None:
        now = time.monotonic()
        print("\n========== Summary ==========")
        for side in ("L", "R"):
            print(f"\n  Glove-{side}:")
            bad: list[tuple[int, str, str, str, int]] = []
            for ch in range(self.n_ch):
                st = self.stats[side][ch]
                finger, pad = self.label[side].get(ch, ("—", "—"))
                h = st.health(now)
                if h in ("DEAD", "STUCK", "NOISY", "WEAK", "QUIET"):
                    bad.append((ch, finger, pad, h, st.range))
            if not bad:
                print("    all 10 pads look OK ✓")
                continue
            for ch, finger, pad, h, rng in bad:
                print(f"    ch{ch} {finger:<7} {pad:<5} {h:<6} session range={rng}")
            # Per-finger verdict
            per_finger: dict[str, list[str]] = {f: [] for f in FINGER_NAMES}
            for ch in range(self.n_ch):
                if ch not in self.label[side]:
                    continue
                finger, pad = self.label[side][ch]
                per_finger[finger].append(self.stats[side][ch].health(now))
            print(f"\n    per-finger verdict:")
            for finger in FINGER_NAMES:
                hs = per_finger.get(finger) or []
                if not hs:
                    continue
                bad_count = sum(1 for h in hs if h in ("DEAD", "STUCK", "QUIET", "WEAK"))
                if bad_count == 0:
                    verdict = "OK"
                elif bad_count == 1:
                    verdict = "DEGRADED (one pad bad — bend still partially detectable)"
                else:
                    verdict = "BROKEN (both pads bad — this finger is effectively a dead input)"
                print(f"      {finger:<7} {' / '.join(hs):<24} -> {verdict}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--secs", type=float, default=0.0,
                    help="0 = run until Ctrl+C; otherwise auto-finish after N seconds.")
    args = ap.parse_args()
    runner = PadHealth(args.secs)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        runner._summary()


if __name__ == "__main__":
    main()
