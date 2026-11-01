"""GloveConditioner: collier RawFrame -> v3 GloveSignals."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .config import (
    CondCfg,
    FINGER_CHANNELS_L,
    FINGER_CHANNELS_R,
    FINGER_NAMES,
    N_CHANNELS,
)
from .dsp import (
    CircularOnePole,
    EnvelopeFollower,
    EnvelopeMinMax,
    HighPass,
    LeakyVelocity,
    OnePole,
    clamp,
    wrap180,
)
from .orientation import (
    apply_mount_correction,
    body_basis_vectors,
    horizontal_heading_deg,
    magnitude,
    quat_to_euler_deg,
    rotate_vector_to_body,
    rotate_vector_to_world,
)

if TYPE_CHECKING:
    from collier.frames import RawFrame


class FlexFrame(Protocol):
    quat: tuple[float, float, float, float]
    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]
    flex: tuple[int, ...]
    game_status: int
    rv_accuracy: float


@dataclass
class GloveSignals:
    side: str
    present: bool
    wrist_tilt: float = 0.0
    palm_roll: float = 0.0
    wrist_tilt_rate: float = 0.0
    palm_roll_rate: float = 0.0
    forward_up: float = 0.0
    openness: float = 1.0
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
    hand_steady: bool = True


class GloveConditioner:
    def __init__(self, side: str, cfg: CondCfg | None = None):
        self.side = side
        self.cfg = cfg or CondCfg()
        c = self.cfg
        self._channels = FINGER_CHANNELS_L if side == "L" else FINGER_CHANNELS_R
        self._flex_norm = [
            EnvelopeMinMax(c.flex_env_decay_per_s, c.flex_min_span) for _ in range(N_CHANNELS)
        ]
        self._flex_lo: list[float] | None = None
        self._flex_hi: list[float] | None = None
        self._finger_hp = [HighPass(c.wiggle_hp_tau) for _ in FINGER_NAMES]
        self._osc_hp = [HighPass(c.wiggle_hp_tau) for _ in FINGER_NAMES]
        self._spawn_hp = [HighPass(c.spawn_flutter_hp_tau) for _ in FINGER_NAMES]
        self._gyro = [OnePole(c.gyro_tau) for _ in range(3)]
        self._yaw = OnePole(c.orient_tau)
        self._pitch = OnePole(c.orient_tau)
        self._roll = OnePole(c.orient_tau)
        self._h_heading = CircularOnePole(c.orient_tau)
        self._openness = OnePole(c.openness_tau, initial=1.0)
        self._wiggle_env = EnvelopeFollower(c.wiggle_env_attack_s, c.wiggle_env_release_s)
        self._spawn_env = EnvelopeFollower(c.spawn_env_attack_s, c.spawn_env_release_s)
        self._wvel = [LeakyVelocity(c.vel_bias_tau) for _ in range(3)]
        self._still_t = 0.0
        self._prev_pitch: float | None = None
        self._prev_roll: float | None = None
        self._pitch_rate_lp = OnePole(c.pitch_rate_tau)
        self._wrist_tilt_rate_lp = OnePole(c.pitch_rate_tau)

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
            return clamp((raw - self._flex_lo[i]) / span, 0.0, 1.0)
        return self._flex_norm[i].update(raw, dt)

    def process(self, frame: FlexFrame | None, dt: float) -> GloveSignals:
        empty = tuple(0.0 for _ in FINGER_NAMES)
        if frame is None:
            return GloveSignals(
                side=self.side,
                present=False,
                finger_bend=empty,
                finger_osc=empty,
                raw_flex=tuple(0 for _ in range(N_CHANNELS)),
                h_heading=float("nan"),
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
        still = (
            gyro_mag < c.vel_zero_gyro
            and abs(az) < c.vel_zero_accel
            and math.hypot(ax, ay) < c.vel_zero_accel
        )
        self._still_t = self._still_t + dt if still else 0.0
        zero = self._still_t >= c.vel_zero_dwell_s
        world_vel = tuple(self._wvel[i].update(world_accel[i], dt, zero=zero) for i in range(3))
        vert_vel = world_vel[2]
        vel_horiz_mag = math.hypot(world_vel[0], world_vel[1])
        along, across, _palm = body_basis_vectors(body_quat)
        vel_along = (
            world_vel[0] * along[0] + world_vel[1] * along[1] + world_vel[2] * along[2]
        )
        vel_across = (
            world_vel[0] * across[0] + world_vel[1] * across[1] + world_vel[2] * across[2]
        )

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
            side=self.side,
            present=True,
            wrist_tilt=wrist_tilt,
            palm_roll=palm_roll,
            wrist_tilt_rate=wrist_tilt_rate,
            palm_roll_rate=palm_roll_rate,
            forward_up=forward_up,
            openness=openness,
            hand_gyro=hand_gyro,
            hand_accel=hand_accel,
            vert_vel=vert_vel,
            vel_along=vel_along,
            vel_across=vel_across,
            vel_horiz_mag=vel_horiz_mag,
            lin_accel_mag=lin_accel_mag,
            finger_bend=finger_bend,
            finger_osc=finger_osc,
            raw_flex=tuple(frame.flex),
            h_heading=h_heading,
            is_open=is_open,
            yaw=yaw,
            pitch=palm_roll,
            roll=wrist_tilt,
            pitch_rate=palm_roll_rate,
            gyro_mag=gyro_mag,
            world_accel=world_accel,
            wiggle=wiggle,
            spawn_flutter=spawn_flutter,
            game_status=frame.game_status,
            rv_accuracy=frame.rv_accuracy,
            hand_steady=still,
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
            osc = [
                abs(self._spawn_hp[i].update(finger_bend[i], dt))
                for i in range(1, len(finger_bend))
            ]
            active = [o for o in osc if o > c.twinkle_finger_osc_thresh]
            raw = min(1.0, sum(osc) * c.spawn_flutter_gain)
            if not is_open and len(active) < 2:
                raw *= 0.25
        return min(1.0, self._spawn_env.update(raw, dt))
