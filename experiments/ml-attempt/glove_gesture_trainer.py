#!/usr/bin/env python3
"""
Glove gesture trainer — standalone model-training cycle for the gesture layer.

A single PyQt6 window (same look/feel as glove_pose_trainer.py — no 3D model)
that walks the whole gesture-recognition training loop end to end, with its own
self-contained BLE, signal conditioning, calibration, dataset and model code. It
deliberately does NOT depend on the `collier` package or the `apps/*` modules —
the equivalent math is copied in here so this app stands completely on its own.

The five tabs are the cycle:
    1. Connect & Calibrate  — pair both gloves, point-forward calibration.
    2. Record               — capture takes (record casual noise around the gesture).
    3. Label                — scrub each clip, drag-select the gesture span; the rest
                              becomes background ("none") training data.
    4. Train                — window the labelled takes, train the PyTorch TCN, see metrics.
    5. Live Test            — run the trained model live, watch predictions.

Everything is stored under ./gesture_trainer_data/ (clips, calibration, models)
so it never touches the existing collier data pipeline.

Install (~/glove-env):
    pip install bleak pyqt6 pyqtgraph qasync numpy torch

Diagnose training (no UI):
    python scripts/gesture_train_diagnose.py --side L

Run:
    source ~/glove-env/bin/activate
    python glove_gesture_trainer.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import qasync
from bleak import BleakClient, BleakScanner

from qt_env import bootstrap_qt

bootstrap_qt()
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets


# ===========================================================================
# Paths
# ===========================================================================
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "gesture_trainer_data"
CLIPS_DIR = DATA_DIR / "clips"
CALIB_DIR = DATA_DIR / "calibration"
MODELS_DIR = DATA_DIR / "models"
for _d in (CLIPS_DIR, CALIB_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ===========================================================================
# BLE / packet decode  (v3 firmware: "<BBH4f3f3f10H4ffBB" -> 86 bytes)
# ===========================================================================
SERVICE_UUID = "5a3e8f00-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
CHAR_UUID = "5a3e8f01-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
PACKET_FMT_V3 = "<BBH4f3f3f10H4ffBB"
PACKET_FMT_V2 = "<BBH4f3f3f10H"
SIZE_V3 = struct.calcsize(PACKET_FMT_V3)
SIZE_V2 = struct.calcsize(PACKET_FMT_V2)
N_CHANNELS = 10
GLOVE_NAMES = ("Glove-L", "Glove-R")


@dataclass
class RawFrame:
    """One decoded packet from one glove."""

    side: str
    seq: int
    quat: tuple[float, float, float, float]
    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]
    flex: tuple[int, ...]
    recv_t: float
    rv_accuracy: float = 0.0
    game_status: int = 3


def decode_packet(side: str, data: bytes) -> RawFrame | None:
    """Decode v3 (86 B) or v2 (64 B) packets. v2 defaults heading-health to good."""
    n = len(data)
    if n == SIZE_V3:
        f = struct.unpack(PACKET_FMT_V3, data)
        rv_accuracy = float(f[27])
        game_status = int(f[29])
    elif n == SIZE_V2:
        f = struct.unpack(PACKET_FMT_V2, data)
        rv_accuracy = 0.0
        game_status = 3
    else:
        return None
    return RawFrame(
        side=side,
        seq=int(f[1]),
        quat=(f[3], f[4], f[5], f[6]),
        accel=(f[7], f[8], f[9]),
        gyro=(f[10], f[11], f[12]),
        flex=tuple(int(v) for v in f[13:23]),
        recv_t=time.monotonic(),
        rv_accuracy=rv_accuracy,
        game_status=game_status,
    )


class GloveLink:
    """Latest-frame holder + BLE maintenance for one glove (direct bleak)."""

    def __init__(self, name: str, side: str):
        self.name = name
        self.side = side
        self.connected = False
        self.latest: RawFrame | None = None

    def _handler(self, _sender, data: bytearray) -> None:
        frame = decode_packet(self.side, bytes(data))
        if frame is not None:
            self.latest = frame

    async def maintain(self) -> None:
        while True:
            try:
                dev = await BleakScanner.find_device_by_name(self.name, timeout=8.0)
                if dev is None:
                    await asyncio.sleep(1.0)
                    continue
                async with BleakClient(dev) as cli:
                    self.connected = True
                    await cli.start_notify(CHAR_UUID, self._handler)
                    while cli.is_connected:
                        await asyncio.sleep(0.2)
            except Exception as exc:  # noqa: BLE001 — never crash the show
                print(f"[{self.name}] {exc!r}")
            finally:
                self.connected = False
                await asyncio.sleep(0.6)

    def fresh(self, now: float, stale_after_s: float) -> RawFrame | None:
        f = self.latest
        if f is None or (now - f.recv_t) > stale_after_s:
            return None
        return f


# ===========================================================================
# DSP primitives  (copied, dependency-free)
# ===========================================================================
def _tau_to_alpha(tau: float, dt: float) -> float:
    if tau <= 0.0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)


def wrap180(deg: float) -> float:
    d = (deg + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def _clamp(x: float, lo: float, hi: float) -> float:
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
        self.y += _tau_to_alpha(self.tau, dt) * (x - self.y)
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
            a = _tau_to_alpha(self.tau, dt)
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
        self.env += _tau_to_alpha(tau, dt) * (mag - self.env)
        return self.env


class EnvelopeMinMax:
    """Drift-adaptive normalizer: snaps to extremes, relaxes inward slowly."""

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
        return _clamp((x - self.lo) / span, 0.0, 1.0)


class LeakyVelocity:
    """Integrate accel into a self-zeroing velocity with slow bias removal."""

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


# ===========================================================================
# Orientation math  (copied)
# ===========================================================================
# IMU is upside-down on the PCB; this fixed quaternion maps sensor frame -> hand
# frame (same value as glove_visualizer / glove_cello — do not change lightly).
IMU_MOUNT_CORRECTION = (0.0, 0.7071, -0.7071, 0.0)

# Euler names from quat_to_euler_deg (ZYX) AFTER mount correction do NOT match
# aviation intuition on this hardware. Use semantic names everywhere in ML/UI:
#   internal roll  -> wrist_tilt   (nod: fingers toward ceiling / floor)
#   internal pitch -> palm_roll    (twist palm left / right)
#   internal yaw   -> room heading (excluded from gesture ML features)


def _q_inv(q):
    w, x, y, z = q
    return (w, -x, -y, -z)


def _q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def apply_mount_correction(q):
    m = IMU_MOUNT_CORRECTION
    return _q_mul(_q_mul(m, q), _q_inv(m))


def quat_to_euler_deg(qw, qx, qy, qz):
    sinr_cosp = 2.0 * (qw * qx + qy * qz)
    cosr_cosp = 1.0 - 2.0 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    sinp = max(-1.0, min(1.0, 2.0 * (qw * qy - qz * qx)))
    pitch = math.asin(sinp)
    siny_cosp = 2.0 * (qw * qz + qx * qy)
    cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def rotate_vector_to_world(q, v, *, mount_correct: bool = False):
    if mount_correct:
        q = apply_mount_correction(q)
    qw, qx, qy, qz = q
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    rx = vx + qw * tx + (qy * tz - qz * ty)
    ry = vy + qw * ty + (qz * tx - qx * tz)
    rz = vz + qw * tz + (qx * ty - qy * tx)
    return rx, ry, rz


def rotate_vector_to_body(q, v, *, mount_correct: bool = False):
    """Rotate a sensor/hand-frame vector into the mount-corrected body frame."""
    if mount_correct:
        q = apply_mount_correction(q)
    return rotate_vector_to_world(_q_inv(q), v, mount_correct=False)


def body_basis_vectors(body_quat: tuple[float, float, float, float]):
    """Unit along-fingers (X), across-hand (Y), palm-normal (Z) in world frame."""
    along = rotate_vector_to_world(body_quat, (1.0, 0.0, 0.0), mount_correct=False)
    across = rotate_vector_to_world(body_quat, (0.0, 1.0, 0.0), mount_correct=False)
    palm = rotate_vector_to_world(body_quat, (0.0, 0.0, 1.0), mount_correct=False)
    return along, across, palm


def magnitude(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def horizontal_heading_deg(q, body_forward=(1.0, 0.0, 0.0)):
    wx, wy, _wz = rotate_vector_to_world(q, body_forward, mount_correct=False)
    if math.hypot(wx, wy) < 0.05:
        return float("nan")
    return math.degrees(math.atan2(wy, wx))


# ===========================================================================
# Conditioning config + finger maps  (copied from collier defaults)
# ===========================================================================
FINGER_NAMES = ("thumb", "index", "middle", "ring", "pinky")
FINGER_CHANNELS_L = {
    "thumb": (5, 2), "index": (7, 4), "middle": (0, 3), "ring": (1, 6), "pinky": (9, 8),
}
FINGER_CHANNELS_R = {
    "thumb": (9, 8), "index": (4, 3), "middle": (6, 0), "ring": (7, 1), "pinky": (5, 2),
}

RATE_HZ = 60.0
DT = 1.0 / RATE_HZ
STALE_AFTER_S = 0.4


@dataclass
class CondCfg:
    flex_env_decay_per_s: float = 0.0017
    flex_min_span: float = 80.0
    flex_bend_is_high: bool = True
    gyro_tau: float = 0.03
    orient_tau: float = 0.02
    openness_tau: float = 0.08
    wiggle_hp_tau: float = 0.08
    wiggle_env_attack_s: float = 0.03
    wiggle_env_release_s: float = 0.10
    twinkle_finger_osc_thresh: float = 0.04
    twinkle_min_active_fingers: int = 2
    twinkle_still_gyro_max: float = 3.5
    twinkle_randomness_gain: float = 4.0
    spawn_flutter_hp_tau: float = 0.06
    spawn_flutter_gain: float = 2.8
    spawn_still_gyro_max: float = 5.0
    spawn_env_attack_s: float = 0.02
    spawn_env_release_s: float = 0.08
    vel_bias_tau: float = 1.1
    vel_zero_gyro: float = 0.35
    vel_zero_accel: float = 0.5
    vel_zero_dwell_s: float = 0.15
    pitch_rate_tau: float = 0.05
    pitch_rate_spike_deg_s: float = 120.0
    finger_open_max: float = 0.50


@dataclass
class SelCfg:
    n_regions: int = 4
    plane_half_deg: float = 90.0
    region_hysteresis_deg: float = 0.0  # unused; kept for old saved configs
    forward_offset_deg: float = 0.0
    finger_open_max: float = 0.50
    # Two-hand point-forward calibration gate (level = limited wrist nod).
    calib_level_tilt_tol_deg: float = 30.0
    calib_steady_gyro_max: float = 0.8
    calib_max_world_accel: float = 1.2
    calib_min_game_status: int = 2
    calib_pre_still_s: float = 1.0
    calib_knock_cycles: int = 3
    calib_knock_total_s: float = 6.0
    calib_pick_below: float = 0.70
    calib_open_above: float = 0.85
    # Whole-hand pose features (computed consistently at record + inference).
    all_palm_up_roll_deg: float = 55.0
    timbre_fingers_up_min: float = 0.42


@dataclass
class GloveSignals:
    side: str
    present: bool
    # Semantic orientation (ML / UI) — see orientation comment block above.
    wrist_tilt: float = 0.0
    palm_roll: float = 0.0
    wrist_tilt_rate: float = 0.0
    palm_roll_rate: float = 0.0
    forward_up: float = 0.0
    openness: float = 1.0
    # Hand-frame kinematics (heading-invariant).
    hand_gyro: tuple[float, float, float] = (0.0, 0.0, 0.0)
    hand_accel: tuple[float, float, float] = (0.0, 0.0, 0.0)
    vert_vel: float = 0.0
    vel_along: float = 0.0
    vel_across: float = 0.0
    vel_horiz_mag: float = 0.0
    lin_accel_mag: float = 0.0
    finger_bend: tuple[float, ...] = (0.0,) * 5
    finger_osc: tuple[float, ...] = (0.0,) * 5
    raw_flex: tuple[int, ...] = (0,) * N_CHANNELS
    h_heading: float = float("nan")
    is_open: bool = True
    # Legacy aliases (debug / calib helpers): pitch=palm_roll, roll=wrist_tilt.
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    pitch_rate: float = 0.0
    gyro_mag: float = 0.0
    world_accel: tuple[float, float, float] = (0.0, 0.0, 0.0)
    wiggle: float = 0.0
    spawn_flutter: float = 0.0
    game_status: int = 0
    rv_accuracy: float = 0.0


class GloveConditioner:
    """Stateful per-glove conditioner — one RawFrame -> one GloveSignals."""

    def __init__(self, side: str, cfg: CondCfg):
        self.side = side
        self.cfg = cfg
        self._channels = FINGER_CHANNELS_L if side == "L" else FINGER_CHANNELS_R
        self._flex_norm = [EnvelopeMinMax(cfg.flex_env_decay_per_s, cfg.flex_min_span)
                           for _ in range(N_CHANNELS)]
        # Fixed per-channel ADC range from the "wiggle" calibration. When set, flex
        # is mapped raw -> (raw-lo)/(hi-lo) clamped to 0..1 instead of the rolling
        # adaptive envelope, so bend percentages are stable across the session.
        self._flex_lo: list[float] | None = None
        self._flex_hi: list[float] | None = None
        self._finger_hp = [HighPass(cfg.wiggle_hp_tau) for _ in FINGER_NAMES]
        self._osc_hp = [HighPass(cfg.wiggle_hp_tau) for _ in FINGER_NAMES]
        self._spawn_hp = [HighPass(cfg.spawn_flutter_hp_tau) for _ in FINGER_NAMES]
        self._gyro = [OnePole(cfg.gyro_tau) for _ in range(3)]
        self._yaw = OnePole(cfg.orient_tau)
        self._pitch = OnePole(cfg.orient_tau)
        self._roll = OnePole(cfg.orient_tau)
        self._h_heading = CircularOnePole(cfg.orient_tau)
        self._openness = OnePole(cfg.openness_tau, initial=1.0)
        self._wiggle_env = EnvelopeFollower(cfg.wiggle_env_attack_s, cfg.wiggle_env_release_s)
        self._spawn_env = EnvelopeFollower(cfg.spawn_env_attack_s, cfg.spawn_env_release_s)
        self._wvel = [LeakyVelocity(cfg.vel_bias_tau) for _ in range(3)]
        self._still_t = 0.0
        self._prev_pitch: float | None = None
        self._prev_roll: float | None = None
        self._pitch_rate_lp = OnePole(cfg.pitch_rate_tau)
        self._wrist_tilt_rate_lp = OnePole(cfg.pitch_rate_tau)

    def set_flex_range(self, lo, hi) -> None:
        if lo is None or hi is None:
            self._flex_lo = self._flex_hi = None
            return
        self._flex_lo = [float(v) for v in lo]
        self._flex_hi = [float(v) for v in hi]

    def get_flex_range(self) -> dict[str, list[float]] | None:
        if not self.has_flex_range:
            return None
        return {"lo": list(self._flex_lo), "hi": list(self._flex_hi)}

    @property
    def has_flex_range(self) -> bool:
        return self._flex_lo is not None

    def _normalize_flex(self, raw: float, i: int, dt: float) -> float:
        if self._flex_lo is not None:
            span = self._flex_hi[i] - self._flex_lo[i]
            if span <= 1e-6:
                return 0.5
            return _clamp((raw - self._flex_lo[i]) / span, 0.0, 1.0)
        # Uncalibrated fallback: rolling adaptive envelope (collier behaviour).
        return self._flex_norm[i].update(raw, dt)

    def process(self, frame: RawFrame | None, dt: float) -> GloveSignals:
        empty = tuple(0.0 for _ in FINGER_NAMES)
        if frame is None:
            return GloveSignals(
                side=self.side, present=False, finger_bend=empty, finger_osc=empty,
                raw_flex=tuple(0 for _ in range(N_CHANNELS)), h_heading=float("nan"),
            )

        c = self.cfg
        body_quat = apply_mount_correction(frame.quat)
        yaw, pitch, roll = quat_to_euler_deg(*body_quat)
        yaw = self._yaw.update(yaw, dt)
        pitch = self._pitch.update(pitch, dt)
        roll = self._roll.update(roll, dt)
        wrist_tilt = roll
        palm_roll = pitch

        raw_h = horizontal_heading_deg(frame.quat)
        h_heading = raw_h if math.isnan(raw_h) else self._h_heading.update(raw_h, dt)

        gyro = tuple(self._gyro[i].update(frame.gyro[i], dt) for i in range(3))
        gyro_mag = magnitude(gyro)
        hand_gyro = rotate_vector_to_body(frame.quat, gyro, mount_correct=True)

        world_accel = rotate_vector_to_world(frame.quat, frame.accel, mount_correct=False)
        ax, ay, az = world_accel
        lin_accel_mag = magnitude(world_accel)
        hand_accel = rotate_vector_to_body(frame.quat, frame.accel, mount_correct=True)
        still = (gyro_mag < c.vel_zero_gyro and abs(az) < c.vel_zero_accel
                 and math.hypot(ax, ay) < c.vel_zero_accel)
        self._still_t = self._still_t + dt if still else 0.0
        zero = self._still_t >= c.vel_zero_dwell_s
        world_vel = tuple(self._wvel[i].update(world_accel[i], dt, zero=zero) for i in range(3))
        vert_vel = world_vel[2]
        vel_horiz_mag = math.hypot(world_vel[0], world_vel[1])
        along, across, _palm = body_basis_vectors(body_quat)
        vel_along = world_vel[0] * along[0] + world_vel[1] * along[1] + world_vel[2] * along[2]
        vel_across = world_vel[0] * across[0] + world_vel[1] * across[1] + world_vel[2] * across[2]

        if self._prev_pitch is None:
            self._prev_pitch = pitch
        pr_inst = wrap180(pitch - self._prev_pitch) / max(dt, 1e-4)
        self._prev_pitch = pitch
        if abs(pr_inst) > c.pitch_rate_spike_deg_s:
            pr_inst = 0.0
        palm_roll_rate = self._pitch_rate_lp.update(pr_inst, dt)

        if self._prev_roll is None:
            self._prev_roll = roll
        tr_inst = wrap180(roll - self._prev_roll) / max(dt, 1e-4)
        self._prev_roll = roll
        if abs(tr_inst) > c.pitch_rate_spike_deg_s:
            tr_inst = 0.0
        wrist_tilt_rate = self._wrist_tilt_rate_lp.update(tr_inst, dt)

        forward_up = rotate_vector_to_world(body_quat, (1.0, 0.0, 0.0), mount_correct=False)[2]

        norm = []
        for i, raw in enumerate(frame.flex):
            v = self._normalize_flex(float(raw), i, dt)
            if not c.flex_bend_is_high:
                v = 1.0 - v
            norm.append(v)
        finger_bend = self._finger_flex(norm)
        finger_osc = tuple(
            abs(self._osc_hp[i].update(finger_bend[i], dt)) for i in range(len(finger_bend))
        )
        is_open, openness_raw = self._openness_from_fingers(finger_bend)
        openness = self._openness.update(openness_raw, dt)
        wiggle = self._twinkle_energy(finger_bend, gyro_mag, dt)
        spawn_flutter = self._spawn_flutter_energy(finger_bend, gyro_mag, dt, is_open=is_open)

        return GloveSignals(
            side=self.side, present=True,
            wrist_tilt=wrist_tilt, palm_roll=palm_roll,
            wrist_tilt_rate=wrist_tilt_rate, palm_roll_rate=palm_roll_rate,
            forward_up=forward_up, openness=openness,
            hand_gyro=hand_gyro, hand_accel=hand_accel,
            vert_vel=vert_vel, vel_along=vel_along, vel_across=vel_across,
            vel_horiz_mag=vel_horiz_mag, lin_accel_mag=lin_accel_mag,
            finger_bend=finger_bend, finger_osc=finger_osc,
            raw_flex=tuple(frame.flex), h_heading=h_heading, is_open=is_open,
            yaw=yaw, pitch=palm_roll, roll=wrist_tilt, pitch_rate=palm_roll_rate,
            gyro_mag=gyro_mag, world_accel=world_accel,
            wiggle=wiggle, spawn_flutter=spawn_flutter,
            game_status=frame.game_status, rv_accuracy=frame.rv_accuracy,
        )

    def _finger_flex(self, norm):
        out = []
        for name in FINGER_NAMES:
            chs = self._channels[name]
            out.append(sum(norm[i] for i in chs) / len(chs))
        return tuple(out)

    def _openness_from_fingers(self, finger_bend):
        open_max = self.cfg.finger_open_max
        non_thumb = finger_bend[1:]
        is_open = all(f <= open_max for f in non_thumb)
        if not non_thumb:
            return True, 1.0
        max_bend = max(non_thumb)
        if max_bend <= open_max:
            return True, 1.0
        span = max(1e-6, 1.0 - open_max)
        return False, max(0.0, 1.0 - (max_bend - open_max) / span)

    def _twinkle_energy(self, finger_bend, gyro_mag, dt):
        c = self.cfg
        if gyro_mag > c.twinkle_still_gyro_max:
            raw = 0.0
        else:
            osc = [abs(self._finger_hp[i].update(flex, dt)) for i, flex in enumerate(finger_bend)]
            active = [o for o in osc if o > c.twinkle_finger_osc_thresh]
            if len(active) < c.twinkle_min_active_fingers:
                raw = 0.0
            else:
                raw = (sum(active) / len(active)) * (len(active) / len(finger_bend))
                raw *= c.twinkle_randomness_gain
        return min(1.0, self._wiggle_env.update(raw, dt))

    def _spawn_flutter_energy(self, finger_bend, gyro_mag, dt, *, is_open):
        c = self.cfg
        if gyro_mag > c.spawn_still_gyro_max:
            raw = 0.0
        else:
            osc = [abs(self._spawn_hp[i].update(finger_bend[i], dt))
                   for i in range(1, len(finger_bend))]
            active = [o for o in osc if o > c.twinkle_finger_osc_thresh]
            raw = min(1.0, sum(osc) * c.spawn_flutter_gain)
            if not is_open and len(active) < 2:
                raw *= 0.25
        return min(1.0, self._spawn_env.update(raw, dt))


# ===========================================================================
# Calibration  (copied: ReferenceFrame + two-hand point-forward gesture)
# ===========================================================================
class ReferenceFrame:
    def __init__(self, cfg: SelCfg):
        self.cfg = cfg
        self.forward: dict[str, float | None] = {"L": None, "R": None}

    def set_forward(self, side: str, heading_deg: float) -> None:
        self.forward[side] = wrap180(heading_deg + self.cfg.forward_offset_deg)

    def is_calibrated(self, side: str) -> bool:
        return self.forward.get(side) is not None

    def heading(self, side: str, heading_deg: float) -> float | None:
        fwd = self.forward.get(side)
        if fwd is None:
            return None
        return wrap180(heading_deg - fwd)

    def region_of(self, side: str, heading_deg: float) -> int | None:
        h = self.heading(side, heading_deg)
        if h is None:
            return None
        half = self.cfg.plane_half_deg
        if abs(h) > half:
            return None
        n = self.cfg.n_regions
        width = (2.0 * half) / n
        raw = max(0, min(n - 1, int((h + half) // width)))
        return raw


class CombinedCalibration:
    """Two-hand point-forward calibration: both hands still+level+open, then
    flutter each a few times. Returns (forward_L, forward_R) on the firing tick."""

    WAIT, FLUTTER = 0, 1

    def __init__(self, cfg: SelCfg):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self._state = self.WAIT
        self._wait_t = 0.0
        self._flutter_t = 0.0
        self._picks = {"L": 0, "R": 0}
        self._closed = {"L": False, "R": False}
        self._cand: dict[str, float | None] = {"L": None, "R": None}

    @property
    def active(self) -> bool:
        return self._state != self.WAIT

    @property
    def progress(self) -> float:
        c = self.cfg
        if self._state == self.WAIT:
            return 0.5 * min(1.0, self._wait_t / max(c.calib_pre_still_s, 1e-6))
        done = min(self._picks["L"], self._picks["R"])
        return 0.5 + 0.5 * min(1.0, done / max(c.calib_knock_cycles, 1))

    def hand_ready(self, sig: GloveSignals) -> bool:
        c = self.cfg
        if not sig.present or math.isnan(sig.h_heading):
            return False
        if not sig.is_open:
            return False
        if abs(sig.wrist_tilt) > c.calib_level_tilt_tol_deg:
            return False
        if sig.gyro_mag > c.calib_steady_gyro_max:
            return False
        if magnitude(sig.world_accel) > c.calib_max_world_accel:
            return False
        if sig.game_status < c.calib_min_game_status:
            return False
        return True

    def _pick_edge(self, side: str, sig: GloveSignals) -> bool:
        c = self.cfg
        o = sig.openness
        if self._closed[side]:
            if o > c.calib_open_above:
                self._closed[side] = False
            return False
        if o < c.calib_pick_below:
            self._closed[side] = True
            return True
        return False

    def update(self, left: GloveSignals, right: GloveSignals, dt: float):
        c = self.cfg
        sigs = {"L": left, "R": right}
        if self._state == self.WAIT:
            if self.hand_ready(left) and self.hand_ready(right):
                self._wait_t += dt
                if self._wait_t >= c.calib_pre_still_s:
                    self._cand["L"] = left.h_heading
                    self._cand["R"] = right.h_heading
                    self._state = self.FLUTTER
                    self._flutter_t = 0.0
                    self._picks = {"L": 0, "R": 0}
                    self._closed = {"L": False, "R": False}
            else:
                self._wait_t = max(0.0, self._wait_t - 2.0 * dt)
            return None

        if not (left.present and right.present
                and not math.isnan(left.h_heading) and not math.isnan(right.h_heading)):
            self.reset()
            return None
        self._flutter_t += dt
        if self._flutter_t > c.calib_knock_total_s:
            self.reset()
            return None
        for side in ("L", "R"):
            if self._picks[side] < c.calib_knock_cycles and self._pick_edge(side, sigs[side]):
                self._picks[side] += 1
        if min(self._picks["L"], self._picks["R"]) >= c.calib_knock_cycles:
            out = (self._cand["L"], self._cand["R"])
            self.reset()
            return out
        return None


# ===========================================================================
# Vocabulary + features
# ===========================================================================
VOCAB: tuple[str, ...] = (
    "none",
    "spawn_note",
    "scale_up",
    "scale_down",
    "semitone_up",
    "semitone_down",
    "collective_scale_up",
    "collective_scale_down",
    "collective_semitone_up",
    "collective_semitone_down",
)
VOCAB_VERSION = "vocab_v3_no_volume"
FEATURE_VERSION = "features_v3_handframe"
from ml.augment import AugmentConfig
from ml.gesture_tcn import (
    MODEL_ARCH,
    GestureTCNConfig,
    load_model as load_gesture_model,
    predict_proba_np,
    save_model as save_gesture_model,
    train_gesture_model,
)
INFER_REFRACTORY_S = 0.4

# Each hand has its own independent model (27-dim hand-frame features).
FEATURE_NAMES: tuple[str, ...] = (
    "vert_vel", "vel_along", "vel_across", "vel_horiz_mag", "lin_accel_mag",
    "wrist_tilt", "palm_roll", "forward_up", "openness",
    "wrist_tilt_rate", "palm_roll_rate",
    "gyro_along", "gyro_across", "gyro_palm",
    "accel_along", "accel_across", "accel_palm",
    "bend_thumb", "bend_index", "bend_middle", "bend_ring", "bend_pinky",
    "osc_thumb", "osc_index", "osc_middle", "osc_ring", "osc_pinky",
)
N_FEATURES = len(FEATURE_NAMES)
_SIG_LEN = N_FEATURES

# Gesture groupings used to drive the recording UI. Both per-hand models share
# the full VOCAB; collective gestures are demonstrated with both hands, the
# single-hand gestures can be recorded on either/both hands.
COLLECTIVE_GESTURES: tuple[str, ...] = tuple(g for g in VOCAB if g.startswith("collective_"))
SINGLE_GESTURES: tuple[str, ...] = tuple(
    g for g in VOCAB if g != "none" and not g.startswith("collective_"))


def _sig_features(sig: GloveSignals | None) -> list[float]:
    if sig is None or not sig.present:
        return [0.0] * _SIG_LEN
    fb = sig.finger_bend if sig.finger_bend else (0.0,) * 5
    fo = sig.finger_osc if sig.finger_osc else (0.0,) * 5
    hg = sig.hand_gyro
    ha = sig.hand_accel
    return [
        float(sig.vert_vel), float(sig.vel_along), float(sig.vel_across),
        float(sig.vel_horiz_mag), float(sig.lin_accel_mag),
        float(sig.wrist_tilt), float(sig.palm_roll), float(sig.forward_up), float(sig.openness),
        float(sig.wrist_tilt_rate), float(sig.palm_roll_rate),
        float(hg[0]), float(hg[1]), float(hg[2]),
        float(ha[0]), float(ha[1]), float(ha[2]),
        float(fb[0]), float(fb[1]), float(fb[2]), float(fb[3]), float(fb[4]),
        float(fo[0]), float(fo[1]), float(fo[2]), float(fo[3]), float(fo[4]),
    ]


# ===========================================================================
# Metrics (threshold tuning + reports)
# ===========================================================================
def _precision(y_true, y_pred, cls) -> float:
    tp = int(((y_true == cls) & (y_pred == cls)).sum())
    fp = int(((y_true != cls) & (y_pred == cls)).sum())
    return tp / (tp + fp) if (tp + fp) else 0.0


def _recall(y_true, y_pred, cls) -> float:
    tp = int(((y_true == cls) & (y_pred == cls)).sum())
    fn = int(((y_true == cls) & (y_pred != cls)).sum())
    return tp / (tp + fn) if (tp + fn) else 0.0


def _f1(p: float, r: float) -> float:
    return 2.0 * p * r / (p + r) if (p + r) > 0 else 0.0


def choose_thresholds(probs, y_true, class_names) -> dict[str, float]:
    thresholds = {name: 0.65 for name in class_names}
    for c, name in enumerate(class_names):
        if name == "none":
            thresholds[name] = 1.0
            continue
        best_t, best_score = 0.75, -1.0
        for t in np.linspace(0.5, 0.95, 10):
            pred = np.argmax(probs, axis=1)
            conf = probs[:, c]
            pred[(pred == c) & (conf < t)] = 0
            p = _precision(y_true, pred, c)
            r = _recall(y_true, pred, c)
            score = _f1(p, r)
            if score > best_score:
                best_score, best_t = score, float(t)
        thresholds[name] = best_t
    return thresholds


def evaluate(probs, y_true, class_names, thresholds) -> dict[str, Any]:
    pred = np.argmax(probs, axis=1)
    conf = probs.max(axis=1)
    for i, cls in enumerate(pred):
        name = class_names[int(cls)]
        if name != "none" and conf[i] < thresholds.get(name, 0.65):
            pred[i] = 0
    acc = float((pred == y_true).mean()) if len(y_true) else 0.0
    neg = (y_true == 0)
    fp = ((y_true == 0) & (pred != 0)).sum()
    fp_rate = float(fp / max(int(neg.sum()), 1))
    per_prec = {name: _precision(y_true, pred, i) for i, name in enumerate(class_names)}
    per_rec = {name: _recall(y_true, pred, i) for i, name in enumerate(class_names)}
    per_f1 = {
        name: _f1(per_prec[name], per_rec[name]) for name in class_names
    }
    return {
        "accuracy": acc,
        "false_positive_rate": fp_rate,
        "per_class_precision": per_prec,
        "per_class_recall": per_rec,
        "per_class_f1": per_f1,
    }


# ===========================================================================
# Clip storage + dataset windowing
# ===========================================================================
# Recording / labelling modes.
MODE_LEFT = "left_only"          # focus the left hand; right is forced background
MODE_RIGHT = "right_only"        # focus the right hand; left is forced background
MODE_BOTH_SYNC = "both_sync"     # both hands perform the SAME gesture in sync
MODE_BOTH_COLLECTIVE = "both_collective"  # both hands perform a collective gesture
MODE_LABELS = {
    MODE_LEFT: "Left only",
    MODE_RIGHT: "Right only",
    MODE_BOTH_SYNC: "Both sync",
    MODE_BOTH_COLLECTIVE: "Both collective",
}
CLIP_SCHEMA_VERSION = 3


@dataclass
class Clip:
    path: Path
    mode: str
    gesture_class: str               # the class this take was recorded for
    X_L: np.ndarray                  # (T, N_FEATURES)
    X_R: np.ndarray                  # (T, N_FEATURES)
    label_L: str
    start_L: int
    end_L: int
    labeled_L: bool
    label_R: str
    start_R: int
    end_R: int
    labeled_R: bool
    forward_L: float = float("nan")
    forward_R: float = float("nan")
    created_at: str = ""

    @property
    def n_frames(self) -> int:
        return int(self.X_L.shape[0])

    @property
    def synced(self) -> bool:
        """Both-hand modes share one span + class across the two hands."""
        return self.mode in (MODE_BOTH_SYNC, MODE_BOTH_COLLECTIVE)

    def x(self, side: str) -> np.ndarray:
        return self.X_L if side == "L" else self.X_R

    def label(self, side: str) -> str:
        return self.label_L if side == "L" else self.label_R

    def span(self, side: str) -> tuple[int, int]:
        return (self.start_L, self.end_L) if side == "L" else (self.start_R, self.end_R)

    def is_labeled(self, side: str) -> bool:
        return self.labeled_L if side == "L" else self.labeled_R

    def editable(self, side: str) -> bool:
        """Which side(s) the operator marks. Synced clips: both. Else: focus hand."""
        if self.synced:
            return True
        return side == ("L" if self.mode == MODE_LEFT else "R")


def _initial_hand(mode: str, gesture_class: str, side: str, t: int):
    """(label, start, end, labeled) for one hand at record time."""
    focus = "L" if mode == MODE_LEFT else "R"
    if mode in (MODE_BOTH_SYNC, MODE_BOTH_COLLECTIVE) or side == focus:
        # span starts as the whole clip; Label tab narrows it. none == labelled.
        return gesture_class, 0, t, (gesture_class == "none")
    # off-hand of a single-hand take: forced background, already final.
    return "none", 0, t, True


def save_clip(gesture_class: str, mode: str,
              rows_L: list[list[float]], rows_R: list[list[float]],
              forward: dict[str, float | None]) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = CLIPS_DIR / f"{gesture_class}_{mode}_{stamp}.npz"
    xl = np.asarray(rows_L, dtype=np.float32) if rows_L else np.zeros((0, N_FEATURES), np.float32)
    xr = np.asarray(rows_R, dtype=np.float32) if rows_R else np.zeros((0, N_FEATURES), np.float32)
    t = int(xl.shape[0])
    lL, sL, eL, dL = _initial_hand(mode, gesture_class, "L", t)
    lR, sR, eR, dR = _initial_hand(mode, gesture_class, "R", t)
    np.savez_compressed(
        path,
        schema_version=np.array(CLIP_SCHEMA_VERSION, dtype=np.int64),
        feature_version=np.array(FEATURE_VERSION),
        mode=np.array(mode),
        gesture_class=np.array(gesture_class),
        feature_names=np.array(FEATURE_NAMES),
        X_L=xl, X_R=xr,
        label_L=np.array(lL), start_L=np.array(sL, np.int64),
        end_L=np.array(eL, np.int64), labeled_L=np.array(dL),
        label_R=np.array(lR), start_R=np.array(sR, np.int64),
        end_R=np.array(eR, np.int64), labeled_R=np.array(dR),
        forward_L=np.array(np.nan if forward.get("L") is None else forward["L"], dtype=np.float64),
        forward_R=np.array(np.nan if forward.get("R") is None else forward["R"], dtype=np.float64),
        created_at=np.array(datetime.now(timezone.utc).isoformat()),
    )
    return path


def _clamp_span(s: int, e: int, t: int) -> tuple[int, int]:
    s = max(0, min(int(s), t))
    e = max(s, min(int(e), t))
    return s, e


def load_clip(path: Path) -> Clip:
    data = np.load(path, allow_pickle=True)
    if "X_L" in data:
        xl = data["X_L"].astype(np.float32)
        xr = data["X_R"].astype(np.float32)
        t = int(xl.shape[0])
        sL, eL = _clamp_span(int(data["start_L"]), int(data["end_L"]), t)
        sR, eR = _clamp_span(int(data["start_R"]), int(data["end_R"]), t)
        return Clip(
            path=path, mode=str(data["mode"]), gesture_class=str(data["gesture_class"]),
            X_L=xl, X_R=xr,
            label_L=str(data["label_L"]), start_L=sL, end_L=eL, labeled_L=bool(data["labeled_L"]),
            label_R=str(data["label_R"]), start_R=sR, end_R=eR, labeled_R=bool(data["labeled_R"]),
            forward_L=float(data["forward_L"]) if "forward_L" in data else float("nan"),
            forward_R=float(data["forward_R"]) if "forward_R" in data else float("nan"),
            created_at=str(data["created_at"]) if "created_at" in data else "",
        )
    # Legacy v1 clip: single concatenated [L14, R14, 4 flags] feature row.
    x = data["X"].astype(np.float32)
    t = int(x.shape[0])
    label = str(data["label"])
    s = int(data["gesture_start"]) if "gesture_start" in data else 0
    e = int(data["gesture_end"]) if "gesture_end" in data else t
    s, e = _clamp_span(s, e, t)
    labeled = bool(data["labeled"]) if "labeled" in data else (label == "none")
    xl = x[:, :N_FEATURES] if x.shape[1] >= N_FEATURES else np.zeros((t, N_FEATURES), np.float32)
    xr = (x[:, N_FEATURES:2 * N_FEATURES] if x.shape[1] >= 2 * N_FEATURES
          else np.zeros((t, N_FEATURES), np.float32))
    return Clip(
        path=path, mode=MODE_BOTH_SYNC, gesture_class=label,
        X_L=xl, X_R=xr,
        label_L=label, start_L=s, end_L=e, labeled_L=labeled,
        label_R=label, start_R=s, end_R=e, labeled_R=labeled,
        forward_L=float(data["forward_L"]) if "forward_L" in data else float("nan"),
        forward_R=float(data["forward_R"]) if "forward_R" in data else float("nan"),
        created_at=str(data["created_at"]) if "created_at" in data else "",
    )


def save_clip_labels(clip: Clip) -> None:
    """Persist the clip's current per-hand labels + spans back to its npz."""
    t = clip.n_frames
    sL, eL = _clamp_span(clip.start_L, clip.end_L, t)
    sR, eR = _clamp_span(clip.start_R, clip.end_R, t)
    np.savez_compressed(
        clip.path,
        schema_version=np.array(CLIP_SCHEMA_VERSION, dtype=np.int64),
        feature_version=np.array(FEATURE_VERSION),
        mode=np.array(clip.mode),
        gesture_class=np.array(clip.gesture_class),
        feature_names=np.array(FEATURE_NAMES),
        X_L=clip.X_L, X_R=clip.X_R,
        label_L=np.array(clip.label_L), start_L=np.array(sL, np.int64),
        end_L=np.array(eL, np.int64), labeled_L=np.array(clip.labeled_L),
        label_R=np.array(clip.label_R), start_R=np.array(sR, np.int64),
        end_R=np.array(eR, np.int64), labeled_R=np.array(clip.labeled_R),
        forward_L=np.array(clip.forward_L, dtype=np.float64),
        forward_R=np.array(clip.forward_R, dtype=np.float64),
        created_at=np.array(clip.created_at),
    )


