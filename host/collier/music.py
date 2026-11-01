"""
Music model: the choir that your gestures conduct.

`ChoirState` is the single source of truth — n regions, each holding one voice
(MIDI + mute), a level, and a vowel. `ChoirEngine` is the only thing allowed to
mutate it: it consumes GestureEvents, updates the state, and pushes the changed
fields out to whatever sinks are attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import MusicConfig
from .gestures import (GestureEvent, PitchStep, SpawnNote, SustainPulse,
                       TimbreCycle, VolumeChange)
from .sinks import SoundSink


# ---------------------------------------------------------------------------
# Theory
# ---------------------------------------------------------------------------
class Scale:
    """Maps scale degrees <-> MIDI and steps notes by degree or by semitone."""

    def __init__(self, cfg: MusicConfig):
        self.root = cfg.root_midi
        self.intervals = cfg.scale
        self.lo, self.hi = cfg.min_midi, cfg.max_midi

    def degree_to_midi(self, degree: int) -> int:
        n = len(self.intervals)
        octave, idx = divmod(degree, n)
        return self._clamp(self.root + 12 * octave + self.intervals[idx])

    def step_diatonic(self, midi: int, steps: int) -> int:
        """Move `steps` scale degrees from the nearest in-scale note."""
        deg = self._nearest_degree(midi)
        return self.degree_to_midi(deg + steps)

    def step_chromatic(self, midi: int, steps: int) -> int:
        return self._clamp(midi + steps)

    def _nearest_degree(self, midi: int) -> int:
        best_d, best_err = 0, 1e9
        for d in range(-len(self.intervals) * 4, len(self.intervals) * 4):
            err = abs(self.degree_to_midi(d) - midi)
            if err < best_err:
                best_err, best_d = err, d
        return best_d

    def _clamp(self, m: int) -> int:
        return max(self.lo, min(self.hi, m))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@dataclass
class Region:
    index: int
    midi: int | None = None          # pitch stored even when muted
    active: bool = False             # audible when True
    level: float = 0.6
    vowel: int = 0                   # index into MusicConfig.vowels


@dataclass
class ChoirState:
    regions: list[Region]

    @classmethod
    def make(cls, cfg: MusicConfig, n_regions: int) -> "ChoirState":
        return cls(regions=[Region(index=i, level=cfg.default_level)
                            for i in range(n_regions)])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ChoirEngine:
    def __init__(self, cfg: MusicConfig, n_regions: int, sinks: list[SoundSink]):
        self.cfg = cfg
        self.scale = Scale(cfg)
        self.state = ChoirState.make(cfg, n_regions)
        self.sinks = sinks
        self._emit_all()

    # -- public ------------------------------------------------------------
    def apply(self, event: GestureEvent) -> None:
        if isinstance(event, SpawnNote):
            self._spawn(event)
        elif isinstance(event, PitchStep):
            self._pitch(event)
        elif isinstance(event, VolumeChange):
            self._volume(event)
        elif isinstance(event, TimbreCycle):
            self._timbre(event)
        elif isinstance(event, SustainPulse):
            self._sustain(event)

    def continuous(self, name: str, value: float) -> None:
        for s in self.sinks:
            s.continuous(name, value)

    # -- handlers ----------------------------------------------------------
    def _spawn(self, e: SpawnNote) -> None:
        root = self.scale.degree_to_midi(self.cfg.spawn_degree)
        for r in self._regions(e):
            if r.midi is None:
                r.midi = root
                r.active = True
            else:
                r.active = not r.active
            self._emit_voice(r)

    def _pitch(self, e: PitchStep) -> None:
        for r in self._regions(e):
            if r.midi is None:
                continue
            if e.chromatic:
                r.midi = self.scale.step_chromatic(r.midi, e.steps)
            else:
                r.midi = self.scale.step_diatonic(r.midi, e.steps)
            self._emit_voice(r)

    def _volume(self, e: VolumeChange) -> None:
        for r in self._regions(e):
            r.level = max(0.0, min(1.0, r.level + e.delta))
            self._emit_level(r)

    def _timbre(self, e: TimbreCycle) -> None:
        n = len(self.cfg.vowels)
        for r in self._regions(e):
            r.vowel = (r.vowel + 1) % n
            self._emit_vowel(r)

    def _sustain(self, e: SustainPulse) -> None:
        for r in self._regions(e):
            for s in self.sinks:
                s.pulse(r.index, self.cfg.sustain_bump)

    # -- emit --------------------------------------------------------------
    def _regions(self, e: GestureEvent):
        return [self.state.regions[i] for i in e.regions
                if 0 <= i < len(self.state.regions)]

    def _emit_all(self) -> None:
        for r in self.state.regions:
            self._emit_voice(r)
            self._emit_level(r)
            self._emit_vowel(r)

    def _emit_voice(self, r: Region) -> None:
        for s in self.sinks:
            s.set_voice(r.index, r.midi, r.active)
            # Legacy OSC path: notes list mirrors active state.
            s.set_notes(r.index, [r.midi] if r.midi is not None and r.active else [])

    def _emit_level(self, r: Region) -> None:
        for s in self.sinks:
            s.set_level(r.index, r.level)

    def _emit_vowel(self, r: Region) -> None:
        for s in self.sinks:
            s.set_vowel(r.index, r.vowel, self.cfg.vowels[r.vowel])
