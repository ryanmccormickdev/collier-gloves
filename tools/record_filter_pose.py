#!/usr/bin/env python3
"""Record a steady hand pose from the live Collier conditioner.

Use this to capture the IMU/orientation center (and spread) for gating
``/expr/filters`` streaming: hold your pose (e.g. palms on chest), stay
still, and the script writes a JSON profile you can paste into config.

Run:
  python tools/record_filter_pose.py --name chest
  python tools/record_filter_pose.py --name chest --secs 4 --margin-deg 20

Workflow:
  1. Connect both gloves.
  2. Live preview shows pitch / roll / gyro until you press Enter.
  3. Hold the pose; countdown, then ~3 s of samples.
  4. Prints per-hand means and suggested [lo, hi] bands; saves JSON under
     ``artifacts/reports/pose_profiles/<name>.json``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host"  # collier lives under host/ in this repo
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from collier.conditioning import GloveConditioner
from collier.config import Config
from collier.frames import RawFrame
from collier.transport import GloveTransport

OUT_DIR = ROOT / "artifacts" / "reports" / "pose_profiles"

# Fields stored for the future filter-pose gate (Collier signals, degrees
# where noted in the conditioner docs).
POSE_FIELDS = (
    "pitch",
    "roll",
    "forward_up",
    "gyro_mag",
    "lin_accel_mag",
    "openness",
    "vert_vel",
)


@dataclass
class Sample:
    t: float
    side: str
    values: dict[str, float] = field(default_factory=dict)


def _sig_values(sig) -> dict[str, float]:
    if sig is None or not sig.present:
        return {}
    out: dict[str, float] = {}
    for name in POSE_FIELDS:
        v = getattr(sig, name, None)
        if v is not None:
            out[name] = float(v)
    out["is_open"] = 1.0 if sig.is_open else 0.0
    out["hand_steady"] = 1.0 if sig.hand_steady else 0.0
    fb = sig.finger_bend or ()
    for i, name in enumerate(("bend_thumb", "bend_index", "bend_middle", "bend_ring", "bend_pinky")):
        if i < len(fb):
            out[name] = float(fb[i])
    return out


def _summarize(samples: list[Sample], field: str) -> dict | None:
    xs = [s.values[field] for s in samples if field in s.values]
    if not xs:
        return None
    a = np.asarray(xs, dtype=np.float64)
    return {
        "mean": float(a.mean()),
        "std": float(a.std()),
        "min": float(a.min()),
        "max": float(a.max()),
        "n": len(xs),
    }


def _suggest_band(stats: dict, margin: float, min_halfwidth: float) -> tuple[float, float]:
    half = max(margin, min_halfwidth, 3.0 * stats["std"])
    c = stats["mean"]
    return (c - half, c + half)


class PoseRecorder:
    def __init__(self, name: str, hold_s: float, margin_deg: float, min_halfwidth_deg: float):
        self.cfg = Config()
        self.name = name
        self.hold_s = hold_s
        self.margin_deg = margin_deg
        self.min_halfwidth_deg = min_halfwidth_deg
        n_ch = self.cfg.packet.flex_count
        c_cfg = self.cfg.conditioning
        s_cfg = self.cfg.selection
        self._cond = {
            "L": GloveConditioner("L", n_ch, c_cfg, s_cfg),
            "R": GloveConditioner("R", n_ch, c_cfg, s_cfg),
        }
        self._latest: dict[str, RawFrame | None] = {"L": None, "R": None}
        self._transport = GloveTransport(
            self.cfg.ble, self.cfg.packet, self._on_frame
        )

    def _on_frame(self, frame: RawFrame) -> None:
        self._latest[frame.side] = frame

    def _fresh(self, side: str, t: float) -> RawFrame | None:
        f = self._latest[side]
        if f is None or t - f.recv_t > self.cfg.process.stale_after_s:
            return None
        return f

    async def run(self) -> None:
        await asyncio.gather(self._transport.run(), self._main())

    async def _main(self) -> None:
        print("Waiting for glove packets (L and R)…")
        while self._latest["L"] is None or self._latest["R"] is None:
            await asyncio.sleep(0.1)
        print("Both gloves connected.\n")

        await self._preview_until_enter()
        samples = await self._record_hold()
        profile = self._build_profile(samples)
        self._print_report(profile)
        self._save(profile)

    async def _preview_until_enter(self) -> None:
        print(
            "Preview — move into your pose and hold steady.\n"
            "  pitch / roll are in degrees (same as Collier HUD).\n"
            "  Press Enter when ready to record.\n"
        )
        dt = self.cfg.process.dt
        last_line = 0.0
        while True:
            # Non-blocking Enter check via asyncio.to_thread on input is awkward;
            # use short sleep + stdin in executor.
            if sys.stdin in (await self._stdin_ready()):
                sys.stdin.readline()
                print()
                return
            t = time.monotonic()
            parts = []
            for side in ("L", "R"):
                raw = self._fresh(side, t)
                sig = self._cond[side].process(raw, dt)
                if sig.present:
                    parts.append(
                        f"{side} p={sig.pitch:+6.1f} r={sig.roll:+6.1f} "
                        f"g={sig.gyro_mag:.2f} open={sig.openness:.2f}"
                    )
                else:
                    parts.append(f"{side} —")
            line = "  |  ".join(parts)
            if t - last_line >= 0.12:
                print(f"\r{line:<72}", end="", flush=True)
                last_line = t
            await asyncio.sleep(0.05)

    async def _stdin_ready(self):
        loop = asyncio.get_event_loop()
        try:
            import select

            ready, _, _ = await loop.run_in_executor(
                None, lambda: select.select([sys.stdin], [], [], 0.05)
            )
            return ready
        except Exception:
            await asyncio.sleep(0.2)
            return []

    async def _record_hold(self) -> list[Sample]:
        dt = self.cfg.process.dt
        for n in (3, 2, 1):
            print(f"Hold pose steady … {n}")
            await asyncio.sleep(0.8)
        print(f"Recording {self.hold_s:.1f}s …\n")
        samples: list[Sample] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.hold_s:
            t = time.monotonic()
            for side in ("L", "R"):
                raw = self._fresh(side, t)
                sig = self._cond[side].process(raw, dt)
                if sig.present:
                    samples.append(Sample(t=t - t0, side=side, values=_sig_values(sig)))
            await asyncio.sleep(max(0.0, dt - 0.001))
        print(f"Captured {len(samples)} samples "
              f"({sum(1 for s in samples if s.side == 'L')} L, "
              f"{sum(1 for s in samples if s.side == 'R')} R).\n")
        return samples

    def _build_profile(self, samples: list[Sample]) -> dict:
        by_side: dict[str, list[Sample]] = {"L": [], "R": []}
        for s in samples:
            by_side.setdefault(s.side, []).append(s)

        profile: dict = {
            "name": self.name,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hold_s": self.hold_s,
            "margin_deg": self.margin_deg,
            "min_halfwidth_deg": self.min_halfwidth_deg,
            "fields": list(POSE_FIELDS),
            "hands": {},
            "suggested_gate": {},
        }

        for side in ("L", "R"):
            ss = by_side.get(side) or []
            hand: dict = {"sample_count": len(ss), "stats": {}}
            gate: dict = {}
            for fld in POSE_FIELDS:
                st = _summarize(ss, fld)
                if st is None:
                    continue
                hand["stats"][fld] = st
                if fld in ("pitch", "roll"):
                    lo, hi = _suggest_band(st, self.margin_deg, self.min_halfwidth_deg)
                    gate[fld] = {"center": st["mean"], "lo": lo, "hi": hi}
                elif fld == "gyro_mag":
                    # Steady gate: allow up to mean + margin (or 3σ), at least 0.8
                    cap = max(
                        st["mean"] + self.margin_deg * 0.05,
                        st["mean"] + 3.0 * st["std"],
                        0.8,
                    )
                    gate["gyro_mag_max"] = float(cap)
                elif fld == "lin_accel_mag":
                    cap = max(st["mean"] + 0.4, st["mean"] + 3.0 * st["std"], 1.2)
                    gate["lin_accel_mag_max"] = float(cap)
            profile["hands"][side] = hand
            profile["suggested_gate"][side] = gate

        return profile

    def _print_report(self, profile: dict) -> None:
        print(f"=== Pose profile: {profile['name']!r} ===\n")
        for side in ("L", "R"):
            hand = profile["hands"].get(side) or {}
            n = hand.get("sample_count", 0)
            if n == 0:
                print(f"  [{side}] no samples\n")
                continue
            print(f"  [{side}] {n} samples")
            stats = hand.get("stats") or {}
            for fld in ("pitch", "roll", "forward_up", "gyro_mag", "lin_accel_mag", "openness"):
                st = stats.get(fld)
                if not st:
                    continue
                print(
                    f"        {fld:<16} mean={st['mean']:+.2f}  "
                    f"std={st['std']:.2f}  min={st['min']:+.2f}  max={st['max']:+.2f}"
                )
            gate = profile["suggested_gate"].get(side) or {}
            if "pitch" in gate:
                p = gate["pitch"]
                print(
                    f"        → pitch band [{p['lo']:+.1f}, {p['hi']:+.1f}]  "
                    f"(center {p['center']:+.1f})"
                )
            if "roll" in gate:
                r = gate["roll"]
                print(
                    f"        → roll  band [{r['lo']:+.1f}, {r['hi']:+.1f}]  "
                    f"(center {r['center']:+.1f})"
                )
            if "gyro_mag_max" in gate:
                print(f"        → gyro_mag_max ≤ {gate['gyro_mag_max']:.2f} rad/s")
            if "lin_accel_mag_max" in gate:
                print(f"        → lin_accel_mag_max ≤ {gate['lin_accel_mag_max']:.2f} m/s²")
            print()

        print("Copy-paste starter (per-hand bands) for a future FilterPoseConfig:\n")
        for side in ("L", "R"):
            g = profile["suggested_gate"].get(side) or {}
            if not g:
                continue
            print(f"  # {side}")
            if "pitch" in g:
                print(f"  {side.lower()}_pitch_lo: float = {g['pitch']['lo']:.2f}")
                print(f"  {side.lower()}_pitch_hi: float = {g['pitch']['hi']:.2f}")
            if "roll" in g:
                print(f"  {side.lower()}_roll_lo: float = {g['roll']['lo']:.2f}")
                print(f"  {side.lower()}_roll_hi: float = {g['roll']['hi']:.2f}")
            if "gyro_mag_max" in g:
                print(f"  {side.lower()}_gyro_mag_max: float = {g['gyro_mag_max']:.2f}")
            print()

    def _save(self, profile: dict) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{profile['name']}.json"
        path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        print(f"Saved → {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--name",
        required=True,
        help="Pose label (e.g. chest) — used for the output filename.",
    )
    ap.add_argument(
        "--secs",
        type=float,
        default=3.0,
        help="Seconds to record while holding the pose (default 3).",
    )
    ap.add_argument(
        "--margin-deg",
        type=float,
        default=18.0,
        help="Half-width for pitch/roll bands if std is small (default 18°).",
    )
    ap.add_argument(
        "--min-halfwidth-deg",
        type=float,
        default=12.0,
        help="Minimum half-width for pitch/roll even when very steady (default 12°).",
    )
    args = ap.parse_args()
    rec = PoseRecorder(
        name=args.name,
        hold_s=args.secs,
        margin_deg=args.margin_deg,
        min_halfwidth_deg=args.min_halfwidth_deg,
    )
    try:
        asyncio.run(rec.run())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
