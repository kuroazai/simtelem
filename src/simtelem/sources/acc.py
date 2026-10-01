"""Assetto Corsa Competizione telemetry via shared memory.

Uses the `pyaccsharedmemory` package, which maps ACC's three shared-memory pages
(Physics / Graphics / Static). ACC exposes the same shared-memory layout as
Assetto Corsa, so this also works for AC with minor field differences.

NOTE: pyaccsharedmemory field names have shifted slightly across releases, so we
read defensively with getattr() and normalize into our Snapshot. If a field comes
back as 0/empty, check the installed package's field names against `_pull()` below.
"""
from __future__ import annotations

import contextlib
import math
from typing import Any

from ..models import Snapshot
from .base import TelemetrySource


def _g(obj, *names, default=0):
    """Return the first present attribute among `names`, else `default`."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _clean_str(v) -> str:
    """ACC stores names in fixed-length char buffers padded with NUL bytes."""
    return str(v).split("\x00")[0].strip()


def _ms(v) -> int:
    """Coerce a lap-time value to int milliseconds, tolerating odd types."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _session(g) -> str:
    """Normalize ACC's session-type enum to practice|qualify|race|hotlap."""
    st = getattr(g, "session_type", "")
    name = str(getattr(st, "name", st)).upper()
    if "PRACTICE" in name:
        return "practice"
    if "QUALIF" in name:
        return "qualify"
    if "HOTLAP" in name or "SUPERPOLE" in name or "TIME_ATTACK" in name:
        return "hotlap"
    if "RACE" in name or "HOTSTINT" in name:
        return "race"
    return ""


