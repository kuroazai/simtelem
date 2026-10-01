"""Replay a recording through the normal source interface.

The point of a uniform Snapshot is that nothing downstream should be able to tell
where a frame came from. A replay source makes that testable: record a real stint
once, then drive every analysis, dashboard and test off the file, with no sim
running and no race rig attached.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from ..models import Snapshot
from ..storage.jsonl import iter_frames
from .base import TelemetrySource


class ReplaySource(TelemetrySource):
    """Serve frames from a recorded JSONL session.

    With ``realtime=True`` frames are paced to roughly the original capture rate,
    so a replay takes about as long as the stint did. That matters when testing
    anything time-dependent; leave it off to burn through a session as fast as
    the disk allows.
    """

    name = "replay"

    def __init__(
        self,
        path: str | Path,
        *,
        realtime: bool = False,
        loop: bool = False,
        rate_hz: float = 20.0,
    ) -> None:
        self.path = Path(path)
        self.realtime = realtime
        self.loop = loop
        self.rate_hz = rate_hz
        self._frames: Iterator[Snapshot] | None = None
        self._last_wall = 0.0

    def connect(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"no recording at {self.path}")
        self._frames = iter_frames(self.path)
        self._last_wall = time.monotonic()

    def read(self) -> Snapshot | None:
        if self._frames is None:
            raise RuntimeError("call connect() before read()")
        try:
            snap = next(self._frames)
        except StopIteration:
            if not self.loop:
                return None
            self._frames = iter_frames(self.path)
            try:
                snap = next(self._frames)
            except StopIteration:
                return None  # empty recording

        if self.realtime and self.rate_hz > 0:
            target = 1.0 / self.rate_hz
            elapsed = time.monotonic() - self._last_wall
            if elapsed < target:
                time.sleep(target - elapsed)
            self._last_wall = time.monotonic()
        return snap

    def close(self) -> None:
        self._frames = None
