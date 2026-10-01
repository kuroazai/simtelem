"""Record, read back, replay. The round trip is the whole promise."""
from __future__ import annotations

from simtelem import JsonlWriter, make_source, read_session
from simtelem.sources.replay import ReplaySource


def _record(tmp_path, frames: int = 120):
    source = make_source("mock")
    source.connect()
    writer = JsonlWriter(tmp_path, flush_every=0)
    written = 0
    try:
        for _ in range(frames * 2):  # mock may emit invalid frames
            snap = source.read()
            if snap and snap.valid:
                writer.log(snap)
                written += 1
            if written >= frames:
                break
    finally:
        source.close()
        writer.close()
    return writer.path, written


def test_recording_is_written(tmp_path):
    path, written = _record(tmp_path)
    assert path.exists()
    assert written > 0
    assert path.suffix == ".jsonl"


def test_header_carries_car_and_track(tmp_path):
    path, _ = _record(tmp_path)
    session = read_session(path)
    assert session.car
    assert session.track
    assert session.header.get("schema") == 1


def test_frame_count_survives_the_round_trip(tmp_path):
    path, written = _record(tmp_path)
    session = read_session(path)
    assert len(session) == written


def test_replay_serves_every_recorded_frame(tmp_path):
    path, written = _record(tmp_path)
    source = ReplaySource(path)
    source.connect()
    replayed = 0
    try:
        while source.read() is not None:
            replayed += 1
    finally:
        source.close()
    assert replayed == written


def test_replay_is_substitutable_for_a_live_source(tmp_path):
    """Consumers must not be able to tell a replay from a sim."""
    path, _ = _record(tmp_path)
    with ReplaySource(path) as source:
        from simtelem.sources.base import TelemetrySource

        assert isinstance(source, TelemetrySource)
        assert source.read() is not None


def test_replay_of_missing_file_fails_loudly(tmp_path):
    import pytest

    source = ReplaySource(tmp_path / "nope.jsonl")
    with pytest.raises(FileNotFoundError):
        source.connect()


def test_truncated_recording_still_reads(tmp_path):
    """A stint cut short by a crash is worth reading up to the break."""
    path, written = _record(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: int(len(text) * 0.8)] + '{"spd": 1', encoding="utf-8")

    session = read_session(path)
    assert 0 < len(session) < written


def test_flatten_produces_one_row_per_frame(tmp_path):
    from simtelem.storage.parquet import to_records

    path, written = _record(tmp_path)
    rows = to_records(path)
    assert len(rows) == written
    assert "slip_fl" in rows[0], "corner fields should explode into per-wheel columns"
    assert not any(isinstance(v, list) for v in rows[0].values()), "output must be flat"
