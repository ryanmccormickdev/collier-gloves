"""
Transport layer: BLE reception + packet decoding.

One async task per glove scans, connects, streams notifications, and reconnects
forever. Each notification is decoded into a `RawFrame` and handed to a callback.
Decodes the firmware v3 packet (`PacketConfig.fmt`, 86 bytes). Packets of any
other size are dropped by the length check in `_Decoder.decode`, so v2 firmware
(64 bytes) does not work here; the v2 defaults below only apply if
`PacketConfig.fmt` is changed back to the v2 layout.

The rest of the pipeline never sees bytes or bleak — only RawFrame objects.
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

from .config import BleConfig, PacketConfig
from .frames import RawFrame

FrameCallback = Callable[[RawFrame], None]


class _Decoder:
    def __init__(self, pkt: PacketConfig):
        self.pkt = pkt

    def decode(self, side: str, data: bytes) -> Optional[RawFrame]:
        if len(data) != self.pkt.size:
            return None
        try:
            f = struct.unpack(self.pkt.fmt, data)
        except struct.error:
            return None
        n = self.pkt.flex_count
        flex_end = 13 + n
        # v3 appends [abs_quat(4f), rv_accuracy(f), rv_status(B), game_status(B)]
        # after the flex array. Fall back to v2 defaults if absent.
        has_v3 = len(f) >= flex_end + 6
        if has_v3:
            abs_quat = (f[flex_end], f[flex_end + 1], f[flex_end + 2], f[flex_end + 3])
            rv_accuracy = float(f[flex_end + 4])
            rv_status = int(f[flex_end + 5])
            game_status = int(f[flex_end + 6])
        else:
            abs_quat = (1.0, 0.0, 0.0, 0.0)
            rv_accuracy, rv_status, game_status = 0.0, 0, 3
        return RawFrame(
            glove_id=f[0],
            side=side,
            seq=f[1],
            ts_ms=f[2],
            quat=(f[3], f[4], f[5], f[6]),
            accel=(f[7], f[8], f[9]),
            gyro=(f[10], f[11], f[12]),
            flex=tuple(f[13:flex_end]),
            recv_t=time.monotonic(),
            abs_quat=abs_quat,
            rv_accuracy=rv_accuracy,
            rv_status=rv_status,
            game_status=game_status,
        )


class GloveTransport:
    """Manages BLE for all configured gloves and emits RawFrames via callback."""

    def __init__(self, ble: BleConfig, pkt: PacketConfig, on_frame: FrameCallback):
        self.ble = ble
        self.decoder = _Decoder(pkt)
        self.on_frame = on_frame
        # name -> side label, by firmware convention (id 0 = L, id 1 = R)
        self._side_of = {
            name: ("L" if i == 0 else "R") for i, name in enumerate(ble.glove_names)
        }

    async def run(self) -> None:
        await asyncio.gather(*(self._maintain(name) for name in self.ble.glove_names))

    async def _maintain(self, name: str) -> None:
        side = self._side_of[name]

        def handler(_sender, data: bytearray) -> None:
            frame = self.decoder.decode(side, bytes(data))
            if frame is not None:
                self.on_frame(frame)

        while True:
            try:
                device = await BleakScanner.find_device_by_name(
                    name, timeout=self.ble.scan_timeout_s
                )
                if device is None:
                    continue  # not powered on yet; keep scanning quietly
                async with BleakClient(device) as client:
                    await client.start_notify(self.ble.char_uuid, handler)
                    while client.is_connected:
                        await asyncio.sleep(0.2)
            except Exception:
                # Any BLE hiccup -> wait briefly and re-scan. Never crash the show.
                await asyncio.sleep(0.5)
