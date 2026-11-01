"""
=============================================================================
 conditioning.py  —  THE SIGNAL LAYER  (read this whole header first)
=============================================================================

This file turns one raw BLE packet (`RawFrame`) into one clean, fully-derived
bundle of numbers for ONE glove (`GloveSignals`), once per 60 Hz tick. Every
value a gesture could possibly want is computed here and nowhere else. If you
are about to write gesture recognition, THIS is the instrument panel you read.

This header is the in-code version of "what does each input actually mean in
the real world." It is intentionally long. Read it once and you'll understand
every number on the panel.

-----------------------------------------------------------------------------
 0. THE HARDWARE, IN ONE PARAGRAPH
-----------------------------------------------------------------------------
Each glove has a BNO085 IMU on the back of the hand and 10 flex (bend) sensors
read through an ADC. The IMU gives us, every 10 ms:
  - a rotation quaternion (orientation in space; uses gyro + accelerometer, NO
    magnetometer — see "game rotation vector"),
  - linear acceleration with gravity already subtracted (m/s^2),
  - angular velocity from the gyroscope (rad/s),
and the ADC gives 10 raw bend readings (0..4095). That's all that physically
exists. Everything below is derived from those four things.

-----------------------------------------------------------------------------
 1. THE TWO COORDINATE FRAMES  (this is the #1 source of confusion)
-----------------------------------------------------------------------------
There are two different XYZ frames in play. Mixing them up will ruin you.

(A) WORLD frame  — fixed to the room / gravity. Used for translation (moving
    your whole hand through space). Axes:
       Z = straight UP (opposite gravity).  +Z accel = hand accelerating
           upward; -Z = downward. THIS AXIS IS REAL AND ABSOLUTE — the IMU
           knows which way is down because it feels gravity.
       X, Y = the horizontal plane (parallel to the floor). BUT — and this is
           the trap — the *direction* of X and Y is NOT north/east or
           forward/right. The game rotation vector has no compass, so its
           horizontal reference is "wherever the sensor happened to power on,"
           and it slowly drifts. So world_accel.x / .y and world_vel.x / .y
           tell you "the hand is moving horizontally this fast," but NOT
           reliably "forward" vs "sideways." Treat horizontal translation as a
           magnitude, not a labeled direction. Only UP/DOWN (Z) is trustworthy
           as an absolute direction.
       => Practical takeaway: vertical hand motion (raise/lower) is rock-solid.
          "Which horizontal direction am I pointing?" comes from HEADING +
          CALIBRATION (section 4), not from world X/Y.

(B) BODY frame  — fixed to your hand. Used for orientation (how the wrist is
    twisted/tilted), reported as the three Euler angles yaw / pitch / roll.
    The IMU is mounted upside-down on the PCB, so we rotate the quaternion by a
    fixed mount correction before reading these angles (so they match your real
    hand, matching glove_cello.py / glove_visualizer.py).

-----------------------------------------------------------------------------
 2. ORIENTATION:  yaw / pitch / roll  (degrees) — "how is the wrist held?"
-----------------------------------------------------------------------------
These describe rotation of the hand itself. Imagine your forearm pointing
straight ahead, palm down, fingers forward — that's the rough zero pose.

  pitch  = tilt of the hand UP/DOWN (nodding the wrist).
             Increase pitch  -> fingertips rise toward the ceiling.
             Decrease pitch  -> fingertips dip toward the floor.
             Range ~ -90 (pointing straight down) .. +90 (straight up).
             This is your "wrist tilt" knob. The OLD semitone gesture used it.

  roll   = rotation about the pointing axis (turning a doorknob / your palm).
             On THIS hardware + mount correction, a level PALM-DOWN hand reads
             |roll| ~ 180 deg, and turning the palm UP reads |roll| ~ 0 deg.
             (Yes, that's backwards from intuition — it's a mount artifact, and
             it's why the OLD "ALL mode = palms up" test checks for SMALL roll.)
             Increase from 0: palm rotates from up toward down/sideways.

  yaw    = heading / turning LEFT-RIGHT about the vertical axis (shaking head
             "no"). Increase yaw -> hand swings to one side horizontally.
             CAUTION: Euler yaw goes haywire ("gimbal lock") when the hand
             points near straight up or down, and it has no fixed reference.
             For "which way am I pointing horizontally" we DO NOT use yaw — we
             use `h_heading` instead (section 4). yaw is here for completeness;
             prefer h_heading for anything pointing-related.

  All three are low-pass smoothed (orient_tau) so they don't jitter.

-----------------------------------------------------------------------------
 3. ROTATION RATES:  angular velocity & angular acceleration
-----------------------------------------------------------------------------
  gyro / ang_vel_dps  = how fast the hand is ROTATING right now, per axis.
       `gyro` is the raw sensor value in rad/s (kept because calibration and the
       old detectors use it). `ang_vel_dps` is the same thing converted to
       deg/s (human-friendly). These are body-axis spin rates: roughly
         x ~ roll rate, y ~ pitch rate, z ~ yaw rate (don't over-trust the
       per-axis labels; the IMU axes aren't perfectly aligned to your wrist).
       `gyro_mag` / `ang_vel_mag_dps` = overall "how fast am I twisting/waving"
       (direction-agnostic). This is your best "is the hand busy rotating?"
       single number — high during flutters, picks, and big arm swings.

  ang_accel_dps2 = angular ACCELERATION (deg/s^2): the time-derivative of
       angular velocity = how quickly the spin rate itself is changing. High at
       the START and STOP of a rotation (the "snap"). Useful if you want to
       detect a sharp flick vs. a smooth turn. (Newly added; noisy by nature —
       it's a derivative of a derivative — so smooth/threshold it generously.)

-----------------------------------------------------------------------------
 4. POINTING:  h_heading + the calibration that DON'T touch
-----------------------------------------------------------------------------
  h_heading = a horizontal compass-like heading in degrees, computed by
       projecting the hand's pointing axis onto the floor plane. Unlike Euler
       yaw it stays sane even when the hand points down. It is NaN when the hand
       points nearly straight up/down (no horizontal component to measure).
       It has NO absolute zero (no magnetometer) and drifts slowly — which is
       exactly why calibration exists.

  CALIBRATION (calibration.py + selection.py ReferenceFrame) stores, per hand,
       the h_heading you were pointing at when you did the "point forward + hold
       + flutter" gesture. After that, your pointing is measured RELATIVE to
       that stored forward, and binned into regions 0..n-1 across the front
       half-plane. >>> This is the part you said works — it is untouched. <<<
       The per-hand relative heading and region live on the Selection object
       (sel.left.heading, sel.left.region), not on GloveSignals.

-----------------------------------------------------------------------------
 5. TRANSLATION:  world_accel + world_vel  — "moving the whole hand"
-----------------------------------------------------------------------------
  world_accel = linear acceleration rotated into the WORLD frame (m/s^2).
       gravity already removed. world_accel[2] (== vert_accel) is up/down
       acceleration and is the trustworthy one (see frame note in section 1).

  world_vel = world_accel integrated into a VELOCITY (m/s), per axis, with slow
       bias removal so it self-zeros between moves (a raw integral of a noisy
       accelerometer drifts to infinity; we high-pass it and hard-zero it when
       the hand is detected truly still). world_vel[2] (== vert_vel) is the
       up/down speed of the hand — the OLD scale-step gesture rode this.
       Honest caveat: this is good for ~1 second gestures and self-zeros; very
       long, very slow constant-speed glides under-read. X/Y are "horizontal
       speed in a drifting frame" (see section 1) — magnitude is meaningful,
       absolute direction is not.

-----------------------------------------------------------------------------
 6. FINGERS:  finger_bend  (this is the ONLY finger signal you asked to keep)
-----------------------------------------------------------------------------
  finger_bend = per-finger bend amount, 0.0 .. 1.0, order (thumb, index,
       middle, ring, pinky). 0.0 = finger fully EXTENDED (straight); 1.0 =
       fully CURLED/bent. Multiply by 100 for a percentage.
       Increase a finger's value -> that finger curls in toward the palm.

       How it's "calibrated": each of the 10 raw ADC channels is auto-normalized
       by a rolling min/max envelope (EnvelopeMinMax) that snaps to new extremes
       instantly and relaxes inward over ~5 minutes. So the 0..1 range
       continuously self-adapts to sensor drift — it is its OWN calibration,
       SEPARATE from the forward-heading calibration in section 4. Two ADC
       channels per finger (a proximal + a distal joint sensor) are averaged.

       THUMB WARNING: the thumb's flex sensor is unreliable (fully extending the
       thumb can physically bend it, reading as "bent"). Every existing
       finger-based test ignores the thumb (uses fingers index..pinky only).
       I'd keep doing that.

  raw_flex / finger_raw = the untouched numbers, in case you want to do your own
       normalization instead of trusting the auto-range:
         raw_flex  = all 10 raw ADC ints (0..4095), in firmware channel order
                     [0..7] = mux channels, [8] = thumb_lo, [9] = thumb_hi.
         finger_raw = those averaged per finger (thumb..pinky), still raw units.

-----------------------------------------------------------------------------
 7. WHAT MIGHT BITE YOU  (raw-data overlaps & ambiguities to design around)
-----------------------------------------------------------------------------
These are the real-world ambiguities the OLD code spent most of its complexity
fighting. You'll face the same ones; forewarned is forearmed:

  * Raising the arm ALSO rotates the wrist. A vertical raise isn't pure
    translation — the gyro spikes and pitch changes too. So "translate up"
    (scale) and "tilt up" (semitone) overlap badly. The old code separated them
    by DOMINANT MODALITY (big vert_vel + low pitch_rate = translate; big
    pitch_rate + low vert_vel = tilt) and it's still fiddly.
  * Fluttering / picking fingers SHAKES the whole IMU. Finger gestures leak into
    gyro, pitch_rate, and even integrate into a phantom vert_vel. The old code
    drops impossible single-frame pitch jumps and gates pitch on gyro.
  * A monotonic finger CLOSE (a grab) looks a lot like a fast finger FLUTTER to
    a high-pass filter. Old code attenuated when the hand wasn't open.
  * Entering a pose with a big flip (e.g. palms-up) injects a huge transient
    into the integrated velocity — the old ALL-mode waits for the wrists to
    settle before trusting anything.
  * world X/Y horizontal direction drifts (no compass). Don't build a gesture
    that needs absolute "north"; use vertical (Z) + calibrated heading instead.
  * h_heading is NaN pointing straight up/down — always guard for NaN.
  * Euler yaw/pitch/roll can wrap at +/-180 and gimbal-lock near vertical — use
    h_heading for pointing, and prefer roll over pitch for "palm orientation."

-----------------------------------------------------------------------------
 8. THE PANEL ITSELF
-----------------------------------------------------------------------------
`GloveSignals` below is grouped into:
   (A) "YOUR GESTURE INPUTS"  — the clean, documented set you build gestures on.
   (B) "CALIBRATION + LEGACY"  — signals the working calibration and the OLD
        detectors need. You can ignore these for new work; they're kept so the
        app, calibration, recorder, and replay keep running. (`openness` /
        `is_open` in particular are consumed by calibration — DO NOT remove.)
=============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

import math

from .config import (
    ConditioningConfig,
    FINGER_CHANNELS_L,
    FINGER_CHANNELS_R,
    FINGER_NAMES,
    SelectionConfig,
)
from .dsp import (CircularOnePole, EnvelopeFollower, EnvelopeMinMax, HighPass,
                  LeakyVelocity, OnePole, SlidingMinMax, wrap180)
from .orientation import (apply_mount_correction, horizontal_heading_deg,
                          magnitude, quat_to_euler_deg, rotate_vector_to_world)
from .frames import RawFrame


@dataclass
class GloveSignals:
    """One glove's fully-conditioned state for a single tick. See file header
    for the physical meaning of every field."""

    # === (A) YOUR GESTURE INPUTS ============================================
    side: str                         # "L" or "R"
    present: bool                     # False if no fresh packet this tick (glove off/out of range)

    # -- orientation: how the wrist is held (degrees) -- (section 2) --
    yaw: float                        # heading-ish left/right; gimbal-locks near vertical (prefer h_heading)
    pitch: float                      # wrist tilt up/down; +up fingers, -down fingers
    roll: float                       # palm rotation; level palm-down ~ |180|, palm-up ~ 0 (mount artifact)

    # -- rotation rates -- (section 3) --
    gyro: tuple[float, float, float]  # angular velocity (rad/s), body axes (raw; calibration/legacy use this)
    gyro_mag: float                   # |gyro| (rad/s) — "how fast am I twisting", direction-agnostic
    ang_vel_dps: tuple[float, float, float]  # angular velocity in deg/s (human-friendly gyro)
    ang_vel_mag_dps: float            # |angular velocity| in deg/s
    ang_accel_dps2: tuple[float, float, float]  # angular acceleration (deg/s^2): the "snap" of a rotation
    ang_accel_mag_dps2: float         # |angular acceleration| (deg/s^2)

    # -- translation: moving the whole hand through space -- (section 5) --
    world_accel: tuple[float, float, float]  # linear accel in world frame (m/s^2); Z=up is the trustworthy axis
    vert_accel: float                 # == world_accel[2]; up/down acceleration (m/s^2)
    lin_accel_mag: float              # |world_accel| (m/s^2) — overall linear push
    world_vel: tuple[float, float, float]    # linear velocity in world frame (m/s), bias-removed, self-zeroing
    vert_vel: float                   # == world_vel[2]; up/down speed (m/s) — old scale gesture used this

    # -- fingers: the per-finger bend you asked to keep -- (section 6) --
    finger_bend: tuple[float, ...]    # 0..1 per finger (thumb,index,middle,ring,pinky); 1 = fully curled
    raw_flex: tuple[int, ...]         # the 10 untouched ADC values (0..4095), firmware channel order
    finger_raw: tuple[float, ...]     # raw ADC averaged per finger (thumb..pinky), pre-normalization

    # -- pointing source (the calibration in section 4 turns this into regions) --
    h_heading: float                  # horizontal heading (deg); NaN when pointing straight up/down

    # === (B) CALIBRATION + LEGACY support (safe to ignore for new gestures) ==
    # Calibration (DO NOT remove — calibration.py reads is_open / openness):
    is_open: bool                     # all non-thumb fingers below open threshold (used by calibration)
    openness: float                   # 1 = fully open, 0 = closed (non-thumb); calibration's pick detector
    game_status: int                  # IMU game-RV fusion accuracy 0..3 (heading-source health; cal gate)
    rv_accuracy: float                # absolute-heading accuracy estimate (radians); drift warning on HUD
    # Legacy alias kept identical for the OLD detectors / recorder / replay:
    finger_flex: tuple[float, ...]    # SAME numbers as finger_bend (old name; kept so nothing breaks)
    hand_steady: bool                 # low-gyro flag the old pick latch used
    pitch_rate: float                 # smoothed d(pitch)/dt (deg/s); old semitone gesture rode this
    forward_up: float                 # world-Z of the pointing axis (fingers-up ~ +1); old timbre pose
    flex_norm: tuple[float, ...]      # per-CHANNEL normalized flex (10 values), pre finger-averaging
    wiggle: float                     # OLD twinkle energy (timbre) — legacy detector only
    spawn_flutter: float              # OLD spawn flutter energy — legacy detector only


class GloveConditioner:
    """Stateful per-glove conditioner. Call `process` once per pipeline tick."""

    def __init__(
        self,
        side: str,
        n_flex: int,
        cfg: ConditioningConfig,
        scfg: SelectionConfig,
    ):
        self.side = side
        self.cfg = cfg
        self.scfg = scfg
        self._channels = FINGER_CHANNELS_L if side == "L" else FINGER_CHANNELS_R
        if cfg.flex_window_s is not None and cfg.flex_window_s > 0:
            self._flex_norm = [
                SlidingMinMax(cfg.flex_window_s, cfg.flex_min_span)
                for _ in range(n_flex)
            ]
        else:
            self._flex_norm = [
                EnvelopeMinMax(cfg.flex_env_decay_per_s, cfg.flex_min_span)
                for _ in range(n_flex)
            ]
        self._finger_hp = [HighPass(cfg.wiggle_hp_tau) for _ in FINGER_NAMES]
        self._spawn_hp = [HighPass(cfg.spawn_flutter_hp_tau) for _ in FINGER_NAMES]
        self._gyro = [OnePole(cfg.gyro_tau) for _ in range(3)]
        self._yaw = OnePole(cfg.orient_tau)
        self._pitch = OnePole(cfg.orient_tau)
        self._roll = OnePole(cfg.orient_tau)
        # Heading is an angle: smooth on the circle so it never glitches at ±180°.
        self._h_heading = CircularOnePole(cfg.orient_tau)
        self._openness = OnePole(cfg.openness_tau, initial=1.0)
        self._wiggle_env = EnvelopeFollower(cfg.wiggle_env_attack_s,
                                            cfg.wiggle_env_release_s)
        self._spawn_env = EnvelopeFollower(cfg.spawn_env_attack_s,
                                           cfg.spawn_env_release_s)
        # Kinematics for the gesture layer.
        # One velocity integrator per WORLD axis (X, Y, Z). Z is vert_vel.
        self._wvel = [LeakyVelocity(cfg.vel_bias_tau) for _ in range(3)]
        self._still_t = 0.0
        self._prev_pitch: float | None = None
        self._pitch_rate_lp = OnePole(cfg.pitch_rate_tau)
        # Angular acceleration = smoothed derivative of the (smoothed) gyro.
        self._prev_gyro: tuple[float, float, float] | None = None
        self._ang_acc_lp = [OnePole(cfg.gyro_tau) for _ in range(3)]

    def process(self, frame: RawFrame | None, dt: float) -> GloveSignals:
        empty_fingers = tuple(0.0 for _ in FINGER_NAMES)
        if frame is None:
            # No fresh packet: report a neutral, "absent" panel. Defaults on the
            # legacy fields keep calibration/replay constructors happy.
            return GloveSignals(
                side=self.side, present=False,
                yaw=0.0, pitch=0.0, roll=0.0,
                gyro=(0.0, 0.0, 0.0), gyro_mag=0.0,
                ang_vel_dps=(0.0, 0.0, 0.0), ang_vel_mag_dps=0.0,
                ang_accel_dps2=(0.0, 0.0, 0.0), ang_accel_mag_dps2=0.0,
                world_accel=(0.0, 0.0, 0.0), vert_accel=0.0, lin_accel_mag=0.0,
                world_vel=(0.0, 0.0, 0.0), vert_vel=0.0,
                finger_bend=empty_fingers,
                raw_flex=tuple(0 for _ in self._flex_norm),
                finger_raw=empty_fingers,
                h_heading=float("nan"),
                is_open=True, openness=1.0, game_status=0, rv_accuracy=0.0,
                finger_flex=empty_fingers, hand_steady=True,
                pitch_rate=0.0, forward_up=0.0,
                flex_norm=tuple(0.0 for _ in self._flex_norm),
                wiggle=0.0, spawn_flutter=0.0,
            )

        hand_quat = frame.quat
        # Body-frame orientation: the BNO is mounted upside-down on the PCB, so
        # the *physical* hand roll/pitch/pointing axis must come from the
        # mount-corrected quaternion (matches glove_cello / glove_visualizer).
        # h_heading and world_accel stay on the raw quat below — those are
        # relative / mount-independent and already calibrate correctly.
        body_quat = apply_mount_correction(hand_quat)
        yaw, pitch, roll = quat_to_euler_deg(*body_quat)
        yaw = self._yaw.update(yaw, dt)
        pitch = self._pitch.update(pitch, dt)
        roll = self._roll.update(roll, dt)

        raw_h = horizontal_heading_deg(hand_quat)
        if math.isnan(raw_h):
            h_heading = raw_h
        else:
            h_heading = self._h_heading.update(raw_h, dt)

        # --- rotation rates ---
        gyro = tuple(self._gyro[i].update(frame.gyro[i], dt) for i in range(3))
        gyro_mag = magnitude(gyro)
        ang_vel_dps = tuple(math.degrees(g) for g in gyro)
        ang_vel_mag_dps = math.degrees(gyro_mag)
        # Angular acceleration: derivative of the smoothed gyro, then smoothed
        # again (a raw derivative of a derivative is very noisy). deg/s^2.
        if self._prev_gyro is None:
            self._prev_gyro = gyro
        ang_acc = []
        for i in range(3):
            inst = (gyro[i] - self._prev_gyro[i]) / max(dt, 1e-4)
            ang_acc.append(self._ang_acc_lp[i].update(inst, dt))
        self._prev_gyro = gyro
        ang_accel_dps2 = tuple(math.degrees(a) for a in ang_acc)
        ang_accel_mag_dps2 = math.degrees(magnitude(tuple(ang_acc)))

        # --- translation (world frame): accel + integrated velocity ---
        world_accel = rotate_vector_to_world(frame.quat, frame.accel, mount_correct=False)
        ax, ay, az = world_accel
        lin_accel_mag = math.hypot(ax, ay, az)
        still = (gyro_mag < self.cfg.vel_zero_gyro
                 and abs(az) < self.cfg.vel_zero_accel
                 and math.hypot(ax, ay) < self.cfg.vel_zero_accel)
        self._still_t = self._still_t + dt if still else 0.0
        zero = self._still_t >= self.cfg.vel_zero_dwell_s
        world_vel = tuple(self._wvel[i].update(world_accel[i], dt, zero=zero)
                          for i in range(3))
        vert_vel = world_vel[2]

        # --- pitch rate (legacy semitone signal) ---
        if self._prev_pitch is None:
            self._prev_pitch = pitch
        inst_rate = wrap180(pitch - self._prev_pitch) / max(dt, 1e-4)
        self._prev_pitch = pitch
        # Drop physically impossible single-frame jumps (flutter/pick wrist whip,
        # euler noise) so they neither trip a phantom semitone nor block a scale
        # step; let the smoothed estimate decay back down naturally.
        if abs(inst_rate) > self.cfg.pitch_rate_spike_deg_s:
            inst_rate = 0.0
        pitch_rate = self._pitch_rate_lp.update(inst_rate, dt)

        # World-Z of the body pointing axis (+X): fingers up ≈ +1, level ≈ 0.
        # Uses the mount-corrected body quat so "+X" is the real finger axis.
        forward_up = rotate_vector_to_world(body_quat, (1.0, 0.0, 0.0),
                                            mount_correct=False)[2]

        # --- fingers: normalize each channel, then average to per-finger bend ---
        norm = []
        for i, raw in enumerate(frame.flex):
            v = self._flex_norm[i].update(float(raw), dt)
            if not self.cfg.flex_bend_is_high:
                v = 1.0 - v
            norm.append(v)

        finger_bend = self._finger_flex(norm)
        finger_raw = self._finger_raw(frame.flex)
        is_open, openness_raw = self._openness_from_fingers(finger_bend)
        openness = self._openness.update(openness_raw, dt)
        hand_steady = gyro_mag <= self.scfg.pick_steady_gyro_max
        wiggle = self._twinkle_energy(finger_bend, gyro_mag, dt)
        spawn_flutter = self._spawn_flutter_energy(finger_bend, gyro_mag, dt,
                                                   is_open=is_open)

        return GloveSignals(
            side=self.side, present=True,
            yaw=yaw, pitch=pitch, roll=roll,
            gyro=gyro, gyro_mag=gyro_mag,
            ang_vel_dps=ang_vel_dps, ang_vel_mag_dps=ang_vel_mag_dps,
            ang_accel_dps2=ang_accel_dps2, ang_accel_mag_dps2=ang_accel_mag_dps2,
            world_accel=world_accel, vert_accel=az, lin_accel_mag=lin_accel_mag,
            world_vel=world_vel, vert_vel=vert_vel,
            finger_bend=finger_bend, raw_flex=tuple(frame.flex), finger_raw=finger_raw,
            h_heading=h_heading,
            is_open=is_open, openness=openness,
            game_status=frame.game_status, rv_accuracy=frame.rv_accuracy,
            finger_flex=finger_bend, hand_steady=hand_steady,
            pitch_rate=pitch_rate, forward_up=forward_up,
            flex_norm=tuple(norm), wiggle=wiggle, spawn_flutter=spawn_flutter,
        )

    def _finger_flex(self, norm: list[float]) -> tuple[float, ...]:
        """Average the two ADC channels that belong to each finger -> 0..1 bend."""
        out: list[float] = []
        for name in FINGER_NAMES:
            chs = self._channels[name]
            out.append(sum(norm[i] for i in chs) / len(chs))
        return tuple(out)

    def _finger_raw(self, flex: tuple[int, ...]) -> tuple[float, ...]:
        """Same channel pairing as _finger_flex, but on the RAW ADC ints."""
        out: list[float] = []
        for name in FINGER_NAMES:
            chs = self._channels[name]
            out.append(sum(float(flex[i]) for i in chs) / len(chs))
        return tuple(out)

    def _openness_from_fingers(
        self, finger_bend: tuple[float, ...]
    ) -> tuple[bool, float]:
        """Open = all non-thumb fingers below finger_open_max (default pose).
        Used by calibration's pick detector — keep behaviour identical."""
        open_max = self.scfg.finger_open_max
        non_thumb = finger_bend[1:]
        is_open = all(f <= open_max for f in non_thumb)
        if not non_thumb:
            return True, 1.0
        max_bend = max(non_thumb)
        if max_bend <= open_max:
            return True, 1.0
        span = max(1e-6, 1.0 - open_max)
        return False, max(0.0, 1.0 - (max_bend - open_max) / span)

    # ------------------------------------------------------------------ #
    # LEGACY finger-motion energies. These exist only for the OLD spawn /
    # timbre detectors in gestures.py (which you are replacing). They are NOT
    # part of the clean panel. Left intact so the current app keeps working.
    # ------------------------------------------------------------------ #
    def _twinkle_energy(
        self, finger_bend: tuple[float, ...], gyro_mag: float, dt: float
    ) -> float:
        """Twinkle = several fingers oscillating independently while the wrist
        stays put. Single-finger closes (pick) and arm sweeps are suppressed."""
        c = self.cfg
        if gyro_mag > c.twinkle_still_gyro_max:
            raw = 0.0
        else:
            osc = []
            for i, flex in enumerate(finger_bend):
                osc.append(abs(self._finger_hp[i].update(flex, dt)))
            active = [o for o in osc if o > c.twinkle_finger_osc_thresh]
            if len(active) < c.twinkle_min_active_fingers:
                raw = 0.0
            else:
                raw = (sum(active) / len(active)) * (len(active) / len(finger_bend))
                raw *= c.twinkle_randomness_gain
        return min(1.0, self._wiggle_env.update(raw, dt))

    def _spawn_flutter_energy(
        self, finger_bend: tuple[float, ...], gyro_mag: float, dt: float,
        *, is_open: bool,
    ) -> float:
        """Spawn flutter: sum of per-finger high-pass magnitudes (non-thumb).
        Looser than timbre twinkle; attenuated when the hand isn't open so a
        slow grab doesn't masquerade as a flutter."""
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
