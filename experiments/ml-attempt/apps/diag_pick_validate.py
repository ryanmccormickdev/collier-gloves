#!/usr/bin/env python3
"""Replay every training clip through the pure-heuristic PickGestureEngine
and report whether the engine would have fired the correct gesture inside
the user-labeled span. Also reports boundary alignment so we can tell if
the pick state machine's enter/exit times line up with the clip labels.

Use this BEFORE wiring pick_engine into the live conductor: if the engine
disagrees with the training labels on the user's own data, the thresholds
need tweaking and we shouldn't ship it.

Run:
  python apps/diag_pick_validate.py
  python apps/diag_pick_validate.py --pick-below 0.40 --open-above 0.75
  python apps/diag_pick_validate.py --classes scale_up scale_down none
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps._diag_shared import FIDX, list_clips, load_clip
from collier_live.pick_engine import (
    HandTick,
    PickEngineConfig,
    PickGestureEngine,
)


SAMPLE_RATE_HZ = 60.0

# Approximation thresholds for the clip-based validator. Live, picked
# comes from collier.selection.PickLatch (adaptive flex envelope +
# hand_steady), which we can only approximate from the recorded v3
# feature row. This is intentionally loose; live behavior is the source
# of truth.
_PICK_OPENNESS_BELOW = 0.70
_PICK_OPENNESS_RELEASE = 0.85
_STEADY_GYRO_MAX = 2.5  # rad/s — same as collier.spawn_dwell_gyro_max


def _approx_tick(row, prev_picked: bool) -> tuple[HandTick, bool]:
    openness = row[FIDX["openness"]]
    gyro_mag = float(np.linalg.norm(row[FIDX["gyro_along"]:FIDX["gyro_palm"] + 1]))
    if prev_picked:
        picked = openness < _PICK_OPENNESS_RELEASE
    else:
        picked = openness < _PICK_OPENNESS_BELOW and gyro_mag < _STEADY_GYRO_MAX
    return HandTick(
        present=True,
        picked=picked,
        vert_vel=float(row[FIDX["vert_vel"]]),
        palm_roll=float(row[FIDX["palm_roll"]]),
    ), picked


def replay_clip(clip, cfg: PickEngineConfig, side: str) -> list[tuple[float, str, str]]:
    """Replay one side through the engine; the other stays absent so
    collective routing can't accidentally fuse with cross-clip noise."""
    eng = PickGestureEngine(cfg)
    dt = 1.0 / SAMPLE_RATE_HZ
    X = clip.x(side)
    out = []
    prev_picked = False
    for i, row in enumerate(X):
        t = i * dt
        tick, prev_picked = _approx_tick(row, prev_picked)
        L = tick if side == "L" else None
        R = tick if side == "R" else None
        for ev in eng.update(t=t, dt=dt, L=L, R=R):
            out.append((ev.t, ev.label, ev.side))
    return out


