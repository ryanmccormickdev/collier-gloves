"""
Central configuration for the conducting-gloves instrument.

Everything you will want to *tune by feel* lives here, grouped by layer. The
philosophy: code in the other modules should read parameters from a Config
object, never hard-code a threshold. When a gesture feels twitchy or sluggish,
you change a number here and nowhere else.

Units convention:
  - angles exposed to humans (yaw/pitch/roll, headings, region edges): DEGREES
  - gyro: rad/s (as the firmware sends it)
  - accel: m/s^2 (gravity already removed by the BNO085)
  - time: seconds
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Transport / packet
# ---------------------------------------------------------------------------
@dataclass
class BleConfig:
    service_uuid: str = "5a3e8f00-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
    char_uuid: str = "5a3e8f01-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
    # name -> side label. id 0 = left, id 1 = right (matches firmware).
    glove_names: tuple[str, ...] = ("Glove-L", "Glove-R")
    scan_timeout_s: float = 8.0


@dataclass
class PacketConfig:
    # Must match the firmware struct exactly.
    #   <BBH4f3f3f10H4ffBB -> 86 bytes  (v3: game RV + stabilized RV + accuracy/status)
    #   <BBH4f3f3f10H      -> 64 bytes  (v2: 10 flex channels via mux + thumb)
    #   <BBH4f3f3f5H       -> 54 bytes  (older: 5 flex channels)
    #
    # v3 layout keeps the v2 fields at identical offsets (glove_id, seq, ts,
    # primary quat, accel, gyro, flex) and appends: absolute quat (4f), the
    # heading-accuracy estimate (f, radians), and two status bytes (B, B).
    fmt: str = "<BBH4f3f3f10H4ffBB"
    flex_count: int = 10

    @property
    def size(self) -> int:
        return struct.calcsize(self.fmt)

    def __post_init__(self) -> None:
        # Sanity: the format string must contain the flex_count uint16 block.
        # (In v3 the flex array is followed by appended fields, so it's no
        # longer at the very end — just require it to be present.)
        assert f"{self.flex_count}H" in self.fmt, (
            f"fmt {self.fmt!r} does not contain {self.flex_count}H"
        )


# ---------------------------------------------------------------------------
# Processing loop
# ---------------------------------------------------------------------------
@dataclass
class ProcessConfig:
    rate_hz: float = 60.0          # fixed-rate pipeline tick (decoupled from BLE jitter)
    stale_after_s: float = 0.4     # a glove with no packet for this long is "absent"

    @property
    def dt(self) -> float:
        return 1.0 / self.rate_hz


# ---------------------------------------------------------------------------
# Finger channel maps (from glove_cello.py calibration sessions)
# ---------------------------------------------------------------------------
FINGER_NAMES: tuple[str, ...] = ("thumb", "index", "middle", "ring", "pinky")

FINGER_CHANNELS_L: dict[str, tuple[int, int]] = {
    "thumb":  (5, 2),
    "index":  (7, 4),
    "middle": (0, 3),
    "ring":   (1, 6),
    "pinky":  (9, 8),
}
FINGER_CHANNELS_R: dict[str, tuple[int, int]] = {
    "thumb":  (9, 8),
    "index":  (4, 3),
    "middle": (6, 0),
    "ring":   (7, 1),
    "pinky":  (5, 2),
}


# ---------------------------------------------------------------------------
# Signal conditioning
# ---------------------------------------------------------------------------
@dataclass
class ConditioningConfig:
    # Flex normalization: decaying min/max envelope (adapts to drift in ~minutes).
    # Higher decay = faster adaptation but more sensitive to a quiet hand.
    flex_env_decay_per_s: float = 0.0017 # min/max walls relax inward (~5 min time constant)
    flex_min_span: float = 80.0          # guard against div-by-zero on dead channels
    # When set (e.g. 60.0 for collier_live), use a sliding min/max over this many
    # seconds instead of the all-time decaying envelope above.
    flex_window_s: float | None = None
    flex_bend_is_high: bool = True       # True: bigger raw reading == more bent

    # Smoothing time constants (seconds). Bigger = smoother + laggier.
    gyro_tau: float = 0.03
    orient_tau: float = 0.02
    openness_tau: float = 0.08

    # Twinkle: multi-finger random oscillation while the wrist stays put.
    # Per-finger high-pass on normalized flex; count how many fingers are wiggling.
    wiggle_hp_tau: float = 0.08          # high-pass to drop slow open/close
    wiggle_env_attack_s: float = 0.03
    wiggle_env_release_s: float = 0.10
    twinkle_finger_osc_thresh: float = 0.04   # per-finger HP magnitude to count as active
    twinkle_min_active_fingers: int = 2       # need this many fingers moving at once
    twinkle_still_gyro_max: float = 3.5       # rad/s — orientation must stay this still (timbre twinkle)
    twinkle_randomness_gain: float = 4.0      # scales multi-finger chaos toward 0..1

    # Spawn flutter: looser than timbre twinkle — any rapid finger motion counts,
    # even if all fingers move together (a pick-flutter) or the wrist shakes a bit.
    spawn_flutter_hp_tau: float = 0.06        # slightly faster HP than timbre twinkle
    spawn_flutter_gain: float = 2.8           # scales sum(|finger HP|) toward 0..1
    spawn_still_gyro_max: float = 5.0         # rad/s — tolerate IMU shake while fluttering
    spawn_env_attack_s: float = 0.02
    spawn_env_release_s: float = 0.08

    # Kinematics used by the gesture layer (computed here so they're also on the HUD):
    #   vert_vel   — world-vertical velocity, from integrating linear accel with
    #                slow bias removal; self-zeros between moves.
    #   pitch_rate — smoothed d(pitch)/dt in deg/s.
    #   forward_up — world-Z component of the hand's pointing axis (fingers up = ~+1).
    vel_bias_tau: float = 1.1            # accel bias/high-pass time constant (drift control)
    vel_zero_gyro: float = 0.35          # rad/s — below this (with low accel) the limb is "still"
    vel_zero_accel: float = 0.5          # m/s² — still threshold per axis bundle
    vel_zero_dwell_s: float = 0.15       # still must persist this long to hard-zero velocity
    pitch_rate_tau: float = 0.05         # smoothing for the pitch-rate estimate
    # A vigorous flutter/pick whips the wrist; quat->euler pitch can jump a frame
    # and ~30% of that leaks through the smoother — enough to trip a phantom
    # semitone and to spuriously block a scale step. Discard frames whose implied
    # tilt rate exceeds anything a human plays (a real semitone tilt is <60°/s).
    pitch_rate_spike_deg_s: float = 120.0


# ---------------------------------------------------------------------------
# Selection (which regions)
# ---------------------------------------------------------------------------
@dataclass
class SelectionConfig:
    n_regions: int = 4                   # segments across the pointing plane
    plane_half_deg: float = 120.0      # plane spans [-120, +120] from forward (60°/region @ n=4)

    # Rotate the calibrated forward vector so play regions land in front of you.
    # With the prayer pose both gloves point at forward, so 0° is the usual start.
    forward_offset_deg: float = 0.0

    # Open / pick from per-finger flex (thumb excluded — it reads bent when fully extended).
    finger_open_max: float = 0.50        # below this = finger extended; default pose is open
    pick_steady_gyro_max: float = 3.0    # hand must be this still to latch a pick

    # ALL mode: both hands open and turned palms-up ("upside down"). Detected from
    # ROLL (stable) rather than pitch: palm-down ≈ 0°, palm-up ≈ ±180°. Hysteresis
    # so it enters firmly and exits the instant you roll back palm-down.
    # On this hardware (mount-corrected body frame) a level PALM-DOWN hand reads
    # |roll| ≈ 180°, and turning the palm UP reads |roll| ≈ 0°. So "palms up" =
    # SMALL |roll|. Enter ALL when both hands are below the enter threshold; exit
    # when either rolls back past the (larger) exit threshold — hysteresis.
    all_palm_up_roll_deg: float = 55.0        # enter ALL when both hands' |roll| < this (palms up)
    all_exit_roll_deg: float = 90.0           # exit when either hand's |roll| rises above this

    # "timbre pose": palms toward chest, thumbs out, FINGERS POINTING UP. Detected
    # from the world-up component of the pointing axis (stable even at the vertical
    # where euler pitch hits gimbal lock). Both hands open. Wiggle both to fire.
    timbre_fingers_up_min: float = 0.42  # forward_up above this = fingers pointing up

    # Calibration (v3, point-forward): each hand is calibrated INDEPENDENTLY by
    # pointing the arm straight forward, hand roughly level, fingers open, held
    # still and briefly. Each hand stores its OWN forward heading, so the two
    # hands agree by construction and independent magnetometer/gyro offsets
    # cancel. Heading comes from the magnetometer-free game rotation vector.
    calib_level_pitch_tol_deg: float = 30.0  # arm near-horizontal (heading axis well-conditioned)
    calib_steady_gyro_max: float = 0.8       # rad/s — must be essentially still
    calib_max_world_accel: float = 1.2       # m/s² — no translation during hold
    calib_min_game_status: int = 2           # require this game-RV fusion accuracy (0..3)

    # Single deliberate TWO-HAND trigger (both hands at once; one alone never
    # fires) so calibration never goes off from just being steady:
    #   1. PRE-STILL : BOTH open + still + level for calib_pre_still_s -> the
    #                  forward headings are captured here, from the clean pose
    #   2. FLUTTER   : pick/open each hand calib_knock_cycles× (loose timing,
    #                  motion ignored) -> commit when BOTH hands are done
    calib_pre_still_s: float = 1.0           # deliberate quiet hold before the flutter
    calib_knock_cycles: int = 3              # pick/open flutters per hand to confirm
    calib_knock_total_s: float = 6.0         # whole two-hand flutter must finish within this
    # Hysteretic pick detector on `openness` (robust: ~1.0 open, dips low on a
    # pick — unlike raw flex, which can sit mid-range on a relaxed-open hand):
    calib_pick_below: float = 0.70           # openness below this = a pick (closed enough)
    calib_open_above: float = 0.85           # ... back above this = re-opened (hysteresis)

    # Don't let calibration arm while you're actively playing: any musical event
    # suppresses calibration detection for this long (a clean cal pose held from
    # rest still fires immediately, since nothing has fired recently).
    calib_play_lockout_s: float = 2.0

    # HUD: warn when the absolute heading-accuracy estimate exceeds this (radians).
    heading_accuracy_warn_rad: float = 0.35  # ~20°


# ---------------------------------------------------------------------------
# Gesture detectors (actions)
# ---------------------------------------------------------------------------
@dataclass
class GestureConfig:
    # No global per-arm refractory: the segment steppers self-arbitrate, so a fast
    # raise→lower fires +1 then −1 (and pitch can never silently desync).

    # --- Scale step: world-vertical TRANSLATION, segmented by rest/reversal,
    #     quantized by distance. Velocity = integrated world-vertical accel. ---
    stroke_move_vel: float = 0.14        # m/s to begin a motion segment
    stroke_stop_vel: float = 0.05        # m/s below which motion is "resting"
    stroke_stop_time_s: float = 0.07     # rest must persist this long to end a segment
    stroke_reversal_vel: float = 0.11    # m/s in the opposite direction ends + flips a segment
    stroke_dist_per_step: float = 0.17   # m of vertical travel per scale step
    stroke_min_dist: float = 0.12        # m minimum to count (rejects small/jittery moves)
    stroke_max_steps: int = 2            # cap per segment (a grand gesture jumps 2)
    stroke_block_gyro: float = 3.0       # suppress while the wrist rotates a lot (circles)
    stroke_block_horiz_accel: float = 4.0  # suppress while sweeping sideways
    stroke_block_pitch_rate: float = 55.0  # deg/s — that's a tilt (semitone), not a translation

    # --- Semitone: pitch TILT, same segmentation but on the pitch angle directly ---
    semitone_tilt_deg: float = 30.0      # degrees of tilt per semitone (also the step quantum)
    semitone_move_rate: float = 22.0     # deg/s to begin a tilt segment
    semitone_stop_rate: float = 7.0      # deg/s below which the tilt is resting
    semitone_stop_time_s: float = 0.07
    semitone_reversal_rate: float = 18.0 # deg/s opposite to end + flip a segment
    semitone_min_deg: float = 18.0       # minimum tilt to count
    semitone_max_steps: int = 3
    semitone_block_vert_vel: float = 0.16  # m/s — that's a translation (scale), not a tilt
    semitone_block_gyro: float = 4.0       # rad/s — a flutter/pick whips faster than a tilt
    # Block semitone only in the fingers-UP zone (same axis as timbre pose). Do NOT
    # use abs(forward_up): a normal forward point reads strongly negative on `up`
    # and was incorrectly blocking every chromatic attempt.
    semitone_block_fingers_up: float = 0.50

    # Pitch is point-based: aim at a region and raise/lower (or tilt) — no finger
    # "pick" needed. Dwell once on a region, then the region is *latched* so the
    # heading drift from raising the arm doesn't reset the steppers mid-gesture.
    pitch_arm_dwell_s: float = 0.12
    # Finger flutter only blocks pitch when the arm is NOT already moving in that
    # modality (dominant-modality arbitration — sf noise during a raise is ignored).
    pitch_flutter_max_vert_vel: float = 0.12   # m/s — above this, sf doesn't block scale
    pitch_flutter_max_pitch_rate: float = 18.0 # deg/s — above this, sf doesn't block semitone

    # --- Louder/softer: circular hand waving -> accumulated circulation ---
    circle_min_accel: float = 2.5        # ignore tiny wobble below this magnitude
    circle_radians_per_step: float = 4.5 # circulation accumulated per "louder" tick
    circle_volume_delta: float = 0.08    # level change per tick
    circle_consistency: float = 0.6      # fraction of recent samples that must agree on direction

    # --- Twinkle (spawn / timbre): multi-finger random motion while still ---
    wiggle_fire_above: float = 0.16      # timbre twinkle energy to fire
    timbre_wiggle_fire_above: float = 0.10 # lower bar once both hands are in timbre pose
    wiggle_min_hold_s: float = 0.10
    wiggle_reset_below: float = 0.08
    wiggle_refractory_s: float = 0.5
    spawn_fire_above: float = 0.48       # intentional flutter ~0.8+; noise/pick sits ~0.05–0.40
    spawn_min_hold_s: float = 0.12       # ... sustained this long (a single pick won't hold)
    spawn_reset_below: float = 0.20
    spawn_refractory_s: float = 0.40
    spawn_max_vert_vel: float = 0.25       # arm must not be sweeping vertically at fire time
    spawn_point_dwell_s: float = 0.15    # point at a region & hold roughly steady this long
    spawn_dwell_gyro_max: float = 2.5    # rad/s — arm can wiggle fingers after this settle
    # When THIS hand is picked, only a strong deliberate flutter while the arm is
    # still counts as spawn (note-grab motion also closes fingers and spikes sf).
    spawn_pick_min_sf: float = 0.78
    spawn_pick_max_vert_vel: float = 0.10
    spawn_pick_max_gyro: float = 2.0
    spawn_other_still_gyro: float = 1.2  # legacy; dwell only (other-hand guard uses spawn_flutter now)

    # --- ALL-mode collective scale/chromatic: mean of both hands, in sync ---
    all_stroke_dist_per_step: float = 0.16
    all_stroke_min_dist: float = 0.11
    all_stroke_max_steps: int = 2
    # Ignore the violent palms-up FLIP into the pose: require both wrists below
    # this gyro for all_settle_s before any collective gesture, holding the
    # steppers in reset meanwhile (the flip integrates into a huge phantom vel).
    all_settle_gyro: float = 2.0
    all_settle_s: float = 0.3

    # --- ALL-mode collective volume: one hand twinkles, the other's height = fader ---
    all_volume_vel_gain: float = 0.06    # (m/s of the controlling hand) -> level delta per tick
    all_volume_delta: float = 0.05       # clamp per tick


# ---------------------------------------------------------------------------
# Music model (the choir)
# ---------------------------------------------------------------------------
@dataclass
class MusicConfig:
    root_midi: int = 48                  # C3 — a comfortable choral floor
    # Scale as semitone offsets from the root, one octave. Major by default.
    scale: tuple[int, ...] = (0, 2, 4, 5, 7, 9, 11)
    spawn_degree: int = 0                # new notes spawn on the root degree
    min_midi: int = 24
    max_midi: int = 84
    max_voices_per_region: int = 1       # one note per region; spawn toggles mute
    vowels: tuple[str, ...] = ("aaa", "ooo", "mmm")
    default_level: float = 0.6
    sustain_bump: float = 0.15           # one-shot level gain on "sustain"


# ---------------------------------------------------------------------------
# Preview (local GUI + sine synth)
# ---------------------------------------------------------------------------
@dataclass
class PreviewConfig:
    sample_rate: int = 48000
    block_size: int = 256
    ui_refresh_hz: float = 30.0
    master_gain: float = 0.25
    amp_ramp_s: float = 0.005              # de-click on mute/unmute


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
@dataclass
class OscConfig:
    host: str = "127.0.0.1"
    port: int = 7400                     # matches `udpreceive 7400` in Max
    # Continuous expression stream (the extension point for messy-analog mapping).
    continuous_enabled: bool = True
    continuous_rate_hz: float = 30.0


# ---------------------------------------------------------------------------
# Top-level aggregate
# ---------------------------------------------------------------------------
@dataclass
class Config:
    ble: BleConfig = field(default_factory=BleConfig)
    packet: PacketConfig = field(default_factory=PacketConfig)
    process: ProcessConfig = field(default_factory=ProcessConfig)
    conditioning: ConditioningConfig = field(default_factory=ConditioningConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    gesture: GestureConfig = field(default_factory=GestureConfig)
    music: MusicConfig = field(default_factory=MusicConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    osc: OscConfig = field(default_factory=OscConfig)
