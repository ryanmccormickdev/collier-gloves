"""
Glove packet monitor - CLI sanity check for one or two BLE gloves.

Decodes the firmware v3 packet (86 bytes, 10 raw flex channels). v3 keeps the
v2 fields at the same offsets, so only the format string changed from the v2
version of this script (which silently ignored 86-byte packets).

Install:
    pip install bleak
Run:
    python tools/packet_monitor.py

This is intentionally bare-bones; for the full live pipeline with
calibration, run `python -m collier_live` from host/.
"""

import asyncio
import struct
import time
from typing import Optional

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "5a3e8f00-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
CHAR_UUID    = "5a3e8f01-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
GLOVE_NAMES  = ["Glove-L", "Glove-R"]
PACKET_FMT   = "<BBH4f3f3f10H4ffBB"   # firmware v3 (was "<BBH4f3f3f10H", 64 bytes, for v2)
PACKET_SIZE  = struct.calcsize(PACKET_FMT)
assert PACKET_SIZE == 86

DISPLAY_HZ = 10


class GloveState:
    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.last_packet: Optional[dict] = None
        self.last_recv_t = 0.0
        self.packet_count = 0
        self.missed = 0
        self.last_seq: Optional[int] = None
        self.rate_hz = 0.0

    def on_packet(self, data: bytes) -> None:
        if len(data) != PACKET_SIZE:
            return
        try:
            f = struct.unpack(PACKET_FMT, data)
        except struct.error:
            return
        pkt = {
            "glove_id": f[0],
            "seq":      f[1],
            "ts_ms":    f[2],
            "quat":     (f[3], f[4], f[5], f[6]),
            "accel":    (f[7], f[8], f[9]),
            "gyro":     (f[10], f[11], f[12]),
            "flex":     f[13:23],          # 10 channels: 8 mux + 2 thumb
        }
        if self.last_seq is not None:
            gap = (pkt["seq"] - self.last_seq) & 0xFF
            if gap > 1: self.missed += gap - 1
        self.last_seq     = pkt["seq"]
        self.last_packet  = pkt
        self.last_recv_t  = time.monotonic()
        self.packet_count += 1


state = {n: GloveState(n) for n in GLOVE_NAMES}


async def maintain_glove(name: str) -> None:
    s = state[name]
    while True:
        try:
            device = await BleakScanner.find_device_by_name(name, timeout=8.0)
            if device is None:
                await asyncio.sleep(1.5)
                continue
            async with BleakClient(device) as client:
                s.connected = True
                s.last_seq  = None
                await client.start_notify(CHAR_UUID, lambda _, d, st=s: st.on_packet(bytes(d)))
                while client.is_connected:
                    await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[{name}] {e!r}")
        finally:
            s.connected = False
            await asyncio.sleep(1.0)


def fmt_glove(s: GloveState) -> str:
    if not s.connected:
        return f"{s.name}  [disconnected]"
    p = s.last_packet
    if p is None:
        return f"{s.name}  [connected, awaiting first packet]"
    qw, qx, qy, qz = p["quat"]
    ax, ay, az    = p["accel"]
    gx, gy, gz    = p["gyro"]
    flex = p["flex"]
    return (
        f"{s.name}   {s.rate_hz:5.1f} Hz   pkts={s.packet_count:<7d}   missed={s.missed:<5d}\n"
        f"   quat   w={qw:+.3f}  x={qx:+.3f}  y={qy:+.3f}  z={qz:+.3f}\n"
        f"   accel  x={ax:+6.2f}  y={ay:+6.2f}  z={az:+6.2f}  m/s^2\n"
        f"   gyro   x={gx:+6.2f}  y={gy:+6.2f}  z={gz:+6.2f}  rad/s\n"
        f"   mux    {' '.join(f'{flex[i]:4d}' for i in range(8))}\n"
        f"   thumb  {flex[8]:4d} {flex[9]:4d}"
    )


async def display_loop() -> None:
    last_t = time.monotonic()
    last_counts = {n: 0 for n in GLOVE_NAMES}
    period = 1.0 / DISPLAY_HZ
    while True:
        await asyncio.sleep(period)
        now = time.monotonic()
        dt  = now - last_t
        for n in GLOVE_NAMES:
            s = state[n]
            s.rate_hz = (s.packet_count - last_counts[n]) / dt if dt > 0 else 0
            last_counts[n] = s.packet_count
        last_t = now

        out = ["\033[2J\033[H", "=== Glove Receiver (Ctrl-C to quit) ===\n"]
        for n in GLOVE_NAMES:
            out.append(fmt_glove(state[n]))
            out.append("")
        print("\n".join(out), flush=True)


async def main() -> None:
    tasks = [asyncio.create_task(maintain_glove(n)) for n in GLOVE_NAMES]
    tasks.append(asyncio.create_task(display_loop()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye.")