def _brake_bias_pct(v) -> float:
    """ACC reports brake bias as a front fraction (0..1); express as a %."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v * 100.0 if v <= 1.0 else v


def _damage_sum(v) -> float:
    """Total car damage across all zones (front/rear/left/right/centre)."""
    if v is None:
        return 0.0
    for attrs in (("front", "rear", "left", "right", "centre"),
                  ("front", "rear", "left", "right", "center")):
        if hasattr(v, attrs[0]):
            return float(sum(getattr(v, a, 0.0) for a in attrs))
    try:
        return float(sum(v))
    except Exception:
        return 0.0


def _wheels(obj, *names) -> tuple:
    """Read a Wheels-like struct (front_left/right, rear_left/right) into FL,FR,RL,RR."""
    w = _g(obj, *names, default=None)
    if w is None:
        return (0.0, 0.0, 0.0, 0.0)
    # pyaccsharedmemory Wheels dataclass
    if hasattr(w, "front_left"):
        return (
            float(w.front_left),
            float(w.front_right),
            float(w.rear_left),
            float(w.rear_right),
        )
    # some versions expose a plain 4-tuple / list
    try:
        return (float(w[0]), float(w[1]), float(w[2]), float(w[3]))
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


def _enum_name(v, default: str = "") -> str:
    """ACC exposes weather/grip as IntEnums; normalize to a lowercase slug."""
    if v is None:
        return default
    name = str(getattr(v, "name", v))
    # ACC_MEDIUM_RAIN -> medium_rain ; ACC_OPTIMUM -> optimum
    return name.removeprefix("ACC_").lower()


def _vec3(v) -> tuple:
    """Vector3f -> (x, y, z), tolerating tuple-shaped variants."""
    if v is None:
        return (0.0, 0.0, 0.0)
    if hasattr(v, "x"):
        return (float(v.x), float(v.y), float(v.z))
    try:
        return (float(v[0]), float(v[1]), float(v[2]))
    except Exception:
        return (0.0, 0.0, 0.0)


def _pair(v) -> tuple:
    """rideHeight is a 2-element (front, rear) array."""
    if v is None:
        return (0.0, 0.0)
    if hasattr(v, "front"):
        return (float(v.front), float(v.rear))
    try:
        return (float(v[0]), float(v[1]))
    except Exception:
        return (0.0, 0.0)


def _positions(g, p) -> dict[str, Any]:
    """Player world position, heading, and every rival in car-relative space.

    ACC gives world coordinates for all cars in one array. The player's slot is
    found by matching car_id against player_car_id -- it is NOT reliably index
    0. Rivals are then rotated into the player's frame so "12 m back on your
    left" falls out directly:

        lateral < 0  -> left        ahead > 0 -> in front
    """
    # Annotated because the literal is heterogeneous: without this mypy infers
    # dict[str, object] and every read needs a cast.
    out: dict[str, Any] = {"world_pos": (0.0, 0.0, 0.0), "heading": 0.0,
           "active_cars": 0, "others": ()}
    try:
        coords = _g(g, "car_coordinates", default=None)
        ids = _g(g, "car_id", default=None)
        # `or -1` would be a bug here: in single player the player IS car 0,
        # and 0 is falsy, so the fallback would fire on a perfectly valid id.
        me = _g(g, "player_car_id", default=None)
        me = -1 if me is None else int(me)
        n = int(_g(g, "active_cars", default=0) or 0)
        out["active_cars"] = n
        out["heading"] = float(_g(p, "heading", default=0.0) or 0.0)
        if not coords or not ids:
            return out
        idx = next((i for i, cid in enumerate(ids) if int(cid) == me), None)
        if idx is None or idx >= len(coords):
            return out
        px, py, pz = _vec3(coords[idx])
        out["world_pos"] = (px, py, pz)

        h = float(out["heading"])
        cos_h, sin_h = math.cos(-h), math.sin(-h)
        rivals = []
        for i in range(min(n, len(coords), len(ids))):
            if i == idx:
                continue
            x, _y, z = _vec3(coords[i])
            dx, dz = x - px, z - pz
            if dx == 0.0 and dz == 0.0:
                continue          # empty slot
            lateral = dx * cos_h - dz * sin_h
            ahead = dx * sin_h + dz * cos_h
            rivals.append((round(math.hypot(dx, dz), 1),
                           round(lateral, 1), round(ahead, 1)))
        rivals.sort(key=lambda r: r[0])
        out["others"] = tuple(rivals[:12])
    except Exception:
        pass
    return out


def _flag_name(raw: object) -> str:
    """Normalise a flag to a lowercase string.

    ACC hands back an enum on some releases and a bare value on others, so take
    `.name` when it is there and fall back to the value itself.
    """
    return str(getattr(raw, "name", raw)).lower()


class ACCSource(TelemetrySource):
    name = "acc"

    def __init__(self) -> None:
        self._asm = None

    def connect(self) -> None:
        try:
            from pyaccsharedmemory import accSharedMemory
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "pyaccsharedmemory is not installed. Run: pip install pyaccsharedmemory\n"
                "(ACC telemetry is Windows-only.)"
            ) from e
        self._asm = accSharedMemory()

    def read(self) -> Snapshot | None:
        if self._asm is None:
            return None
        sm = self._asm.read_shared_memory()
        if sm is None:
            return None
        return self._pull(sm)

    def close(self) -> None:
        if self._asm is not None:
            with contextlib.suppress(Exception):
                self._asm.close()
            self._asm = None

    # --- mapping --------------------------------------------------------------
    @staticmethod
    def _pull(sm) -> Snapshot:
        p = sm.Physics
        g = sm.Graphics
        s = sm.Static

        # ACC_STATUS: 0=OFF, 1=REPLAY, 2=LIVE, 3=PAUSE. Treat only LIVE as valid.
        status = _g(g, "status", default=2)
        status_val = getattr(status, "value", status)
        valid = status_val == 2 or status_val == "AC_LIVE"

        gx, gy, gz = _vec3(_g(p, "g_force", "accG", default=None))
        rh_f, rh_r = _pair(_g(p, "ride_height", "rideHeight", default=None))

        return Snapshot(
            valid=bool(valid),
            speed_kmh=float(_g(p, "speed_kmh", "speedKmh")),
            rpm=int(_g(p, "rpm", "rpms")),
            max_rpm=int(_g(s, "max_rpm", "maxRpm")),
            gear=int(_g(p, "gear")),
            gas=float(_g(p, "gas")),
            brake=float(_g(p, "brake")),
            clutch=float(_g(p, "clutch")),
            steer=float(_g(p, "steer_angle", "steerAngle", "steer")),
            fuel=float(_g(p, "fuel")),
            tyre_temp=_wheels(p, "tyre_core_temp", "tyreCoreTemperature", "tyre_temp"),
            tyre_pressure=_wheels(p, "wheel_pressure", "wheelsPressure", "tyre_pressure"),
            brake_temp=_wheels(p, "brake_temp", "brakeTemp"),
            tc=float(_g(p, "tc")),
            abs=float(_g(p, "abs")),
            wheel_slip=_wheels(p, "wheel_slip", "wheelSlip"),
            brake_bias=_brake_bias_pct(_g(p, "brake_bias", "brakeBias", default=0.0)),
            car_damage_total=_damage_sum(_g(p, "car_damage", "carDamage", default=None)),

            # chassis / setup diagnostics
            suspension_travel=_wheels(p, "suspension_travel", "suspensionTravel"),
            ride_height_f=rh_f,
            ride_height_r=rh_r,
            slip_angle=_wheels(p, "slip_angle", "slipAngle"),
            wheel_load=_wheels(p, "wheel_load", "wheelLoad"),
            brake_pressure=_wheels(p, "brake_pressure", "brakePressure"),
            pad_life=_wheels(p, "pad_life", "padLife"),
            g_lat=gx, g_vert=gy, g_lon=gz,
            pitch=float(_g(p, "pitch", default=0.0) or 0.0),
            roll=float(_g(p, "roll", default=0.0) or 0.0),

            # weather / track state
            air_temp=float(_g(p, "air_temp", "airTemp", default=0.0) or 0.0),
            road_temp=float(_g(p, "road_temp", "roadTemp", default=0.0) or 0.0),
            wind_speed=float(_g(g, "wind_speed", "windSpeed", default=0.0) or 0.0),
            track_grip=_enum_name(_g(g, "track_grip_status", "trackGripStatus",
                                     default=None)),
            rain_intensity=_enum_name(_g(g, "rain_intensity", "rainIntensity",
                                         default=None)),
            rain_in_10min=_enum_name(_g(g, "rain_intensity_in_10min",
                                        "rainIntensityIn10min", default=None)),
            rain_in_30min=_enum_name(_g(g, "rain_intensity_in_30min",
                                        "rainIntensityIn30min", default=None)),
            rain_tyres=bool(_g(g, "rain_tyres", "rainTyres", default=False)),

            tc_level=int(_g(g, "tc_level", "tcLevel", default=0) or 0),
            abs_level=int(_g(g, "abs_level", "absLevel", default=0) or 0),
            session_type=_session(g),
            gap_ahead_ms=_ms(_g(g, "gap_ahead", "gapAhead", default=0)),
            gap_behind_ms=_ms(_g(g, "gap_behind", "gapBehind", default=0)),
            valid_lap=bool(_g(g, "is_valid_lap", "isValidLap", default=1)),
            completed_laps=int(_g(g, "completed_lap", "completedLaps", "completed_laps")),
            position=int(_g(g, "position")),
            current_time_ms=_ms(_g(g, "current_time", "i_current_time", "current_time_ms")),
            last_time_ms=_ms(_g(g, "last_time", "i_last_time", "last_time_ms")),
            best_time_ms=_ms(_g(g, "best_time", "i_best_time", "best_time_ms")),
            delta_ms=_ms(_g(g, "delta_lap_time", "i_delta_lap_time", default=0)),
            fuel_per_lap=float(_g(g, "fuel_x_lap", "fuelXLap", "fuel_per_lap")),
            session_time_left_ms=int(_g(g, "session_time_left", "sessionTimeLeft", default=0)),
            is_in_pit=bool(_g(g, "is_in_pit", "isInPit")),
            is_in_pit_lane=bool(_g(g, "is_in_pit_lane", "isInPitLane")),
            flag=_flag_name(_g(g, "flag", default="none")),
            tyre_compound=_clean_str(_g(g, "tyre_compound", "tyreCompound", default="")),
            normalized_car_position=float(
                _g(g, "normalized_car_position", "normalizedCarPosition")
            ),
            **_positions(g, p),
            car_model=_clean_str(_g(s, "car_model", "carModel", default="")),
            track=_clean_str(_g(s, "track", default="")),
            max_fuel=float(_g(s, "max_fuel", "maxFuel")),
        )
