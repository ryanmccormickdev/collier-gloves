"""Quaternion / hand-frame math (trainer v3 parity)."""
from __future__ import annotations

import math

IMU_MOUNT_CORRECTION = (0.0, 0.7071, -0.7071, 0.0)


def _q_inv(q):
    w, x, y, z = q
    return (w, -x, -y, -z)


def _q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def apply_mount_correction(q):
    m = IMU_MOUNT_CORRECTION
    return _q_mul(_q_mul(m, q), _q_inv(m))


def quat_to_euler_deg(qw, qx, qy, qz):
    sinr_cosp = 2.0 * (qw * qx + qy * qz)
    cosr_cosp = 1.0 - 2.0 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    sinp = max(-1.0, min(1.0, 2.0 * (qw * qy - qz * qx)))
    pitch = math.asin(sinp)
    siny_cosp = 2.0 * (qw * qz + qx * qy)
    cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def rotate_vector_to_world(q, v, *, mount_correct: bool = False):
    if mount_correct:
        q = apply_mount_correction(q)
    qw, qx, qy, qz = q
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    rx = vx + qw * tx + (qy * tz - qz * ty)
    ry = vy + qw * ty + (qz * tx - qx * tz)
    rz = vz + qw * tz + (qx * ty - qy * tx)
    return rx, ry, rz


def rotate_vector_to_body(q, v, *, mount_correct: bool = False):
    if mount_correct:
        q = apply_mount_correction(q)
    return rotate_vector_to_world(_q_inv(q), v, mount_correct=False)


def body_basis_vectors(body_quat: tuple[float, float, float, float]):
    along = rotate_vector_to_world(body_quat, (1.0, 0.0, 0.0), mount_correct=False)
    across = rotate_vector_to_world(body_quat, (0.0, 1.0, 0.0), mount_correct=False)
    palm = rotate_vector_to_world(body_quat, (0.0, 0.0, 1.0), mount_correct=False)
    return along, across, palm


def magnitude(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def horizontal_heading_deg(q, body_forward=(1.0, 0.0, 0.0)):
    wx, wy, _wz = rotate_vector_to_world(q, body_forward, mount_correct=False)
    if math.hypot(wx, wy) < 0.05:
        return float("nan")
    return math.degrees(math.atan2(wy, wx))
