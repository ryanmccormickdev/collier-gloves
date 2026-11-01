"""
Sinks: where the choir state goes once it changes.

`SoundSink` is the seam between the instrument's brain and its voice. Swap OSC
(to Max/MSP) for MIDI (to Ableton) or a direct Python synth without touching any
other layer.

OSC schema sent to Max (build the matching `OSC-route` tree once):
    /region/<i>/notes  m1 ...          audible notes ([] when muted)
    /region/<i>/level  <0..1>          section loudness
    /region/<i>/vowel  <idx> <name>    timbre selection
    /region/<i>/pulse  <amount>        one-shot swell (sustain gesture)
    /expr/<name>       <value>         continuous expression stream
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SoundSink(ABC):
    @abstractmethod
    def set_voice(self, region: int, midi: int | None, active: bool) -> None:
        """Primary voice update: one note per region, active = audible."""

    @abstractmethod
    def set_notes(self, region: int, notes: list[int]) -> None:
        """Legacy list form for Max/OSC ([] = muted or idle)."""

    @abstractmethod
    def set_level(self, region: int, level: float) -> None: ...

    @abstractmethod
    def set_vowel(self, region: int, index: int, name: str) -> None: ...

    @abstractmethod
    def pulse(self, region: int, amount: float) -> None: ...

    @abstractmethod
    def continuous(self, name: str, value: float) -> None: ...


class MultiSink(SoundSink):
    """Fan out to several sinks at once (e.g. OSC + console)."""

    def __init__(self, sinks: list[SoundSink]):
        self.sinks = sinks

    def set_voice(self, region, midi, active):
        for s in self.sinks:
            s.set_voice(region, midi, active)

    def set_notes(self, region, notes):
        for s in self.sinks:
            s.set_notes(region, notes)

    def set_level(self, region, level):
        for s in self.sinks:
            s.set_level(region, level)

    def set_vowel(self, region, index, name):
        for s in self.sinks:
            s.set_vowel(region, index, name)

    def pulse(self, region, amount):
        for s in self.sinks:
            s.pulse(region, amount)

    def continuous(self, name, value):
        for s in self.sinks:
            s.continuous(name, value)


class ConsoleSink(SoundSink):
    """Prints state changes. Great for tuning with no audio backend running."""

    def set_voice(self, region, midi, active):
        state = "active" if active else "muted"
        note = str(midi) if midi is not None else "—"
        print(f"  region {region}: voice -> {note} ({state})")

    def set_notes(self, region, notes):
        print(f"  region {region}: notes -> {notes}")

    def set_level(self, region, level):
        print(f"  region {region}: level -> {level:.2f}")

    def set_vowel(self, region, index, name):
        print(f"  region {region}: vowel -> {name} ({index})")

    def pulse(self, region, amount):
        print(f"  region {region}: PULSE +{amount:.2f}")

    def continuous(self, name, value):
        pass


class OscSink(SoundSink):
    """Sends to Max/MSP (or any OSC receiver) via UDP."""

    def __init__(self, host: str, port: int):
        from pythonosc.udp_client import SimpleUDPClient
        self.client = SimpleUDPClient(host, port)

    def set_voice(self, region, midi, active):
        pass  # set_notes carries audible state for Max

    def set_notes(self, region, notes):
        self.client.send_message(f"/region/{region}/notes", list(notes))

    def set_level(self, region, level):
        self.client.send_message(f"/region/{region}/level", float(level))

    def set_vowel(self, region, index, name):
        self.client.send_message(f"/region/{region}/vowel", [int(index), name])

    def pulse(self, region, amount):
        self.client.send_message(f"/region/{region}/pulse", float(amount))

    def continuous(self, name, value):
        self.client.send_message(f"/expr/{name}", float(value))
