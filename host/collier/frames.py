"""The raw decoded packet, kept free of any transport/library dependency."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RawFrame:
    """One decoded packet from one glove."""

    glove_id: int
    side: str               # "L" or "R"
    seq: int
    ts_ms: int
    quat: tuple[float, float, float, float]   # PRIMARY w,x,y,z (game RV: gyro+accel, no mag)
    accel: tuple[float, float, float]         # linear accel, gravity removed (m/s^2)
    gyro: tuple[float, float, float]          # rad/s
    flex: tuple[int, ...]                     # raw ADC 0..4095
    recv_t: float                             # monotonic receive time
    # v3 fields (default so v2-decoded frames still construct):
    abs_quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)  # stabilized RV (mag, absolute)
    rv_accuracy: float = 0.0                   # est. heading accuracy of abs_quat (radians)
    rv_status: int = 0                         # stabilized RV fusion accuracy 0..3
    game_status: int = 3                       # game RV fusion accuracy 0..3
