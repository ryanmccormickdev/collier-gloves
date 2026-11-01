"""Shared HUD formatting for terminal debug and preview GUI."""

from __future__ import annotations

import math

from ..conditioning import GloveSignals
from ..config import Config
from ..selection import ArmSelection, Selection


def finger_str(sig: GloveSignals) -> str:
    ff = sig.finger_flex[1:]
    return "".join(f"{int(f * 100):02d}" for f in ff)


def format_arm(sig: GloveSignals, arm: ArmSelection) -> str:
    if not sig.present:
        return "absent"
    h = f"{arm.heading:+5.0f}" if arm.heading is not None else "  -- "
    reg = arm.region if arm.region is not None else "-"
    cal = "cal" if arm.heading is not None else "UNC"
    hh = f"y{sig.h_heading:+5.0f}" if sig.h_heading == sig.h_heading else "y  -- "
    acc = f"acc{math.degrees(sig.rv_accuracy):4.0f}"
    return (f"hd{h} reg{reg} {cal} "
            f"{'PICK' if arm.picked else 'open'} "
            f"p{sig.pitch:+5.0f} r{sig.roll:+5.0f} {hh} "
            f"o{sig.openness:.2f} w{sig.wiggle:.2f} sf{sig.spawn_flutter:.2f} "
            f"g{sig.gyro_mag:4.1f} f{finger_str(sig)} "
            f"vv{sig.vert_vel:+5.2f} pr{sig.pitch_rate:+4.0f} up{sig.forward_up:+.2f} {acc}")


def _inspect_hand(sig: GloveSignals, arm: ArmSelection) -> list[str]:
    """A labelled multi-line readout of one hand's full signal panel."""
    if not sig.present:
        return ["  (absent — glove off / out of range / not yet seen)"]

    def trip(t: tuple[float, float, float], fmt: str = "{:+8.2f}") -> str:
        return " ".join(fmt.format(v) for v in t)

    region = arm.region if arm.region is not None else "-"
    heading = f"{arm.heading:+.0f}" if arm.heading is not None else "uncal"
    pose = "PICK" if arm.picked else "open"
    bend = "  ".join(
        f"{n}{int(b * 100):3d}%"
        for n, b in zip(("th", "ix", "md", "rg", "pk"), sig.finger_bend)
    )
    raw = " ".join(f"{v:4d}" for v in sig.raw_flex)
    drift = math.degrees(sig.rv_accuracy)
    return [
        f"  point: region {region}   heading(vs fwd) {heading:>6}   {pose}"
        f"   game_acc {sig.game_status}/3   drift~{drift:3.0f}deg",
        f"  ORIENT  deg     yaw {sig.yaw:+7.1f}   pitch {sig.pitch:+7.1f}"
        f"   roll {sig.roll:+7.1f}   (h_heading {sig.h_heading:+7.1f})"
        if sig.h_heading == sig.h_heading else
        f"  ORIENT  deg     yaw {sig.yaw:+7.1f}   pitch {sig.pitch:+7.1f}"
        f"   roll {sig.roll:+7.1f}   (h_heading   nan  )",
        f"  ANG VEL deg/s   {trip(sig.ang_vel_dps)}   |{sig.ang_vel_mag_dps:7.1f}|",
        f"  ANG ACC deg/s2  {trip(sig.ang_accel_dps2, '{:+8.0f}')}"
        f"   |{sig.ang_accel_mag_dps2:7.0f}|",
        f"  LIN ACC m/s2    {trip(sig.world_accel)}   |{sig.lin_accel_mag:7.2f}|"
        f"   (z=up)",
        f"  LIN VEL m/s     {trip(sig.world_vel)}   (z=up speed)",
        f"  FINGER BEND     {bend}   (ignore thumb)",
        f"  RAW ADC         {raw}",
    ]


def format_inspector(
    left: GloveSignals,
    right: GloveSignals,
    sel: Selection,
    calibrated: bool,
    cfg: Config,
    rec_state: dict | None = None,
) -> str:
    """Full live signal panel for `run.py --inspect`. Multi-line, screen-cleared.

    `rec_state` (optional) surfaces the RECOGNIZER's live pose/pick flags +
    calibration progress — important now that pose detection lives in the
    recognizer (gestures_scratch), not in `sel`. Shape:
        {"all_mode": bool, "timbre": bool,
         "pick": {"L": bool, "R": bool}, "calib": 0..1}
    """
    flags = []
    if rec_state:
        if rec_state.get("all_mode"):
            flags.append("ALL")
        if rec_state.get("timbre"):
            flags.append("TIMBRE")
        pk = rec_state.get("pick") or {}
        if pk.get("L"):
            flags.append("PICK-L")
        if pk.get("R"):
            flags.append("PICK-R")
        prog = rec_state.get("calib", 0.0) or 0.0
        if prog > 0.0:
            phase = "still" if prog < 0.5 else "knock"
            flags.append(f"CAL{int(prog * 100):3d}%·{phase}")
    else:
        if sel.all_mode:
            flags.append("ALL")
        if sel.timbre_pose:
            flags.append("TIMBRE")
    cal = "calibrated" if calibrated else "UNCALIBRATED (point fwd + flutter both hands)"
    out = [
        "=" * 78,
        f" COLLIER SIGNAL INSPECTOR    {cal}    {' '.join(flags)}",
        " (raw panel — see conditioning.py header for the meaning of every value)",
        " (flags above = your recognizer's live pose/pick state + CAL%)",
        "=" * 78,
        "LEFT",
        *_inspect_hand(left, sel.left),
        "-" * 78,
        "RIGHT",
        *_inspect_hand(right, sel.right),
        "=" * 78,
    ]
    return "\n".join(out)


def format_flags(
    left: GloveSignals,
    right: GloveSignals,
    sel: Selection,
    calibrated: bool,
    calib_progress: dict[str, float],
    cfg: Config,
) -> list[str]:
    flags: list[str] = []
    prog = max(calib_progress.values()) if calib_progress else 0.0
    if prog > 0.0:
        # combined two-hand calibration: still (pre-hold) -> knock (flutter)
        phase = "still" if prog < 0.5 else "knock"
        flags.append(f"CAL{int(prog * 100):3d}%·{phase}")
    else:
        for side, sig in (("L", left), ("R", right)):
            if sig.present and math.degrees(sig.rv_accuracy) > math.degrees(
                cfg.selection.heading_accuracy_warn_rad
            ):
                flags.append(f"DRIFT-{side}?")
    if sel.all_mode:
        flags.append("ALL")
    if sel.timbre_pose:
        flags.append("TIMBRE")
    return flags