def list_clips() -> list[Path]:
    return sorted(CLIPS_DIR.glob("*.npz"))


def _window_bounds(t: int, window: int, stride: int) -> list[tuple[int, int]]:
    """Fixed-size [start, end) window bounds over a clip of length t."""
    if t == 0:
        return []
    if t < window:
        return [(0, t)]  # short clip -> one (padded) window
    bounds = []
    start = 0
    while start + window <= t:
        bounds.append((start, start + window))
        start += stride
    if bounds and bounds[-1][1] < t:  # capture the tail
        bounds.append((t - window, t))
    return bounds


def _slice_padded(x: np.ndarray, s: int, e: int, window: int) -> np.ndarray:
    seg = x[s:e]
    if seg.shape[0] < window:
        pad = np.zeros((window - seg.shape[0], x.shape[1]), dtype=np.float32)
        seg = np.concatenate([pad, seg], axis=0)
    return seg


def _clip_features_ok(path: Path) -> bool:
    """True if clip was recorded with the current feature vector."""
    try:
        data = np.load(path, allow_pickle=True)
    except Exception:
        return False
    if "feature_names" in data:
        names = [str(n) for n in data["feature_names"].tolist()]
        return len(names) == N_FEATURES and tuple(names) == FEATURE_NAMES
    if "X_L" in data:
        return int(data["X_L"].shape[1]) == N_FEATURES
    return False


