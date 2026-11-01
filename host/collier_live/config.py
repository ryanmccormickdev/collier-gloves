"""Configuration for collier_live."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .filter_pose import FilterPoseConfig
from .pick_engine import PickEngineConfig


@dataclass
class PostprocessConfig:
    """Postprocess defaults are tuned (June 2026) against the per-clip simulation
    in ``experiments/ml-attempt/apps/diag_offline_predict.py``: at the previous values 20-35% of every
    gesture class double-fired on the training data itself, because the natural
    release/return after any gesture sweeps through neighbouring classes."""

    # Consecutive frames with same label before fire. ~80 ms @60 Hz.
    agreement_k: int = 5
    agreement_n: int = 4  # unused; kept for config compat

    # Per-class cooldown. Has to be longer than one gesture episode (~1s) so the
    # "release" half of the gesture can't refire the same class.
    per_class_refractory_s: float = 1.0

    # Cross-class cooldown after any fire. This is the most important knob —
    # was 0.15s, but the release/return motion lasts ~0.5-0.8s and was firing
    # the *opposite* gesture class.
    global_cooldown_s: float = 0.8

    # Per-label bonus to the manifest's class_threshold. Chromatic classes are
    # the least separable; require them to clear a higher bar.
    confidence_bump: dict[str, float] = field(
        default_factory=lambda: {"semitone_up": 0.10, "semitone_down": 0.10}
    )

    # Hysteresis. Episode starts when conf >= threshold + start_margin; episode
    # ends (label allowed to refire) only after conf drops below
    # threshold - end_margin OR a 'none' tick. Both gate the start state.
    start_margin: float = 0.05
    end_margin: float = 0.10

    region_lookback: int = 20
    collective_requires_both: bool = True


@dataclass
class OscConfig:
    host: str = "127.0.0.1"
    port: int = 7400
    filter_rate_hz: float = 30.0


@dataclass
class LiveConfig:
    manifest_path: Path | None = None
    active_json: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "models" / "active.json"
    )
    n_regions: int = 4
    flex_min_span: float = 80.0
    flex_window_s: float = 60.0
    infer_refractory_s: float = 0.4
    # Pause gesture recognition this long after the first live calibration
    # ceremony completes (pre-loaded forward headings skip the delay).
    gesture_arm_delay_s: float = 2.0
    root_midi: int = 60
    scale_intervals: tuple[int, ...] = (0, 2, 4, 5, 7, 9, 11)
    min_midi: int = 31   # G1
    max_midi: int = 79   # G5
    debug: bool = False
    # Engine selection: "pick" = heuristic pick-state-machine (default, no
    # calibration drift sensitivity); "ml" = the old TCN inference path
    # (kept as a fallback while we settle in).
    engine: str = "pick"
    post: PostprocessConfig = field(default_factory=PostprocessConfig)
    pick_engine: PickEngineConfig = field(default_factory=PickEngineConfig)
    filter_pose: FilterPoseConfig = field(default_factory=FilterPoseConfig)
    osc: OscConfig = field(default_factory=OscConfig)

    def resolve_manifest(self) -> Path:
        if self.manifest_path is not None:
            return Path(self.manifest_path)
        if self.active_json.exists():
            import json

            data = json.loads(self.active_json.read_text(encoding="utf-8"))
            p = data.get("manifest_path")
            if p:
                path = Path(p)
                if not path.is_absolute():
                    root = Path(__file__).resolve().parents[1]
                    path = root / path
                return path
        raise FileNotFoundError(
            "No manifest: pass --manifest or set collier_live/models/active.json"
        )
