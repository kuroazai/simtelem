"""The on-disk frame schema, declared once.

A recording is only useful if you can read it back. Writer and reader therefore
share this single list of field specs rather than each hard-coding compact keys
and hoping they agree — a mismatch here silently corrupts every stint on disk,
and that class of bug does not announce itself.

Each spec knows how to pack a value out of a `Snapshot` and unpack it back into
Snapshot constructor kwargs. Awkward fields (world position dropping Y, ride
heights living in two attributes, the G triple) are handled by their own
pack/unpack pair rather than by special cases in the writer.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..models import Snapshot


@dataclass(frozen=True)
class FieldSpec:
    key: str  # compact key as written to disk
    pack: Callable[[Snapshot], Any]
    unpack: Callable[[Any, dict[str, Any]], None]
    note: str = ""


def _scalar(key: str, attr: str, ndigits: int | None = None, note: str = "") -> FieldSpec:
    def pack(s: Snapshot) -> Any:
        v = getattr(s, attr)
        return round(v, ndigits) if ndigits is not None else v

    def unpack(v: Any, kw: dict[str, Any]) -> None:
        kw[attr] = v

    return FieldSpec(key, pack, unpack, note)


def _corners(key: str, attr: str, ndigits: int | None = None, note: str = "") -> FieldSpec:
    """A four-element FL, FR, RL, RR tuple."""

    def pack(s: Snapshot) -> Any:
        vals = getattr(s, attr)
        return [round(v, ndigits) if ndigits is not None else v for v in vals]

    def unpack(v: Any, kw: dict[str, Any]) -> None:
        kw[attr] = tuple(v)

    return FieldSpec(key, pack, unpack, note)


def _world_xz(s: Snapshot) -> Any:
    return [round(s.world_pos[0], 2), round(s.world_pos[2], 2)]


def _world_xz_unpack(v: Any, kw: dict[str, Any]) -> None:
    # Y is deliberately dropped: elevation adds bytes and answers no question
    # about racing line. Restored as 0.0.
    kw["world_pos"] = (v[0], 0.0, v[1])


def _ride_heights(s: Snapshot) -> Any:
    return [round(s.ride_height_f, 4), round(s.ride_height_r, 4)]


def _ride_heights_unpack(v: Any, kw: dict[str, Any]) -> None:
    kw["ride_height_f"], kw["ride_height_r"] = v[0], v[1]


def _g_triple(s: Snapshot) -> Any:
    return [round(s.g_lat, 2), round(s.g_vert, 2), round(s.g_lon, 2)]


def _g_triple_unpack(v: Any, kw: dict[str, Any]) -> None:
    kw["g_lat"], kw["g_vert"], kw["g_lon"] = v[0], v[1], v[2]


#: Ordered so the on-disk line reads roughly driver inputs → car state → context.
FRAME_SCHEMA: list[FieldSpec] = [
    _scalar("lap", "completed_laps"),
    _scalar("pos", "normalized_car_position", 4, "0..1 around the lap"),
    _scalar("spd", "speed_kmh", 1),
    _scalar("rpm", "rpm"),
    _scalar("gear", "gear", None, "raw sim gear: 0=R, 1=N, 2=1st"),
    # --- driver inputs ---
    _scalar("thr", "gas", 3),
    _scalar("brk", "brake", 3),
    _scalar("clu", "clutch", 3),
    _scalar("str", "steer", 4),
    # --- tyres and brakes ---
    _corners("slip", "wheel_slip", 2, "slip ratio; spots lockups and wheelspin"),
    _scalar("bias", "brake_bias", 1),
    _scalar("tc", "tc_level"),
    _scalar("abs_lv", "abs_level"),
    _corners("ttmp", "tyre_temp", 1),
    _corners("tpsi", "tyre_pressure", 2),
    _corners("btmp", "brake_temp", 0),
    _scalar("fuel", "fuel", 2),
    # --- timing ---
    _scalar("cur_ms", "current_time_ms"),
    _scalar("last_ms", "last_time_ms"),
    _scalar("flag", "flag"),
    _scalar("pit", "is_in_pit_lane"),
    _scalar("vlap", "valid_lap", None, "off-track or a cut invalidates the lap"),
    # --- world position ---
    # normalized_car_position says how far round the lap you are but nothing
    # about where on the track. Without x/z there is no way to tell a tight
    # entry from a wide one after the fact.
    FieldSpec("wxz", _world_xz, _world_xz_unpack, "world x,z — 2dp is cm resolution"),
    _scalar("hdg", "heading", 4, "radians; 0 = along world +Z"),
    # --- racecraft ---
    _scalar("gap_a", "gap_ahead_ms"),
    _scalar("gap_b", "gap_behind_ms"),
    _scalar("racepos", "position"),
    _scalar("delta", "delta_ms"),
    # --- chassis ---
    _corners("susp", "suspension_travel", 4, "near zero = bottoming out"),
    FieldSpec("rh", _ride_heights, _ride_heights_unpack),
    _corners("slipang", "slip_angle", 4, "front > rear = understeer"),
    _corners("load", "wheel_load", 0, "often zeroed by ACC — verify before trusting"),
    FieldSpec("g", _g_triple, _g_triple_unpack, "lateral, vertical, longitudinal"),
    _scalar("ptch", "pitch", 4),
    _scalar("roll", "roll", 4),
]

_BY_KEY = {f.key: f for f in FRAME_SCHEMA}


def encode_frame(snap: Snapshot) -> dict[str, Any]:
    """Pack a Snapshot into its compact on-disk form."""
    return {f.key: f.pack(snap) for f in FRAME_SCHEMA}


def decode_frame(frame: dict[str, Any]) -> Snapshot:
    """Rebuild a Snapshot from a recorded frame.

    Unknown keys are ignored rather than raising, so a recording made by a newer
    version still loads in an older one. Fields absent from the frame keep their
    Snapshot defaults.
    """
    kwargs: dict[str, Any] = {}
    for key, value in frame.items():
        spec = _BY_KEY.get(key)
        if spec is not None:
            spec.unpack(value, kwargs)
    kwargs.setdefault("valid", True)
    return Snapshot(**kwargs)


def frame_keys() -> list[str]:
    """On-disk keys in write order — used for tabular export headers."""
    return [f.key for f in FRAME_SCHEMA]
