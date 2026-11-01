"""
Orientation math for the BNO085 rotation quaternion (w, x, y, z).

Two jobs:
  1. Turn the quaternion into human angles (yaw / pitch / roll).
  2. Rotate the sensor-frame linear-accel vector into the WORLD frame, so we can
     ask "is the hand accelerating upward?" independent of how the wrist is held.
     This is what lets the scale-step detector work off true vertical motion.

The BNO085 is mounted upside-down on the glove PCB. Other apps in this repo apply
IMU_MOUNT_CORRECTION before reading angles; collier keeps the raw firmware euler
for calibration compatibility and applies a configurable forward offset instead
(see SelectionConfig.forward_offset_deg).
"""

from __future__ import annotations

import math

# Matches glove_cello.py / glove_visualizer.py — 180° fix for PCB mount.
IMU_MOUNT_CORRECTION = (0.0, 0.7071, -0.7071, 0.0)


def q_inv(q: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    w, x, y, z = q
    return (w, -x, -y, -z)


def q_mul(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def apply_mount_correction(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Rotate raw sensor quaternion into the hand/body frame."""
    m = IMU_MOUNT_CORRECTION
    return q_mul(q_mul(m, q), q_inv(m))


def quat_to_euler_deg(qw: float, qx: float, qy: float, qz: float) -> tuple[float, float, float]:
    """
    Returns (yaw, pitch, roll) in degrees.
      yaw   = heading about world-vertical (used for pointing at regions)
      pitch = tilt up/down
      roll  = rotation about the pointing axis
    Standard aerospace ZYX convention.
    """
    # roll (x-axis)
    sinr_cosp = 2.0 * (qw * qx + qy * qz)
    cosr_cosp = 1.0 - 2.0 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # pitch (y-axis), clamped to avoid NaN at the poles
    sinp = 2.0 * (qw * qy - qz * qx)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)

    # yaw (z-axis)
    siny_cosp = 2.0 * (qw * qz + qx * qy)
    cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def rotate_vector_to_world(
    q: tuple[float, float, float, float],
    v: tuple[float, float, float],
    *,
    mount_correct: bool = False,
) -> tuple[float, float, float]:
    """
    Rotate sensor-frame vector v by quaternion q (w,x,y,z) into the world frame.
    Implements  v' = q * v * q_conjugate  (Hamilton product, expanded).
    """
    if mount_correct:
        q = apply_mount_correction(q)
    qw, qx, qy, qz = q
    vx, vy, vz = v

    # t = 2 * cross(q_xyz, v)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)

    # v' = v + qw * t + cross(q_xyz, t)
    rx = vx + qw * tx + (qy * tz - qz * ty)
    ry = vy + qw * ty + (qz * tx - qx * tz)
    rz = vz + qw * tz + (qx * ty - qy * tx)
    return rx, ry, rz


def magnitude(v: tuple[float, float, float]) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def horizontal_heading_deg(
    q: tuple[float, float, float, float],
    body_forward: tuple[float, float, float] = (1.0, 0.0, 0.0),
    *,
    mount_correct: bool = False,
) -> float:
    """
    Magnetometer-referenced heading: project a body-axis into the horizontal
    plane (world XY). Stable when the hand points down — unlike euler yaw at
    gimbal lock, which is what made arms-out calibration jump randomly.
    """
    wx, wy, wz = rotate_vector_to_world(q, body_forward, mount_correct=mount_correct)
    horiz = math.hypot(wx, wy)
    if horiz < 0.05:
        return float("nan")
    return math.degrees(math.atan2(wy, wx))
