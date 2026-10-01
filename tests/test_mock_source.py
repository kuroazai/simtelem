"""The mock source is what makes everything else testable without a sim."""
from __future__ import annotations

from simtelem import Snapshot, make_source
from simtelem.sources.base import TelemetrySource


def test_mock_is_a_telemetry_source():
    source = make_source("mock")
    assert isinstance(source, TelemetrySource)


def test_mock_yields_valid_snapshots():
    source = make_source("mock", step_seconds=0.05)
    source.connect()
    try:
        frames = [source.read() for _ in range(50)]
    finally:
        source.close()

    assert all(isinstance(f, Snapshot) for f in frames)
    assert any(f.valid for f in frames), "mock produced nothing usable"


def test_mock_moves():
    """A source stuck on one frame would make every consumer look like it works
    while testing nothing."""
    source = make_source("mock", step_seconds=0.05)
    source.connect()
    try:
        speeds = {round(source.read().speed_kmh, 1) for _ in range(200)}
    finally:
        source.close()
    assert len(speeds) > 1, "mock telemetry never changed"


def test_context_manager_connects_and_closes():
    with make_source("mock") as source:
        assert source.read() is not None


def test_unknown_source_name_lists_valid_options():
    import pytest

    with pytest.raises(ValueError, match="unknown source"):
        make_source("gran-turismo")


def test_virtual_clock_is_deterministic():
    """Same step size, same sequence — otherwise tests that depend on the mock
    are flaky by construction."""
    def run():
        source = make_source("mock", step_seconds=0.05)
        source.connect()
        try:
            return [round(source.read().speed_kmh, 3) for _ in range(100)]
        finally:
            source.close()

    assert run() == run()


def test_wall_clock_remains_the_default():
    source = make_source("mock")
    assert source.step_seconds is None
