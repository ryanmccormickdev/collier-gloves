"""Region MIDI/spawn state and C-major / chromatic pitch rules."""
from __future__ import annotations

from dataclasses import dataclass, field

from collier.config import MusicConfig
from collier.music import Scale

from .commands import GestureCommand


@dataclass
class RegionState:
    midi: int | None = None
    spawn: bool = False


@dataclass
class RegionStateEngine:
    n_regions: int
    root_midi: int
    scale: Scale
    regions: dict[int, RegionState] = field(default_factory=dict)
    _last_notes: dict[int, int | None] = field(default_factory=dict)
    _last_spawn: dict[int, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for i in range(self.n_regions):
            if i not in self.regions:
                self.regions[i] = RegionState()
            self._last_notes[i] = None
            self._last_spawn[i] = False

    @classmethod
    def from_live(
        cls,
        n_regions: int,
        root_midi: int,
        intervals: tuple[int, ...],
        min_midi: int,
        max_midi: int,
    ) -> RegionStateEngine:
        cfg = MusicConfig(
            root_midi=root_midi,
            scale=intervals,
            min_midi=min_midi,
            max_midi=max_midi,
        )
        return cls(n_regions=n_regions, root_midi=root_midi, scale=Scale(cfg))

    def apply(self, cmd: GestureCommand) -> list[tuple[str, int, object]]:
        """Apply command; return OSC deltas [(kind, region, value), ...]."""
        out: list[tuple[str, int, object]] = []
        if cmd.kind == "spawn":
            for r in cmd.regions:
                out.extend(self._toggle_spawn(r))
        elif cmd.kind == "scale":
            for r in cmd.regions:
                out.extend(self._step_pitch(r, cmd.steps, chromatic=False))
        elif cmd.kind == "chromatic":
            for r in cmd.regions:
                out.extend(self._step_pitch(r, cmd.steps, chromatic=True))
        elif cmd.kind == "volume":
            pass  # no-op until retrain without volume; OSC volume not in schema
        return out

    def _toggle_spawn(self, region: int) -> list[tuple[str, int, object]]:
        st = self.regions[region]
        deltas: list[tuple[str, int, object]] = []
        if st.midi is None:
            st.midi = self.root_midi
            st.spawn = True
        else:
            st.spawn = not st.spawn
        deltas.append(("spawn", region, 1 if st.spawn else 0))
        deltas.append(("notes", region, st.midi if st.spawn else []))
        return deltas

    def _step_pitch(
        self, region: int, steps: int, *, chromatic: bool
    ) -> list[tuple[str, int, object]]:
        st = self.regions[region]
        if st.midi is None:
            return []
        if chromatic:
            st.midi = self.scale.step_chromatic(st.midi, steps)
        else:
            st.midi = self.scale.step_diatonic(st.midi, steps)
        if not st.spawn:
            return []
        return [("notes", region, st.midi)]

    def osc_snapshot(self) -> dict[str, dict[int, object]]:
        """Current values for all regions."""
        notes = {}
        spawn = {}
        for i, st in self.regions.items():
            notes[i] = st.midi if st.spawn and st.midi is not None else []
            spawn[i] = 1 if st.spawn else 0
        return {"notes": notes, "spawn": spawn}

    def drain_changes(self, deltas: list[tuple[str, int, object]]) -> list[tuple[str, int, object]]:
        """Filter to change-only OSC payloads."""
        changed: list[tuple[str, int, object]] = []
        for kind, region, value in deltas:
            if kind == "notes":
                prev = self._last_notes.get(region)
                cur = value if value != [] else None
                if prev == cur:
                    continue
                self._last_notes[region] = cur
                changed.append((kind, region, value))
            elif kind == "spawn":
                prev = self._last_spawn.get(region, False)
                cur = bool(value)
                if prev == cur:
                    continue
                self._last_spawn[region] = cur
                changed.append((kind, region, value))
        return changed
