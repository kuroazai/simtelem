"""The schema is the contract between writer and reader.

If encode/decode ever disagree, every recording on disk silently becomes wrong,
and nothing raises to tell you. These tests exist to make that failure loud.
"""
from __future__ import annotations

import math

import pytest

from simtelem import Snapshot
from simtelem.storage.schema import FRAME_SCHEMA, decode_frame, encode_frame, frame_keys


def _populated() -> Snapshot:
    """A Snapshot with every schema-covered field set to a distinct value."""
    return Snapshot(
        valid=True,
        speed_kmh=212.4,
        rpm=7350,
        gear=5,
        gas=0.98,
        brake=0.12,
        clutch=1.0,
        steer=-0.3421,
        fuel=43.21,
        tyre_temp=(88.1, 89.2, 86.0, 85.5),
        tyre_pressure=(27.51, 27.62, 26.93, 26.84),
        brake_temp=(412.0, 420.0, 330.0, 325.0),
        wheel_slip=(0.01, 0.02, 0.0, 0.03),
        brake_bias=51.4,
        tc_level=3,
        abs_level=2,
        suspension_travel=(0.0412, 0.0398, 0.0501, 0.0489),
        ride_height_f=0.0612,
        ride_height_r=0.0724,
        slip_angle=(0.0421, 0.0398, 0.0312, 0.0305),
        wheel_load=(3100.0, 3250.0, 2900.0, 2850.0),
        g_lat=1.84,
        g_lon=-0.41,
        g_vert=1.02,
        pitch=0.0123,
        roll=0.0045,
        session_type="race",
        gap_ahead_ms=1234,
        gap_behind_ms=5678,
        valid_lap=True,
        completed_laps=7,
        position=3,
        current_time_ms=61234,
        last_time_ms=103456,
        flag="yellow",
        normalized_car_position=0.4312,
        world_pos=(123.45, 9.87, 678.9),
        heading=1.2345,
        is_in_pit_lane=False,
    )


def test_every_spec_has_a_unique_key():
    keys = frame_keys()
    assert len(keys) == len(set(keys)), "duplicate on-disk key would overwrite data"


def test_roundtrip_preserves_scalars():
    original = _populated()
    restored = decode_frame(encode_frame(original))

    assert restored.speed_kmh == pytest.approx(original.speed_kmh)
    assert restored.rpm == original.rpm
    assert restored.gear == original.gear
    assert restored.completed_laps == original.completed_laps
    assert restored.flag == original.flag
    assert restored.position == original.position
    assert restored.heading == pytest.approx(original.heading, abs=1e-4)


def test_roundtrip_preserves_corner_ordering():
    """FL, FR, RL, RR must survive intact — a swap here is silent and ruins
    every per-wheel conclusion drawn downstream."""
    original = _populated()
    restored = decode_frame(encode_frame(original))

    for attr in ("tyre_temp", "tyre_pressure", "wheel_slip", "slip_angle", "suspension_travel"):
        got = getattr(restored, attr)
        want = getattr(original, attr)
        assert len(got) == 4, f"{attr} lost its corner shape"
        for g, w in zip(got, want):
            assert g == pytest.approx(w, abs=0.01), f"{attr} corner mismatch"


def test_roundtrip_preserves_g_triple_order():
    original = _populated()
    restored = decode_frame(encode_frame(original))
    assert restored.g_lat == pytest.approx(original.g_lat)
    assert restored.g_vert == pytest.approx(original.g_vert)
    assert restored.g_lon == pytest.approx(original.g_lon)


def test_roundtrip_preserves_ride_heights_separately():
    original = _populated()
    restored = decode_frame(encode_frame(original))
    assert restored.ride_height_f == pytest.approx(original.ride_height_f, abs=1e-4)
    assert restored.ride_height_r == pytest.approx(original.ride_height_r, abs=1e-4)
    assert restored.ride_height_f != restored.ride_height_r


def test_world_y_is_dropped_by_design():
    """Elevation costs bytes and answers no racing-line question. x and z are
    kept at centimetre resolution; y comes back as zero, documented not lost."""
    original = _populated()
    restored = decode_frame(encode_frame(original))
    assert restored.world_pos[0] == pytest.approx(123.45)
    assert restored.world_pos[2] == pytest.approx(678.9)
    assert restored.world_pos[1] == 0.0


def test_unknown_keys_are_ignored():
    """A recording from a newer version must still load in an older one."""
    frame = encode_frame(_populated())
    frame["some_future_field"] = 42
    restored = decode_frame(frame)
    assert restored.speed_kmh == pytest.approx(212.4)


def test_missing_keys_fall_back_to_defaults():
    restored = decode_frame({"spd": 100.0})
    assert restored.speed_kmh == pytest.approx(100.0)
    assert restored.rpm == 0
    assert restored.tyre_temp == (0.0, 0.0, 0.0, 0.0)


def test_decoded_frames_are_valid():
    """Only valid frames are written, so a decoded frame is valid by definition."""
    assert decode_frame({"spd": 50.0}).valid is True


def test_no_nan_leaks_through_encoding():
    snap = Snapshot(valid=True, speed_kmh=float("nan"))
    encoded = encode_frame(snap)
    assert math.isnan(encoded["spd"]), "NaN should survive rather than silently become 0"