def resolve_active_vocab(selected: set[str] | frozenset[str]) -> tuple[str, ...]:
    """Build training vocabulary: none first, then selected gestures in VOCAB order."""
    names = ["none"]
    for c in VOCAB:
        if c != "none" and c in selected:
            names.append(c)
    if len(names) < 2:
        raise ValueError("Select at least one gesture class (none is always included).")
    return tuple(names)


def build_windowed_dataset(
    side: str,
    window: int,
    stride: int,
    *,
    active_vocab: tuple[str, ...] | None = None,
    excluded_as_none: bool = True,
    overlap_thresh: float = 0.6,
    max_none_fraction: float = 0.5,
    seed: int = 7,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Window every clip for ONE hand using that hand's gesture span.

    Windows mostly inside the marked span get that hand's class; windows entirely
    outside become background ("none"); windows straddling the boundary are
    dropped so labels stay clean. "none"-labelled hands contribute background.

    Returns (X[N,window,F], y[N], groups[N], skipped_clips).
    """
    xs, ys, groups = [], [], []
    skipped = 0
    active_vocab = active_vocab or VOCAB
    active_set = set(active_vocab)
    if "none" not in active_set:
        raise ValueError("active_vocab must include 'none'")
    none_idx = active_vocab.index("none")
    for gi, path in enumerate(list_clips()):
        if not _clip_features_ok(path):
            skipped += 1
            continue
        clip = load_clip(path)
        label = clip.label(side)
        x = clip.x(side)
        if label not in VOCAB or x.ndim != 2 or x.shape[1] != N_FEATURES:
            skipped += 1
            continue
        # Clips whose recorded class is NOT in the active vocabulary should
        # be dropped entirely rather than re-labelled as "none". Otherwise the
        # 'none' prototype absorbs whatever movement was being recorded for
        # that excluded gesture (e.g. collective_volume_* clips, which pull the
        # 'none' prototype toward palm-down rotating motion and then make
        # scale_*/semitone_* much harder to separate). See
        # docs/diagnosis_findings.md.
        if (
            clip.gesture_class != "none"
            and clip.gesture_class not in active_set
        ):
            skipped += 1
            continue
        t = int(x.shape[0])
        g_start, g_end = clip.span(side)
        for s, e in _window_bounds(t, window, stride):
            seg = _slice_padded(x, s, e, window)
            if label == "none":
                label_idx = none_idx
            else:
                wlen = max(1, e - s)
                ov = max(0, min(e, g_end) - max(s, g_start))
                frac = ov / wlen
                if frac >= overlap_thresh:
                    win_label = label
                elif ov == 0:
                    win_label = "none"
                else:
                    continue  # ambiguous boundary window
                if win_label != "none" and win_label not in active_set:
                    if excluded_as_none:
                        win_label = "none"
                    else:
                        continue
                label_idx = active_vocab.index(win_label)
            xs.append(seg)
            ys.append(label_idx)
            groups.append(gi)
    if not xs:
        msg = f"No usable clips for hand {side}. Record + label takes with {FEATURE_VERSION}."
        if skipped:
            msg += f" ({skipped} clip(s) skipped — wrong feature schema; re-record.)"
        raise ValueError(msg)
    y_arr = np.asarray(ys, dtype=np.int64)
    none_mask = y_arr == none_idx
    n_none = int(none_mask.sum())
    n_gesture = int((~none_mask).sum())
    if n_gesture > 0 and max_none_fraction < 1.0 and n_none > 0:
        cap = int(n_gesture * max_none_fraction / max(1e-6, 1.0 - max_none_fraction))
        if n_none > cap:
            rng = np.random.default_rng(seed)
            none_i = np.where(none_mask)[0]
            keep_none = rng.choice(none_i, cap, replace=False)
            keep = np.sort(np.concatenate([np.where(~none_mask)[0], keep_none]))
            xs = [xs[int(i)] for i in keep]
            ys = [ys[int(i)] for i in keep]
            groups = [groups[int(i)] for i in keep]
    return (
        np.asarray(xs, dtype=np.float32),
        np.asarray(ys, dtype=np.int64),
        np.asarray(groups, dtype=np.int64),
        skipped,
    )


def split_by_group(groups: np.ndarray, y: np.ndarray, val_frac=0.2, seed=7):
    """Split window indices by clip group so windows from one take never leak."""
    rng = np.random.default_rng(seed)
    uniq = np.unique(groups)
    rng.shuffle(uniq)
    n_val = max(1, int(round(len(uniq) * val_frac))) if len(uniq) > 1 else 0
    val_groups = set(uniq[:n_val].tolist())
    train_idx = np.array([i for i in range(len(groups)) if groups[i] not in val_groups], np.int64)
    val_idx = np.array([i for i in range(len(groups)) if groups[i] in val_groups], np.int64)
    if len(train_idx) == 0:
        train_idx = np.arange(len(groups))
    return train_idx, val_idx


# ===========================================================================
# Runtime inferencer (live test)
# ===========================================================================
class RuntimeInferencer:
    """One hand's classifier with its own normalization + rolling window."""

    def __init__(self, manifest: dict, side: str, base_dir: Path):
        self.side = side
        self.class_names = list(manifest["class_names"])
        self.window_size = int(manifest["window_size"])
        fn = manifest.get("feature_names") or []
        if len(fn) != N_FEATURES:
            raise ValueError(
                f"Model expects {len(fn)} features; app has {N_FEATURES} ({FEATURE_VERSION}).")
        sd = manifest["sides"][side]
        self.thresholds = {str(k): float(v) for k, v in sd.get("class_thresholds", {}).items()}
        weights = Path(sd["model_weights_path"])
        if not weights.is_absolute():
            weights = base_dir / weights.name
        arch = manifest.get("model_arch", "")
        if weights.suffix == ".pt" or arch == MODEL_ARCH:
            self.model = load_gesture_model(weights)
            self._legacy_numpy = False
        else:
            from ml.tiny_tcn_legacy import LegacyTinyTCN

            self.model = LegacyTinyTCN.load(weights)
            self._legacy_numpy = True
        mean = sd.get("feature_mean")
        std = sd.get("feature_std")
        self.mean = np.asarray(mean, dtype=np.float32) if mean else np.zeros(N_FEATURES, np.float32)
        self.std = np.asarray(std, dtype=np.float32) if std else np.ones(N_FEATURES, np.float32)
        self._frames: deque[list[float]] = deque(maxlen=self.window_size)
        self._refractory_until = 0.0
        self._last_fire_label = "none"

    def reset(self) -> None:
        self._frames.clear()
        self._refractory_until = 0.0
        self._last_fire_label = "none"

    def update(self, row: list[float]) -> tuple[str, float, np.ndarray | None]:
        self._frames.append(row)
        if len(self._frames) < self.window_size:
            return "none", 0.0, None
        x = ((np.asarray(self._frames, dtype=np.float32) - self.mean) / self.std)[None, :, :]
        if self._legacy_numpy:
            probs = self.model.predict_proba(x)[0]
        else:
            probs = predict_proba_np(self.model, x)[0]
        idx = int(np.argmax(probs))
        conf = float(probs[idx])
        label = self.class_names[idx]
        if label != "none" and conf < self.thresholds.get(label, 0.65):
            label = "none"
        now = time.monotonic()
        if label != "none":
            if now < self._refractory_until and label == self._last_fire_label:
                return "none", conf, probs
            self._refractory_until = now + INFER_REFRACTORY_S
            self._last_fire_label = label
        return label, conf, probs


class DualInferencer:
    """Loads both per-hand models from one combined manifest."""

    def __init__(self, manifest_path: Path):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base = manifest_path.parent
        self.class_names = list(manifest["class_names"])
        self.window_size = int(manifest["window_size"])
        self.hands = {s: RuntimeInferencer(manifest, s, base) for s in ("L", "R")}

    def reset(self) -> None:
        for h in self.hands.values():
            h.reset()

    def update(self, side: str, row: list[float]) -> tuple[str, float, np.ndarray | None]:
        return self.hands[side].update(row)


# ===========================================================================
# Training worker thread
# ===========================================================================
@dataclass
class TrainJobConfig:
    window: int
    stride: int
    epochs: int
    channels: int
    active_vocab: tuple[str, ...]
    excluded_as_none: bool
    augment: AugmentConfig
    flex_range: dict[str, dict[str, list[float]]]


class TrainWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str)
    finished_ok = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, job: TrainJobConfig):
        super().__init__()
        self.job = job

    def run(self) -> None:
        try:
            job = self.job
            active = job.active_vocab
            n_classes = len(active)
            model_id = f"gesture_tcn_{time.strftime('%Y%m%d_%H%M%S')}"
            sides_manifest: dict[str, Any] = {}
            for side in ("L", "R"):
                self.progress.emit(f"[{side}] windowing clips…")
                x, y, groups, skipped = build_windowed_dataset(
                    side,
                    job.window,
                    job.stride,
                    active_vocab=active,
                    excluded_as_none=job.excluded_as_none,
                )
                train_idx, val_idx = split_by_group(groups, y)
                skip_msg = f" · {skipped} clip(s) skipped (old schema)" if skipped else ""
                cnt = Counter(active[i] for i in y)
                top = ", ".join(f"{k}:{v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])[:6])
                self.progress.emit(
                    f"[{side}] {x.shape[0]} windows · {len(np.unique(groups))} clips · "
                    f"train {len(train_idx)} / val {len(val_idx)}{skip_msg}"
                )
                self.progress.emit(f"[{side}] labels: {top}")
                flat = x[train_idx].reshape(-1, N_FEATURES)
                mean = flat.mean(axis=0)
                std = flat.std(axis=0) + 1e-6
                x = ((x - mean) / std).astype(np.float32)

                cfg = GestureTCNConfig(epochs=job.epochs, channels=job.channels)

                def on_epoch(e, total, _s=side):
                    self.progress.emit(f"[{_s}] PyTorch training… epoch {e}/{total}")

                fit = train_gesture_model(
                    N_FEATURES,
                    n_classes,
                    cfg,
                    x[train_idx],
                    y[train_idx],
                    x_val=x[val_idx] if len(val_idx) else None,
                    y_val=y[val_idx] if len(val_idx) else None,
                    progress=on_epoch,
                    augment=job.augment,
                    model_arch=MODEL_ARCH,
                )
                model = fit["model"]
                arch = fit["model_arch"]
                self.progress.emit(f"[{side}] choosing thresholds + evaluating…")
                eval_idx = val_idx if len(val_idx) else train_idx
                probs = predict_proba_np(model, x[eval_idx])
                class_names = list(active)
                thresholds = choose_thresholds(probs, y[eval_idx], class_names)
                report = evaluate(probs, y[eval_idx], class_names, thresholds)
                raw_pred = probs.argmax(axis=1)
                raw_acc = float((raw_pred == y[eval_idx]).mean())

                weights_path = MODELS_DIR / f"{model_id}_{side}.pt"
                save_gesture_model(
                    model, weights_path,
                    in_features=N_FEATURES, n_classes=n_classes, model_arch=arch,
                )
                sides_manifest[side] = {
                    "model_weights_path": str(weights_path),
                    "feature_mean": mean.astype(float).tolist(),
                    "feature_std": std.astype(float).tolist(),
                    "class_thresholds": thresholds,
                    "metrics": {
                        "train_acc": fit["train_acc"],
                        "val_acc": fit.get("val_acc"),
                        "device": fit.get("device"),
                        "test_acc": report["accuracy"],
                        "raw_val_acc": raw_acc,
                        "false_positive_rate": report["false_positive_rate"],
                    },
                    "per_class_precision": report["per_class_precision"],
                    "per_class_recall": report["per_class_recall"],
                    "per_class_f1": report["per_class_f1"],
                }

            train_config = {
                "full_vocab": list(VOCAB),
                "active_classes": list(active),
                "excluded_as_none": job.excluded_as_none,
                "augment": job.augment.to_dict(),
            }
            manifest = {
                "model_id": model_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "vocab_version": VOCAB_VERSION,
                "feature_version": FEATURE_VERSION,
                "model_arch": MODEL_ARCH,
                "class_names": list(active),
                "window_size": job.window,
                "stride": job.stride,
                "feature_names": list(FEATURE_NAMES),
                "flex_range": job.flex_range,
                "train_config": train_config,
                "sides": sides_manifest,
            }
            manifest_path = MODELS_DIR / f"{model_id}.manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            config_path = MODELS_DIR / f"{model_id}.train.json"
            config_path.write_text(json.dumps({
                "model_id": model_id,
                "manifest_path": str(manifest_path),
                **train_config,
                "flex_range": job.flex_range,
                "window_size": job.window,
                "stride": job.stride,
                "model_arch": MODEL_ARCH,
            }, indent=2), encoding="utf-8")
            self.finished_ok.emit({"manifest_path": str(manifest_path), "train_config_path": str(config_path), **manifest})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(repr(exc))


# ===========================================================================
# Main window
# ===========================================================================
HINT_STYLE = "color:#8a7964;font-family:monospace;font-size:10pt;"
HDR_STYLE = "color:#aaa;font-weight:bold;"
MONO = "font-family:monospace;"


class GestureTrainerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Glove gesture trainer")
        self.resize(1080, 760)

        self.cond_cfg = CondCfg()
        self.sel_cfg = SelCfg()
        self.links = {
            "L": GloveLink(GLOVE_NAMES[0], "L"),
            "R": GloveLink(GLOVE_NAMES[1], "R"),
        }
        self.cond = {"L": GloveConditioner("L", self.cond_cfg),
                     "R": GloveConditioner("R", self.cond_cfg)}
        self.frame = ReferenceFrame(self.sel_cfg)
        self.calib = CombinedCalibration(self.sel_cfg)
        # Finger-range ("wiggle") calibration: fixed per-channel ADC min/max.
        self.flex_range: dict[str, dict[str, list[float]]] = {}
        self._range_capturing = False
        self._range_min: dict[str, list[float]] = {}
        self._range_max: dict[str, list[float]] = {}
        self.left_sig = self.cond["L"].process(None, DT)
        self.right_sig = self.cond["R"].process(None, DT)
        self.cur_row_L: list[float] = [0.0] * N_FEATURES
        self.cur_row_R: list[float] = [0.0] * N_FEATURES

        # recording state (both hands captured every take)
        self.recording = False
        self._rec_rows_L: list[list[float]] = []
        self._rec_rows_R: list[list[float]] = []
        self.saved_count = 0
        self._rec_busy = False
        self._last_clip_path: Path | None = None
        self.tally: dict[tuple[str, str], int] = {}

        # live test (two independent per-hand models)
        self.infer: DualInferencer | None = None
        self.infer_active = False
        self._live: dict[str, tuple[str, float, np.ndarray | None]] = {
            "L": ("none", 0.0, None), "R": ("none", 0.0, None)}

        self.train_worker: TrainWorker | None = None

        self._build_ui()
        self._auto_load_calibration()

        # 60 Hz signal/recording tick
        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.setInterval(int(DT * 1000))
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()
        # 20 Hz UI refresh
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.setInterval(50)
        self.ui_timer.timeout.connect(self._refresh_ui)
        self.ui_timer.start()

    # ---------------------------------------------------------------- UI ---
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        # shared status strip
        self.status_lbl = QtWidgets.QLabel("Gloves: scanning…")
        self.status_lbl.setStyleSheet("font-size:13pt;font-weight:bold;padding:4px;")
        root.addWidget(self.status_lbl)

        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, stretch=1)
        self.tabs.addTab(self._build_calib_tab(), "1 · Connect & Calibrate")
        self.tabs.addTab(self._build_record_tab(), "2 · Record")
        self.tabs.addTab(self._build_label_tab(), "3 · Label")
        self.tabs.addTab(self._build_train_tab(), "4 · Train")
        self.tabs.addTab(self._build_live_tab(), "5 · Live Test")
        self.tabs.currentChanged.connect(self._on_tab_changed)

    @staticmethod
    def _hdr(txt: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(txt)
        lbl.setStyleSheet(HDR_STYLE)
        return lbl

    # --- Tab 1: calibrate ---
    def _build_calib_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)

        hint = QtWidgets.QLabel(
            "Calibrate forward heading for both gloves:\n"
            "  • Point both arms straight ahead, hands level & open.\n"
            "  • Hold still ~1 s, then flutter (pick→open) each hand 3×.\n"
            "Or set each hand's forward to its current heading manually."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(HINT_STYLE)
        lay.addWidget(hint)

        self.calib_state_lbl = QtWidgets.QLabel("Calibration: not calibrated")
        self.calib_state_lbl.setStyleSheet("font-size:12pt;font-weight:bold;color:#c66;")
        lay.addWidget(self.calib_state_lbl)

        self.calib_progress = QtWidgets.QProgressBar()
        self.calib_progress.setRange(0, 100)
        lay.addWidget(self.calib_progress)

        ready_row = QtWidgets.QHBoxLayout()
        self.l_ready_lbl = QtWidgets.QLabel("L pose: —")
        self.r_ready_lbl = QtWidgets.QLabel("R pose: —")
        for l in (self.l_ready_lbl, self.r_ready_lbl):
            l.setStyleSheet(MONO)
            ready_row.addWidget(l)
        lay.addLayout(ready_row)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_set_l = QtWidgets.QPushButton("Set L forward = current")
        self.btn_set_l.clicked.connect(lambda: self._manual_forward("L"))
        self.btn_set_r = QtWidgets.QPushButton("Set R forward = current")
        self.btn_set_r.clicked.connect(lambda: self._manual_forward("R"))
        btn_row.addWidget(self.btn_set_l)
        btn_row.addWidget(self.btn_set_r)
        lay.addLayout(btn_row)

        save_row = QtWidgets.QHBoxLayout()
        self.btn_save_calib = QtWidgets.QPushButton("Save calibration…")
        self.btn_save_calib.clicked.connect(self._save_calibration)
        self.btn_load_calib = QtWidgets.QPushButton("Load calibration…")
        self.btn_load_calib.clicked.connect(self._load_calibration_dialog)
        save_row.addWidget(self.btn_save_calib)
        save_row.addWidget(self.btn_load_calib)
        lay.addLayout(save_row)

        lay.addWidget(self._hdr("Finger range — wiggle calibration"))
        range_hint = QtWidgets.QLabel(
            "Set the bend 0–100% scale once: press capture, then wiggle ALL fingers on\n"
            "both gloves through their full range (fully open ↔ fully closed) until done.\n"
            "Recordings then map raw flex into this fixed range (and clip beyond it)."
        )
        range_hint.setWordWrap(True)
        range_hint.setStyleSheet(HINT_STYLE)
        lay.addWidget(range_hint)

        rrow = QtWidgets.QHBoxLayout()
        rrow.addWidget(QtWidgets.QLabel("Capture (s)"))
        self.spin_range_dur = QtWidgets.QDoubleSpinBox()
        self.spin_range_dur.setRange(3, 20)
        self.spin_range_dur.setValue(6.0)
        rrow.addWidget(self.spin_range_dur)
        self.btn_capture_range = QtWidgets.QPushButton("Capture finger range")
        self.btn_capture_range.clicked.connect(self._on_capture_range_clicked)
        rrow.addWidget(self.btn_capture_range, stretch=1)
        lay.addLayout(rrow)
        self.range_lbl = QtWidgets.QLabel("Finger range: not captured (using adaptive fallback)")
        self.range_lbl.setStyleSheet(MONO + "color:#c66;")
        lay.addWidget(self.range_lbl)

        lay.addWidget(self._hdr("Live signals"))
        self.live_l = QtWidgets.QLabel("L: —")
        self.live_r = QtWidgets.QLabel("R: —")
        for l in (self.live_l, self.live_r):
            l.setStyleSheet(MONO + "color:#888;font-size:9pt;")
            l.setWordWrap(True)
            lay.addWidget(l)
        lay.addStretch()
        return w

    # --- Tab 2: record ---
    def _build_record_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(w)

        # left: tally of what's been captured so far
        left = QtWidgets.QVBoxLayout()
        root.addLayout(left, stretch=1)
        left.addWidget(self._hdr("Captured takes"))
        self.tally_tree = QtWidgets.QTreeWidget()
        self.tally_tree.setHeaderLabels(["gesture · mode", "takes"])
        self.tally_tree.setRootIsDecorated(False)
        left.addWidget(self.tally_tree, stretch=1)

        self.rec_stats = QtWidgets.QLabel("Saved: 0")
        self.rec_stats.setStyleSheet(MONO)
        left.addWidget(self.rec_stats)
        self.btn_undo = QtWidgets.QPushButton("Undo last take")
        self.btn_undo.clicked.connect(self._undo_last_take)
        left.addWidget(self.btn_undo)
        self.btn_open_clips = QtWidgets.QPushButton("Reveal clips folder")
        self.btn_open_clips.clicked.connect(
            lambda: QtWidgets.QMessageBox.information(self, "Clips", str(CLIPS_DIR)))
        left.addWidget(self.btn_open_clips)

        # right: capture controls
        right = QtWidgets.QVBoxLayout()
        root.addLayout(right, stretch=2)

        sel = QtWidgets.QGridLayout()
        sel.addWidget(QtWidgets.QLabel("Mode"), 0, 0)
        self.mode_combo = QtWidgets.QComboBox()
        for m in (MODE_LEFT, MODE_RIGHT, MODE_BOTH_SYNC, MODE_BOTH_COLLECTIVE):
            self.mode_combo.addItem(MODE_LABELS[m], m)
        self.mode_combo.setCurrentIndex(2)  # both sync
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        sel.addWidget(self.mode_combo, 0, 1)
        sel.addWidget(QtWidgets.QLabel("Gesture"), 1, 0)
        self.gesture_combo = QtWidgets.QComboBox()
        self.gesture_combo.currentIndexChanged.connect(self._update_record_hint)
        sel.addWidget(self.gesture_combo, 1, 1)
        right.addLayout(sel)

        self.class_lbl = QtWidgets.QLabel("—")
        self.class_lbl.setStyleSheet("font-size:24pt;font-weight:bold;color:#2a5;")
        self.class_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.class_lbl)

        self.rec_hint = QtWidgets.QLabel("")
        self.rec_hint.setWordWrap(True)
        self.rec_hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.rec_hint.setStyleSheet("font-size:11pt;color:#888;padding:4px;")
        right.addWidget(self.rec_hint)

        self.rec_preview = QtWidgets.QLabel("L bend —   R bend —")
        self.rec_preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.rec_preview.setStyleSheet(MONO + "color:#789;font-size:10pt;")
        right.addWidget(self.rec_preview)

        self.countdown_lbl = QtWidgets.QLabel("")
        self.countdown_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.countdown_lbl.setStyleSheet("font-size:34pt;font-weight:bold;color:#c44;")
        right.addWidget(self.countdown_lbl)

        self.rec_progress = QtWidgets.QProgressBar()
        right.addWidget(self.rec_progress)

        self.btn_record = QtWidgets.QPushButton("Record this take")
        self.btn_record.setMinimumHeight(64)
        self.btn_record.setStyleSheet("font-size:16pt;font-weight:bold;")
        self.btn_record.clicked.connect(self._on_record_clicked)
        right.addWidget(self.btn_record)

        timing = QtWidgets.QGroupBox("Timing")
        form = QtWidgets.QFormLayout(timing)
        self.spin_countdown = QtWidgets.QDoubleSpinBox()
        self.spin_countdown.setRange(0, 5)
        self.spin_countdown.setValue(2.0)
        self.spin_countdown.setSingleStep(0.5)
        self.spin_duration = QtWidgets.QDoubleSpinBox()
        self.spin_duration.setRange(2, 20)
        self.spin_duration.setValue(8.0)
        form.addRow("Countdown (s)", self.spin_countdown)
        form.addRow("Capture (s)", self.spin_duration)
        right.addWidget(timing)

        self.last_saved_lbl = QtWidgets.QLabel("")
        self.last_saved_lbl.setStyleSheet(MONO + "color:#666;")
        self.last_saved_lbl.setWordWrap(True)
        right.addWidget(self.last_saved_lbl)
        right.addStretch()

        self._on_mode_changed()
        return w

    RECORD_HINTS = {
        MODE_LEFT: "Left hand performs the gesture; right hand stays idle (auto-labelled none).",
        MODE_RIGHT: "Right hand performs the gesture; left hand stays idle (auto-labelled none).",
        MODE_BOTH_SYNC: "Both hands perform the SAME gesture in sync (robustness data).",
        MODE_BOTH_COLLECTIVE: "Both hands perform the collective gesture together.",
    }

    def _current_mode(self) -> str:
        return self.mode_combo.currentData()

    def _on_mode_changed(self) -> None:
        mode = self._current_mode()
        gestures = COLLECTIVE_GESTURES if mode == MODE_BOTH_COLLECTIVE else SINGLE_GESTURES
        prev = self.gesture_combo.currentText()
        self.gesture_combo.blockSignals(True)
        self.gesture_combo.clear()
        self.gesture_combo.addItems([*gestures, "none"])
        i = self.gesture_combo.findText(prev)
        self.gesture_combo.setCurrentIndex(i if i >= 0 else 0)
        self.gesture_combo.blockSignals(False)
        self._update_record_hint()

    def _update_record_hint(self) -> None:
        mode = self._current_mode()
        gesture = self.gesture_combo.currentText()
        self.class_lbl.setText(f"{gesture}")
        base = self.RECORD_HINTS.get(mode, "")
        self.rec_hint.setText(
            base + "\nAlso record explicit \"none\" takes (idle motion) for background.")

    # --- Tab 3: label ---
    LABEL_DEFAULT_CHANNELS = (0, 5, 6, 11, 8, 23)  # vvel, wrist_tilt, palm_roll, gyro_along, open, osc_ix

    def _build_label_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(w)

        # left: clip list + channel checklist
        left_w = QtWidgets.QWidget()
        left_w.setFixedWidth(270)
        left = QtWidgets.QVBoxLayout(left_w)
        root.addWidget(left_w, stretch=0)

        left.addWidget(self._hdr("Clips"))
        self.label_list = QtWidgets.QListWidget()
        self.label_list.currentRowChanged.connect(self._on_label_clip_selected)
        left.addWidget(self.label_list, stretch=1)
        btn_reload = QtWidgets.QPushButton("Reload clip list")
        btn_reload.clicked.connect(self._refresh_label_clip_list)
        left.addWidget(btn_reload)

        left.addWidget(self._hdr("Signals to overlay"))
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget()
        vb = QtWidgets.QVBoxLayout(inner)
        vb.setSpacing(1)
        self._label_checks: dict[int, QtWidgets.QCheckBox] = {}
        n = N_FEATURES
        self._label_colors = {i: pg.intColor(i, hues=max(9, n)) for i in range(n)}
        for i, name in enumerate(FEATURE_NAMES):
            cb = QtWidgets.QCheckBox(name)
            cb.setChecked(i in self.LABEL_DEFAULT_CHANNELS)
            cb.setStyleSheet(f"color:{self._label_colors[i].name()};font-size:9pt;")
            cb.stateChanged.connect(lambda _s, idx=i: self._on_label_channel_toggle(idx))
            vb.addWidget(cb)
            self._label_checks[i] = cb
        vb.addStretch(1)
        scroll.setWidget(inner)
        left.addWidget(scroll, stretch=1)

        # right: plot + controls
        right = QtWidgets.QVBoxLayout()
        root.addLayout(right, stretch=1)

        hint = QtWidgets.QLabel(
            "Each hand is labelled independently for its own model. Drag the band to\n"
            "mark when the gesture happens: inside → that hand's class, outside → none.\n"
            "Both-hand clips share one span across L+R; single-hand clips lock the\n"
            "off-hand to none. Switch the viewed hand to inspect/edit each curve set."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(HINT_STYLE)
        right.addWidget(hint)

        ctl_row = QtWidgets.QHBoxLayout()
        ctl_row.addWidget(QtWidgets.QLabel("View hand:"))
        self.label_hand_combo = QtWidgets.QComboBox()
        self.label_hand_combo.addItem("Left", "L")
        self.label_hand_combo.addItem("Right", "R")
        self.label_hand_combo.currentIndexChanged.connect(self._on_label_hand_changed)
        ctl_row.addWidget(self.label_hand_combo)
        ctl_row.addSpacing(16)
        ctl_row.addWidget(QtWidgets.QLabel("Class:"))
        self.label_class_combo = QtWidgets.QComboBox()
        self.label_class_combo.addItems(list(VOCAB))
        ctl_row.addWidget(self.label_class_combo, stretch=1)
        right.addLayout(ctl_row)

        self.label_mode_lbl = QtWidgets.QLabel("")
        self.label_mode_lbl.setStyleSheet(MONO + "color:#a96;")
        right.addWidget(self.label_mode_lbl)

        pg.setConfigOptions(antialias=True)
        self.label_plot = pg.PlotWidget()
        self.label_plot.setBackground("#101014")
        self.label_plot.showGrid(x=True, y=True, alpha=0.25)
        self.label_plot.setLabel("bottom", "frame")
        self.label_plot.setLabel("left", "feature value (mixed units)")
        self.label_plot.addLegend(offset=(10, 10))
        self._region = pg.LinearRegionItem(values=(0, 1), brush=(80, 170, 90, 60),
                                           movable=True)
        self._region.sigRegionChanged.connect(self._on_region_changed)
        self.label_plot.addItem(self._region)
        right.addWidget(self.label_plot, stretch=1)
        self._label_curves: dict[int, pg.PlotDataItem] = {}
        self._cur_clip: Clip | None = None
        self._view_side = "L"

        self.label_span_lbl = QtWidgets.QLabel("No clip selected.")
        self.label_span_lbl.setStyleSheet(MONO)
        right.addWidget(self.label_span_lbl)

        row = QtWidgets.QHBoxLayout()
        self.btn_span_full = QtWidgets.QPushButton("Span = whole clip")
        self.btn_span_full.clicked.connect(self._span_full)
        self.btn_save_label = QtWidgets.QPushButton("Save label")
        self.btn_save_label.setStyleSheet("font-weight:bold;")
        self.btn_save_label.clicked.connect(self._save_label)
        row.addWidget(self.btn_span_full)
        row.addWidget(self.btn_save_label)
        right.addLayout(row)
        return w

    def _refresh_label_clip_list(self) -> None:
        self.label_list.blockSignals(True)
        self.label_list.clear()
        self._label_clip_paths = list_clips()
        for p in self._label_clip_paths:
            clip = load_clip(p)

            def hand_tag(side):
                lab = clip.label(side)
                if lab == "none":
                    return f"{side}:none"
                if clip.is_labeled(side):
                    s, e = clip.span(side)
                    return f"{side}:{lab}[{s}:{e}]"
                return f"{side}:{lab}?"

            needs = (not clip.labeled_L) or (not clip.labeled_R)
            item = QtWidgets.QListWidgetItem(
                f"{clip.gesture_class} · {MODE_LABELS.get(clip.mode, clip.mode)}  "
                f"({clip.n_frames}f)\n  {hand_tag('L')}  {hand_tag('R')}")
            if needs:
                item.setForeground(QtCore.Qt.GlobalColor.red)
            self.label_list.addItem(item)
        self.label_list.blockSignals(False)

    def _on_label_clip_selected(self, row: int) -> None:
        paths = getattr(self, "_label_clip_paths", [])
        if row < 0 or row >= len(paths):
            return
        self._cur_clip = load_clip(paths[row])
        # default to the focus hand
        default = "R" if self._cur_clip.mode == MODE_RIGHT else "L"
        self.label_hand_combo.blockSignals(True)
        self.label_hand_combo.setCurrentIndex(0 if default == "L" else 1)
        self.label_hand_combo.blockSignals(False)
        self._view_side = default
        self._sync_label_controls()

    def _on_label_hand_changed(self) -> None:
        self._view_side = self.label_hand_combo.currentData()
        self._sync_label_controls()

    def _sync_label_controls(self) -> None:
        clip = self._cur_clip
        if clip is None:
            return
        side = self._view_side
        editable = clip.editable(side)
        t = max(1, clip.n_frames)
        s, e = clip.span(side)

        lab = clip.label(side)
        in_vocab = lab in VOCAB
        self.label_class_combo.blockSignals(True)
        if in_vocab:
            self.label_class_combo.setCurrentText(lab)
        self.label_class_combo.blockSignals(False)
        self.label_class_combo.setEnabled(editable and in_vocab)
        if not in_vocab:
            self.label_class_combo.setToolTip(f"Label '{lab}' is not in the training vocabulary (clip skipped).")
        else:
            self.label_class_combo.setToolTip("")
        self.btn_save_label.setEnabled(editable and in_vocab)
        self.btn_span_full.setEnabled(editable)

        self._region.blockSignals(True)
        self._region.setBounds((0, t))
        self._region.setRegion((s, e))
        self._region.setMovable(editable)
        self._region.blockSignals(False)

        if clip.synced:
            self.label_mode_lbl.setText(
                f"{MODE_LABELS[clip.mode]}: span + class apply to BOTH hands.")
        elif editable:
            self.label_mode_lbl.setText(
                f"{MODE_LABELS[clip.mode]}: editing the {side} (focus) hand.")
        else:
            self.label_mode_lbl.setText(
                f"{MODE_LABELS[clip.mode]}: {side} hand is the off-hand → locked to none.")
        self._redraw_label_curves()
        self._on_region_changed()

    def _redraw_label_curves(self) -> None:
        for idx, curve in list(self._label_curves.items()):
            self.label_plot.removeItem(curve)
        self._label_curves.clear()
        if self._cur_clip is None or self._cur_clip.n_frames == 0:
            return
        x = self._cur_clip.x(self._view_side)
        frames = np.arange(x.shape[0])
        for i, cb in self._label_checks.items():
            if not cb.isChecked():
                continue
            pen = pg.mkPen(self._label_colors[i], width=2)
            curve = self.label_plot.plot(frames, x[:, i], pen=pen,
                                         name=FEATURE_NAMES[i], connect="finite")
            self._label_curves[i] = curve

    def _on_label_channel_toggle(self, _idx: int) -> None:
        self._redraw_label_curves()

    def _on_region_changed(self) -> None:
        if self._cur_clip is None:
            return
        lo, hi = self._region.getRegion()
        s, e = int(round(lo)), int(round(hi))
        t = self._cur_clip.n_frames
        s = max(0, min(s, t))
        e = max(s, min(e, t))
        secs = (e - s) * DT
        scope = "both hands" if self._cur_clip.synced else f"{self._view_side} hand"
        self.label_span_lbl.setText(
            f"gesture span ({scope}): frames [{s}:{e}]  ·  {secs:.2f}s  of  {t}f "
            f"({t * DT:.2f}s)   |   outside the band → none")

    def _span_full(self) -> None:
        if self._cur_clip is None:
            return
        self._region.setRegion((0, self._cur_clip.n_frames))

    def _save_label(self) -> None:
        clip = self._cur_clip
        if clip is None:
            QtWidgets.QMessageBox.information(self, "No clip", "Select a clip first.")
            return
        side = self._view_side
        if not clip.editable(side):
            QtWidgets.QMessageBox.information(
                self, "Off-hand locked",
                "This hand is the idle off-hand (auto-labelled none). Switch to the "
                "focus hand to edit its label.")
            return
        lo, hi = self._region.getRegion()
        s, e = int(round(lo)), int(round(hi))
        t = clip.n_frames
        s = max(0, min(s, t))
        e = max(s, min(e, t))
        label = self.label_class_combo.currentText()
        if label != "none" and e - s < 2:
            QtWidgets.QMessageBox.warning(self, "Span too short",
                                          "Mark a wider gesture span (or set class to none).")
            return
        if clip.synced:
            clip.label_L = clip.label_R = label
            clip.start_L = clip.start_R = s
            clip.end_L = clip.end_R = e
            clip.labeled_L = clip.labeled_R = True
        elif side == "L":
            clip.label_L, clip.start_L, clip.end_L, clip.labeled_L = label, s, e, True
        else:
            clip.label_R, clip.start_R, clip.end_R, clip.labeled_R = label, s, e, True
        save_clip_labels(clip)
        row = self.label_list.currentRow()
        self._refresh_label_clip_list()
        if 0 <= row < self.label_list.count():
            self.label_list.setCurrentRow(row)

    # --- Tab 4: train ---
    def _build_train_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)

        hint = QtWidgets.QLabel(
            "Window the labelled takes and train the PyTorch TCN classifier.\n"
            "Windows inside each clip's gesture span get its class; the rest become\n"
            "background (none). None windows are subsampled if they dominate. Old clips\n"
            "(pre-v3 features) are skipped — re-record. Clips whose hand label is not in the\n"
            "vocabulary (e.g. collective_volume_*) are skipped; files are left unchanged.\n"
            "Window default 64 frames (~1 s): use ~p75 of your label spans, not the max.\n"
            "Quick check without waiting on the UI: python scripts/gesture_train_diagnose.py --side L"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(HINT_STYLE)
        lay.addWidget(hint)

        form = QtWidgets.QFormLayout()
        self.spin_window = QtWidgets.QSpinBox()
        self.spin_window.setRange(8, 240)
        self.spin_window.setValue(64)
        self.spin_stride = QtWidgets.QSpinBox()
        self.spin_stride.setRange(1, 120)
        self.spin_stride.setValue(12)
        self.spin_epochs = QtWidgets.QSpinBox()
        self.spin_epochs.setRange(1, 300)
        self.spin_epochs.setValue(40)
        self.spin_channels = QtWidgets.QSpinBox()
        self.spin_channels.setRange(4, 128)
        self.spin_channels.setValue(64)
        form.addRow("Window (frames)", self.spin_window)
        form.addRow("Stride (frames)", self.spin_stride)
        form.addRow("Epochs", self.spin_epochs)
        form.addRow("Conv channels", self.spin_channels)
        lay.addLayout(form)

        lay.addWidget(self._hdr("Classes to train (none is always included)"))
        self.train_class_checks: dict[str, QtWidgets.QCheckBox] = {}
        class_grid = QtWidgets.QGridLayout()
        gi = 0
        for name in VOCAB:
            if name == "none":
                continue
            cb = QtWidgets.QCheckBox(name)
            cb.setChecked(True)
            class_grid.addWidget(cb, gi // 2, gi % 2)
            self.train_class_checks[name] = cb
            gi += 1
        lay.addLayout(class_grid)

        self.chk_excluded_as_none = QtWidgets.QCheckBox(
            "Treat windows from unchecked classes as none (recommended)")
        self.chk_excluded_as_none.setChecked(True)
        lay.addWidget(self.chk_excluded_as_none)

        lay.addWidget(self._hdr("Training augmentations (live features only; clips unchanged)"))
        self.train_aug_checks: dict[str, QtWidgets.QCheckBox] = {}
        aug_specs = (
            ("gaussian_noise", "Gaussian noise on all features"),
            ("bend_scale", "Scale jitter on finger bend / osc"),
            ("bend_bias", "Bias drift on finger bend / osc"),
            ("time_warp", "Time warp (0.9–1.1×)"),
            ("orientation_noise", "Noise on wrist_tilt / palm_roll (+ rates)"),
            ("channel_dropout", "Random channel dropout"),
        )
        for name, label in aug_specs:
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(name in ("gaussian_noise", "bend_scale", "orientation_noise"))
            lay.addWidget(cb)
            self.train_aug_checks[name] = cb

        self.btn_train = QtWidgets.QPushButton("Build dataset & train")
        self.btn_train.setMinimumHeight(48)
        self.btn_train.setStyleSheet("font-size:14pt;font-weight:bold;")
        self.btn_train.clicked.connect(self._start_training)
        lay.addWidget(self.btn_train)

        self.train_status = QtWidgets.QLabel("Idle.")
        self.train_status.setStyleSheet(MONO)
        lay.addWidget(self.train_status)

        self.train_report = QtWidgets.QPlainTextEdit()
        self.train_report.setReadOnly(True)
        self.train_report.setStyleSheet(MONO + "font-size:9pt;")
        lay.addWidget(self.train_report, stretch=1)
        return w

    # --- Tab 4: live test ---
    def _build_live_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Model"))
        self.model_combo = QtWidgets.QComboBox()
        row.addWidget(self.model_combo, stretch=1)
        self.btn_refresh_models = QtWidgets.QPushButton("↻")
        self.btn_refresh_models.setFixedWidth(36)
        self.btn_refresh_models.clicked.connect(self._refresh_models)
        row.addWidget(self.btn_refresh_models)
        lay.addLayout(row)

        run_row = QtWidgets.QHBoxLayout()
        self.btn_load_model = QtWidgets.QPushButton("Load + start")
        self.btn_load_model.clicked.connect(self._load_model)
        self.btn_stop_infer = QtWidgets.QPushButton("Stop")
        self.btn_stop_infer.clicked.connect(self._stop_infer)
        run_row.addWidget(self.btn_load_model)
        run_row.addWidget(self.btn_stop_infer)
        lay.addLayout(run_row)

        self.pred_lbl: dict[str, QtWidgets.QLabel] = {}
        self.conf_bar: dict[str, QtWidgets.QProgressBar] = {}
        pred_row = QtWidgets.QHBoxLayout()
        for side, name in (("L", "Left hand"), ("R", "Right hand")):
            box = QtWidgets.QVBoxLayout()
            cap = QtWidgets.QLabel(name)
            cap.setStyleSheet(HDR_STYLE)
            cap.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            pl = QtWidgets.QLabel("—")
            pl.setStyleSheet("font-size:26pt;font-weight:bold;color:#39c;")
            pl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            cb = QtWidgets.QProgressBar()
            cb.setRange(0, 100)
            box.addWidget(cap)
            box.addWidget(pl)
            box.addWidget(cb)
            pred_row.addLayout(box)
            self.pred_lbl[side] = pl
            self.conf_bar[side] = cb
        lay.addLayout(pred_row)

        lay.addWidget(self._hdr("Class probabilities (L | R)"))
        self.prob_bars: dict[str, dict[str, QtWidgets.QProgressBar]] = {"L": {}, "R": {}}
        grid = QtWidgets.QGridLayout()
        for i, name in enumerate(VOCAB):
            lbl = QtWidgets.QLabel(name)
            lbl.setStyleSheet(MONO + "font-size:9pt;")
            grid.addWidget(lbl, i, 0)
            for col, side in ((1, "L"), (2, "R")):
                bar = QtWidgets.QProgressBar()
                bar.setRange(0, 100)
                bar.setTextVisible(False)
                grid.addWidget(bar, i, col)
                self.prob_bars[side][name] = bar
        lay.addLayout(grid)
        lay.addStretch()
        self._refresh_models()
        return w

    # ------------------------------------------------------------- logic ---
    def _tick(self) -> None:
        now = time.monotonic()
        fl = self.links["L"].fresh(now, STALE_AFTER_S)
        fr = self.links["R"].fresh(now, STALE_AFTER_S)
        self.left_sig = self.cond["L"].process(fl, DT)
        self.right_sig = self.cond["R"].process(fr, DT)

        # Each hand emits its own independent 14-dim feature row.
        self.cur_row_L = _sig_features(self.left_sig)
        self.cur_row_R = _sig_features(self.right_sig)

        # Two-hand point-forward calibration gesture, always armed except while a
        # recording take is in progress (so a gesture can't accidentally re-zero).
        if not self.recording:
            fired = self.calib.update(self.left_sig, self.right_sig, DT)
            if fired is not None:
                fl_h, fr_h = fired
                if fl_h is not None:
                    self.frame.set_forward("L", fl_h)
                if fr_h is not None:
                    self.frame.set_forward("R", fr_h)

        if self._range_capturing:
            for side, sig in (("L", self.left_sig), ("R", self.right_sig)):
                if sig.present:
                    raw = sig.raw_flex
                    for i in range(min(N_CHANNELS, len(raw))):
                        self._range_min[side][i] = min(self._range_min[side][i], float(raw[i]))
                        self._range_max[side][i] = max(self._range_max[side][i], float(raw[i]))

        if self.recording:
            self._rec_rows_L.append(list(self.cur_row_L))
            self._rec_rows_R.append(list(self.cur_row_R))

        if self.infer_active and self.infer is not None:
            self._live["L"] = self.infer.update("L", list(self.cur_row_L))
            self._live["R"] = self.infer.update("R", list(self.cur_row_R))

    def _refresh_ui(self) -> None:
        l_ok = self.links["L"].connected and self.left_sig.present
        r_ok = self.links["R"].connected and self.right_sig.present
        if l_ok and r_ok:
            self.status_lbl.setText("Gloves: L ✓   R ✓")
            self.status_lbl.setStyleSheet("font-size:13pt;font-weight:bold;color:#2a7;padding:4px;")
        elif l_ok or r_ok:
            self.status_lbl.setText(f"Gloves: {'L ✓ R —' if l_ok else 'L — R ✓'}  (power on the other)")
            self.status_lbl.setStyleSheet("font-size:13pt;font-weight:bold;color:#a82;padding:4px;")
        else:
            self.status_lbl.setText("Gloves: scanning…  (power on Glove-L and Glove-R)")
            self.status_lbl.setStyleSheet("font-size:13pt;font-weight:bold;color:#c44;padding:4px;")

        idx = self.tabs.currentIndex()
        if idx == 0:
            self._refresh_calib_ui()
        elif idx == 1:
            self._refresh_record_ui()
        elif idx == 4:
            self._refresh_live_ui()

    def _refresh_record_ui(self) -> None:
        def bend_txt(side, sig):
            if not sig.present:
                return f"{side} —"
            fb = " ".join(f"{int(b*100):02d}" for b in sig.finger_bend)
            return f"{side} [{fb}]"
        self.rec_preview.setText(
            f"{bend_txt('L', self.left_sig)}    {bend_txt('R', self.right_sig)}")

    def _on_tab_changed(self, idx: int) -> None:
        if idx == 2:
            self._refresh_label_clip_list()

    def _refresh_calib_ui(self) -> None:
        cal_l = self.frame.is_calibrated("L")
        cal_r = self.frame.is_calibrated("R")
        if cal_l and cal_r:
            self.calib_state_lbl.setText("Calibration: L + R ready ✓")
            self.calib_state_lbl.setStyleSheet("font-size:12pt;font-weight:bold;color:#2a7;")
        else:
            done = ", ".join(s for s, ok in (("L", cal_l), ("R", cal_r)) if ok) or "none"
            self.calib_state_lbl.setText(f"Calibration: {done} ready")
            self.calib_state_lbl.setStyleSheet("font-size:12pt;font-weight:bold;color:#c66;")
        self.calib_progress.setValue(int(self.calib.progress * 100))

        def ready_txt(side, sig):
            if not sig.present:
                return f"{side} pose: glove off"
            ok = self.calib.hand_ready(sig)
            return f"{side} pose: {'HOLDING ✓' if ok else 'not steady/level/open'}"
        self.l_ready_lbl.setText(ready_txt("L", self.left_sig))
        self.r_ready_lbl.setText(ready_txt("R", self.right_sig))

        if not self._range_capturing:
            ranged = [s for s in ("L", "R") if self.cond[s].has_flex_range]
            if ranged:
                self.range_lbl.setText(f"Finger range: {' + '.join(ranged)} captured ✓")
                self.range_lbl.setStyleSheet(MONO + "color:#2a7;")
            else:
                self.range_lbl.setText("Finger range: not captured (using adaptive fallback)")
                self.range_lbl.setStyleSheet(MONO + "color:#c66;")

        self.live_l.setText(self._sig_text(self.left_sig))
        self.live_r.setText(self._sig_text(self.right_sig))

    @staticmethod
    def _sig_text(sig: GloveSignals) -> str:
        if not sig.present:
            return f"{sig.side}: —"
        fb = " ".join(f"{int(b*100):02d}" for b in sig.finger_bend)  # thumb..pinky
        hh = "nan" if math.isnan(sig.h_heading) else f"{sig.h_heading:+.0f}"
        return (f"{sig.side}: wrist_tilt {sig.wrist_tilt:+6.1f}  palm_roll {sig.palm_roll:+6.1f}  "
                f"vvel {sig.vert_vel:+5.2f}  gyro| {sig.gyro_mag:4.2f}  open {sig.openness:.2f}  "
                f"hdg {hh}  bend[{fb}]")

    def _on_capture_range_clicked(self) -> None:
        if self._range_capturing:
            return
        if not (self.left_sig.present or self.right_sig.present):
            QtWidgets.QMessageBox.warning(self, "No gloves", "Power on a glove first.")
            return
        asyncio.ensure_future(self._capture_finger_range())

    async def _capture_finger_range(self) -> None:
        self.btn_capture_range.setEnabled(False)
        for n in (3, 2, 1):
            self.range_lbl.setText(f"Get ready to wiggle… {n}")
            self.range_lbl.setStyleSheet(MONO + "color:#a82;")
            await asyncio.sleep(0.7)
        self._range_min = {s: [float("inf")] * N_CHANNELS for s in ("L", "R")}
        self._range_max = {s: [float("-inf")] * N_CHANNELS for s in ("L", "R")}
        self._range_capturing = True
        dur = float(self.spin_range_dur.value())
        t0 = time.monotonic()
        while time.monotonic() - t0 < dur:
            self.range_lbl.setText(f"WIGGLE ALL FINGERS!  {dur - (time.monotonic() - t0):.1f}s")
            self.range_lbl.setStyleSheet(MONO + "color:#e33;font-weight:bold;")
            await asyncio.sleep(0.1)
        self._range_capturing = False

        self.flex_range = {}
        for side in ("L", "R"):
            lo, hi = self._range_min[side], self._range_max[side]
            ok = (all(math.isfinite(v) for v in lo) and all(math.isfinite(v) for v in hi)
                  and any(hi[i] - lo[i] > 1.0 for i in range(N_CHANNELS)))
            if ok:
                self.flex_range[side] = {"lo": list(lo), "hi": list(hi)}
        self._apply_flex_range()
        self.btn_capture_range.setEnabled(True)
        self.range_lbl.setStyleSheet(MONO)

    def _apply_flex_range(self) -> None:
        for side in ("L", "R"):
            r = self.flex_range.get(side)
            if r:
                self.cond[side].set_flex_range(r["lo"], r["hi"])
            else:
                self.cond[side].set_flex_range(None, None)

    def _manual_forward(self, side: str) -> None:
        sig = self.left_sig if side == "L" else self.right_sig
        if not sig.present or math.isnan(sig.h_heading):
            QtWidgets.QMessageBox.warning(self, "No heading",
                                          f"{side} glove has no valid heading right now.")
            return
        self.frame.set_forward(side, sig.h_heading)

    def _save_calibration(self) -> None:
        if not (self.frame.is_calibrated("L") or self.frame.is_calibrated("R")):
            QtWidgets.QMessageBox.warning(self, "Nothing to save", "Calibrate at least one hand first.")
            return
        default = str(CALIB_DIR / "profile_default.json")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save calibration", default, "JSON (*.json)")
        if not path:
            return
        payload = {
            "profile_id": Path(path).stem,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "forward_L": self.frame.forward["L"],
            "forward_R": self.frame.forward["R"],
            "flex_range": self.flex_range,
        }
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        QtWidgets.QMessageBox.information(self, "Saved", f"Calibration saved:\n{path}")

    def _load_calibration_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load calibration", str(CALIB_DIR), "JSON (*.json)")
        if path:
            self._load_calibration(Path(path))

    def _load_calibration(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Load failed", repr(exc))
            return
        for side in ("L", "R"):
            val = payload.get(f"forward_{side}")
            self.frame.forward[side] = None if val is None else float(val)
        fr = payload.get("flex_range") or {}
        self.flex_range = {}
        for side in ("L", "R"):
            r = fr.get(side)
            if r and "lo" in r and "hi" in r:
                self.flex_range[side] = {"lo": [float(v) for v in r["lo"]],
                                         "hi": [float(v) for v in r["hi"]]}
        self._apply_flex_range()

    def _auto_load_calibration(self) -> None:
        files = sorted(CALIB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            self._load_calibration(files[0])

    # --- recording ---
    def _needed_hands(self, mode: str) -> tuple[str, ...]:
        if mode == MODE_LEFT:
            return ("L",)
        if mode == MODE_RIGHT:
            return ("R",)
        return ("L", "R")

    def _refresh_tally(self) -> None:
        self.tally_tree.clear()
        for (gesture, mode), n in sorted(self.tally.items()):
            QtWidgets.QTreeWidgetItem(self.tally_tree,
                                      [f"{gesture} · {MODE_LABELS.get(mode, mode)}", str(n)])
        self.rec_stats.setText(f"Saved: {self.saved_count}")

    def _undo_last_take(self) -> None:
        if self._rec_busy or self._last_clip_path is None:
            return
        p = self._last_clip_path
        if p.exists():
            try:
                clip = load_clip(p)
                key = (clip.gesture_class, clip.mode)
                if self.tally.get(key):
                    self.tally[key] -= 1
                    if self.tally[key] <= 0:
                        del self.tally[key]
                self.saved_count = max(0, self.saved_count - 1)
                p.unlink()
                self.last_saved_lbl.setText(f"Removed {p.name}")
            except Exception as exc:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(self, "Undo failed", repr(exc))
        self._last_clip_path = None
        self.btn_undo.setEnabled(False)
        self._refresh_tally()

    def _on_record_clicked(self) -> None:
        if self._rec_busy:
            return
        mode = self._current_mode()
        need = self._needed_hands(mode)
        present = {"L": self.left_sig.present, "R": self.right_sig.present}
        missing_gloves = [s for s in need if not present[s]]
        if missing_gloves:
            QtWidgets.QMessageBox.warning(
                self, "Gloves not ready",
                f"Mode '{MODE_LABELS[mode]}' needs glove(s): {', '.join(missing_gloves)}.")
            return
        if any(not self.cond[s].has_flex_range for s in need):
            res = QtWidgets.QMessageBox.question(
                self, "Finger range not set",
                "Finger range isn't captured for every hand in use, so bend % uses the\n"
                "drifting adaptive fallback. Record anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if res != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        asyncio.ensure_future(self._record_take())

    async def _record_take(self) -> None:
        self._rec_busy = True
        self.btn_record.setEnabled(False)
        self.btn_undo.setEnabled(False)
        mode = self._current_mode()
        gesture = self.gesture_combo.currentText()
        countdown = int(self.spin_countdown.value())
        duration = float(self.spin_duration.value())

        for n in range(countdown, 0, -1):
            self.countdown_lbl.setText(str(n))
            await asyncio.sleep(1.0)
        self.countdown_lbl.setText("● REC")

        self._rec_rows_L = []
        self._rec_rows_R = []
        self.recording = True
        t0 = time.monotonic()
        total = max(0.1, duration)
        while time.monotonic() - t0 < duration:
            self.rec_progress.setValue(int((time.monotonic() - t0) / total * 100))
            await asyncio.sleep(0.05)
        self.recording = False
        self.rec_progress.setValue(100)
        self.countdown_lbl.setText("")

        rows_L = list(self._rec_rows_L)
        rows_R = list(self._rec_rows_R)
        n = min(len(rows_L), len(rows_R))
        rows_L, rows_R = rows_L[:n], rows_R[:n]
        if n < 4:
            QtWidgets.QMessageBox.warning(self, "Empty clip", "Too few frames captured.")
        else:
            path = save_clip(gesture, mode, rows_L, rows_R, dict(self.frame.forward))
            self.saved_count += 1
            self._last_clip_path = path
            self.tally[(gesture, mode)] = self.tally.get((gesture, mode), 0) + 1
            self.last_saved_lbl.setText(f"Saved {n} frames → {path.name}")
            self.btn_undo.setEnabled(True)
            self._refresh_tally()

        self._rec_busy = False
        self.rec_progress.setValue(0)
        self.btn_record.setEnabled(True)

    # --- training ---
    def _collect_train_job(self) -> TrainJobConfig:
        selected = {n for n, cb in self.train_class_checks.items() if cb.isChecked()}
        active = resolve_active_vocab(selected)
        aug = AugmentConfig(
            gaussian_noise=self.train_aug_checks["gaussian_noise"].isChecked(),
            bend_scale=self.train_aug_checks["bend_scale"].isChecked(),
            bend_bias=self.train_aug_checks["bend_bias"].isChecked(),
            time_warp=self.train_aug_checks["time_warp"].isChecked(),
            orientation_noise=self.train_aug_checks["orientation_noise"].isChecked(),
            channel_dropout=self.train_aug_checks["channel_dropout"].isChecked(),
        )
        flex_snapshot: dict[str, dict[str, list[float]]] = {}
        for side in ("L", "R"):
            r = self.cond[side].get_flex_range()
            if r:
                flex_snapshot[side] = r
        return TrainJobConfig(
            window=self.spin_window.value(),
            stride=self.spin_stride.value(),
            epochs=self.spin_epochs.value(),
            channels=self.spin_channels.value(),
            active_vocab=active,
            excluded_as_none=self.chk_excluded_as_none.isChecked(),
            augment=aug,
            flex_range=flex_snapshot,
        )

    def _start_training(self) -> None:
        if self.train_worker is not None and self.train_worker.isRunning():
            return
        if not list_clips():
            QtWidgets.QMessageBox.warning(self, "No clips", "Record some takes first.")
            return
        try:
            job = self._collect_train_job()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Training setup", str(exc))
            return
        if not job.flex_range:
            ok = QtWidgets.QMessageBox.warning(
                self, "No flex range",
                "Finger min/max (wiggle) calibration is not set on either hand.\n"
                "Live inference uses flex range from the trained model manifest.\n"
                "Train anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if ok != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.btn_train.setEnabled(False)
        self.train_report.clear()
        self.train_status.setText("Starting…")
        self.train_worker = TrainWorker(job)
        self.train_worker.progress.connect(self.train_status.setText)
        self.train_worker.finished_ok.connect(self._on_train_done)
        self.train_worker.failed.connect(self._on_train_failed)
        self.train_worker.start()

    def _on_train_done(self, manifest: dict) -> None:
        self.btn_train.setEnabled(True)
        tc = manifest.get("train_config", {})
        active = manifest.get("class_names", list(VOCAB))
        lines = [
            f"Model: {manifest['model_id']}",
            f"Window {manifest['window_size']} · stride {manifest['stride']} · {manifest.get('model_arch', '')}",
            f"Classes: {', '.join(active)}",
            f"Flex range saved: {'yes' if manifest.get('flex_range') else 'no'}",
            "",
        ]
        for side in ("L", "R"):
            sd = manifest.get("sides", {}).get(side, {})
            m = sd.get("metrics", {})
            lines += [
                f"=== {side} hand model ===",
                f"  train_acc = {m.get('train_acc', 0):.3f}   "
                f"val_acc = {m.get('val_acc', float('nan')):.3f}   "
                f"val_acc (thresholded) = {m.get('test_acc', 0):.3f}   "
                f"val_acc (raw argmax) = {m.get('raw_val_acc', float('nan')):.3f}",
                f"  false_positive_rate (none) = {m.get('false_positive_rate', 0):.3f}",
                "  per-class precision:",
            ]
            for name in active:
                p = sd.get("per_class_precision", {}).get(name, 0.0)
                r = sd.get("per_class_recall", {}).get(name, 0.0)
                f1 = sd.get("per_class_f1", {}).get(name, 0.0)
                lines.append(f"    {name:<26} P={p:.3f} R={r:.3f} F1={f1:.3f}")
            lines.append("")
        self.train_report.setPlainText("\n".join(lines))
        self.train_status.setText(f"Done · saved {Path(manifest['manifest_path']).name}")
        self._refresh_models()

    def _on_train_failed(self, msg: str) -> None:
        self.btn_train.setEnabled(True)
        self.train_status.setText("Failed.")
        QtWidgets.QMessageBox.critical(self, "Training failed", msg)

    # --- live test ---
    def _refresh_models(self) -> None:
        self.model_combo.clear()
        manifests = sorted(MODELS_DIR.glob("*.manifest.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if not manifests:
            self.model_combo.addItem("(no trained models)", "")
            return
        for p in manifests:
            self.model_combo.addItem(p.stem.replace(".manifest", ""), str(p))

    def _load_model(self) -> None:
        path_s = self.model_combo.currentData()
        if not path_s:
            QtWidgets.QMessageBox.warning(self, "No model", "Train a model first.")
            return
        try:
            manifest_path = Path(path_s)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            fr = manifest.get("flex_range") or {}
            if fr:
                for side in ("L", "R"):
                    r = fr.get(side)
                    if r and "lo" in r and "hi" in r:
                        self.flex_range[side] = {
                            "lo": [float(v) for v in r["lo"]],
                            "hi": [float(v) for v in r["hi"]],
                        }
                self._apply_flex_range()
            self.infer = DualInferencer(manifest_path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Load failed", repr(exc))
            return
        self.infer.reset()
        self.infer_active = True
        self._live = {"L": ("none", 0.0, None), "R": ("none", 0.0, None)}
        classes = ", ".join(self.infer.class_names)
        QtWidgets.QMessageBox.information(
            self, "Model loaded",
            f"Classes: {classes}\n"
            f"Flex min/max: {'applied from model' if fr else 'not in manifest — use wiggle cal'}")

    def _stop_infer(self) -> None:
        self.infer_active = False
        for side in ("L", "R"):
            self.pred_lbl[side].setText("—")
            self.conf_bar[side].setValue(0)
            for bar in self.prob_bars[side].values():
                bar.setValue(0)

    def _refresh_live_ui(self) -> None:
        if not self.infer_active or self.infer is None:
            return
        for side in ("L", "R"):
            label, conf, probs = self._live.get(side, ("none", 0.0, None))
            self.pred_lbl[side].setText(label)
            self.pred_lbl[side].setStyleSheet(
                "font-size:26pt;font-weight:bold;color:%s;"
                % ("#39c" if label == "none" else "#2a7"))
            self.conf_bar[side].setValue(int(conf * 100))
            if probs is not None and self.infer is not None:
                names = self.infer.class_names
                for name in VOCAB:
                    if name in names:
                        j = names.index(name)
                        self.prob_bars[side][name].setValue(int(float(probs[j]) * 100))
                    else:
                        self.prob_bars[side][name].setValue(0)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = GestureTrainerWindow()
    win.show()
    for link in win.links.values():
        asyncio.ensure_future(link.maintain())

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