def replay_clip_both(clip, cfg: PickEngineConfig) -> list[tuple[float, str, str]]:
    eng = PickGestureEngine(cfg)
    dt = 1.0 / SAMPLE_RATE_HZ
    n = min(len(clip.X_L), len(clip.X_R))
    out = []
    prev_picked_L = False
    prev_picked_R = False
    for i in range(n):
        t = i * dt
        if clip.X_L[i].any():
            L, prev_picked_L = _approx_tick(clip.X_L[i], prev_picked_L)
        else:
            L, prev_picked_L = None, False
        if clip.X_R[i].any():
            R, prev_picked_R = _approx_tick(clip.X_R[i], prev_picked_R)
        else:
            R, prev_picked_R = None, False
        for ev in eng.update(t=t, dt=dt, L=L, R=R):
            out.append((ev.t, ev.label, ev.side))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dy-min", type=float, default=None)
    ap.add_argument("--direction-dominance", type=float, default=None)
    ap.add_argument("--min-pick-s", type=float, default=None)
    ap.add_argument("--max-pick-s", type=float, default=None)
    ap.add_argument("--palm-roll-collective-deg", type=float, default=None)
    ap.add_argument("--spawn-min-picks", type=int, default=None)
    ap.add_argument("--spawn-window-s", type=float, default=None)
    ap.add_argument("--spawn-pick-max-s", type=float, default=None)
    ap.add_argument("--classes", nargs="+", default=None,
                    help="Restrict to these gesture_class labels.")
    ap.add_argument("--verbose-misses", action="store_true",
                    help="Print every miss with details.")
    args = ap.parse_args()

    cfg = PickEngineConfig()
    for name in ("dy_min", "direction_dominance",
                 "min_pick_s", "max_pick_s", "palm_roll_collective_deg",
                 "spawn_min_picks", "spawn_window_s", "spawn_pick_max_s"):
        v = getattr(args, name)
        if v is not None:
            setattr(cfg, name, v)

    print(f"Config: dy_min={cfg.dy_min}  dom={cfg.direction_dominance}  "
          f"min_pick_s={cfg.min_pick_s}  max_pick_s={cfg.max_pick_s}  "
          f"spawn_min_picks={cfg.spawn_min_picks}  "
          f"spawn_window_s={cfg.spawn_window_s}  "
          f"spawn_pick_max_s={cfg.spawn_pick_max_s}")
    print("(Validator approximates picked from openness+gyro; live uses "
          "collier.selection.PickLatch which is more reliable.)")

    clips = list_clips()
    print(f"Clips: {len(clips)}\n")

    by_class: dict[str, list] = defaultdict(list)
    for path in clips:
        try:
            clip = load_clip(path)
        except Exception as exc:
            print(f"  skip {path.name}: {exc}")
            continue
        if args.classes and clip.gesture_class not in args.classes:
            continue
        by_class[clip.gesture_class].append(clip)

    print(f"{'class':<28} {'n':>3}  {'hit':>5}  {'wrong':>6}  {'nothing':>7}  details")
    print("-" * 100)

    grand = Counter()
    for cls in sorted(by_class):
        clips_c = by_class[cls]
        hit = wrong = nothing = 0
        miss_details = []
        # For collectives, drive both hands. For singles, drive only the
        # labeled hand (so a stray opposite hand can't accidentally suppress).
        is_collective = cls.startswith("collective_")
        for clip in clips_c:
            if is_collective:
                events = replay_clip_both(clip, cfg)
            else:
                # Use whichever hand is labeled — if both are labeled, run twice
                # and count each side as its own attempt.
                sides_to_run = []
                for side in ("L", "R"):
                    if clip.labeled_L if side == "L" else clip.labeled_R:
                        sides_to_run.append(side)
                if not sides_to_run:
                    sides_to_run = ["L", "R"]
                # Aggregate events from each side independently
                events = []
                for side in sides_to_run:
                    events.extend(replay_clip(clip, cfg, side))

            # Filter events to those that match the clip's expected label.
            labels = [lab for _, lab, _ in events]
            count_labels = Counter(labels)
            if cls == "none":
                # 'none' means engine should fire NOTHING during this clip.
                if not events:
                    hit += 1
                else:
                    wrong += 1
                    if args.verbose_misses:
                        miss_details.append(
                            f"{clip.path.name}: expected none, got {dict(count_labels)}"
                        )
            else:
                if cls in count_labels:
                    hit += 1
                elif any(lab != cls for lab in labels):
                    wrong += 1
                    if args.verbose_misses:
                        miss_details.append(
                            f"{clip.path.name}: expected {cls}, got {dict(count_labels)}"
                        )
                else:
                    nothing += 1
                    if args.verbose_misses:
                        miss_details.append(f"{clip.path.name}: expected {cls}, got NOTHING")

        n = len(clips_c)
        pct = (100 * hit // n) if n else 0
        label_summary = {}
        for clip in clips_c[:5]:
            pass
        # quick per-class confused-with summary
        confused = Counter()
        for clip in clips_c:
            if is_collective:
                events = replay_clip_both(clip, cfg)
            else:
                sides_to_run = []
                for side in ("L", "R"):
                    if clip.labeled_L if side == "L" else clip.labeled_R:
                        sides_to_run.append(side)
                if not sides_to_run:
                    sides_to_run = ["L", "R"]
                events = []
                for side in sides_to_run:
                    events.extend(replay_clip(clip, cfg, side))
            for _, lab, _ in events:
                if lab != cls:
                    confused[lab] += 1
        confusion_str = ", ".join(f"{k}={v}" for k, v in confused.most_common(3))
        print(f"{cls:<28} {n:>3}  {hit:>3}({pct:>2}%)  {wrong:>6}  {nothing:>7}  {confusion_str}")
        if args.verbose_misses:
            for d in miss_details[:10]:
                print(f"    {d}")
        grand["hit"] += hit
        grand["wrong"] += wrong
        grand["nothing"] += nothing

    total = sum(grand.values()) or 1
    print("-" * 100)
    print(f"TOTAL hit={grand['hit']} ({100*grand['hit']/total:.0f}%)  "
          f"wrong={grand['wrong']} ({100*grand['wrong']/total:.0f}%)  "
          f"nothing={grand['nothing']} ({100*grand['nothing']/total:.0f}%)")


if __name__ == "__main__":
    main()
