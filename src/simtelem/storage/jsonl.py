"""Write and read stint recordings as JSON Lines.

One line per frame, plus typed marker lines: a `header` at the top, a `conditions`
line once the sim reports real weather, and a `lap` line at each lap boundary.

JSONL is the default because it is append-only and survives truncation. If the
game crashes or the machine reboots mid-stint, every line already written is
still valid; a single JSON document would be unreadable.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from ..models import Snapshot, fmt_laptime
from .schema import decode_frame, encode_frame

PathLike = Union[str, "os.PathLike[str]"]


class JsonlWriter:
    """Records frames to a timestamped JSONL file, one file per track.

    Durability here is deliberate rather than incidental: every completed lap is
    fsynced immediately and frames are flushed about once a second. Losing a
    stint to a crash means losing an hour of driving, and no amount of re-running
    gets it back.
    """

    def __init__(self, out_dir: PathLike = "recordings", *, flush_every: int = 20) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.flush_every = flush_every
        self.path: Optional[Path] = None
        self.frames = 0
        self._fh = None
        self._t0 = time.monotonic()
        self._laps = -1
        self._wrote_conditions = False
        self._track = ""

    # -- lifecycle ---------------------------------------------------------
    def _open(self, snap: Snapshot) -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        car = snap.car_model or "car"
        track = snap.track or "track"
        self.path = self.out_dir / f"{stamp}_{car}_{track}.jsonl"
        self._fh = self.path.open("w", encoding="utf-8")
        self._t0 = time.monotonic()
        self._write({
            "type": "header", "schema": 1, "car": car, "track": track,
            "started": time.time(), "compound": snap.tyre_compound,
            "session": snap.session_type,
        })
        self._sync()

    def _write(self, obj: Dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def _sync(self) -> None:
        """Flush all the way to disk. Used where losing the line would hurt."""
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- recording ---------------------------------------------------------
    def log(self, snap: Snapshot, extra: Optional[Dict[str, Any]] = None) -> None:
        """Record one frame. Invalid frames (sim not live) are dropped.

        `extra` carries source-specific values that have no Snapshot slot, for
        example F1's lap distance in metres.
        """
        if not snap.valid:
            return

        # One recording is one circuit. A long-lived process must never append
        # Spa frames to a Monza stint, because every distance-based analysis
        # downstream assumes a single track.
        if self._fh is not None and snap.track and self._track and snap.track != self._track:
            self.close()
            self._laps = -1
            self._wrote_conditions = False
        if self._fh is None:
            self._open(snap)
            self._track = snap.track

        # The header lands on the first frame, usually in the garage before the
        # sim has populated weather, so those values would be zeros. Emit a
        # `conditions` line once real numbers appear.
        if not self._wrote_conditions and snap.air_temp > 0:
            self._wrote_conditions = True
            self._write({
                "type": "conditions", "air_temp": snap.air_temp,
                "road_temp": snap.road_temp, "grip": snap.track_grip,
                "rain": snap.rain_intensity, "compound": snap.tyre_compound,
            })
            self._sync()

        if snap.completed_laps != self._laps:
            if self._laps >= 0 and snap.last_time_ms > 0:
                self._write({
                    "type": "lap", "lap": snap.completed_laps,
                    "last_ms": snap.last_time_ms, "best_ms": snap.best_time_ms,
                })
                self._sync()  # never lose a completed lap
            self._laps = snap.completed_laps

        frame = encode_frame(snap)
        frame["t"] = round(time.monotonic() - self._t0, 3)
        if extra:
            frame.update(extra)
        self._write(frame)
        self.frames += 1
        if self.flush_every and self.frames % self.flush_every == 0:
            self._fh.flush()


@dataclass
class Session:
    """A loaded recording: metadata, lap markers and decoded frames."""

    path: Path
    header: Dict[str, Any] = field(default_factory=dict)
    conditions: Dict[str, Any] = field(default_factory=dict)
    laps: List[Dict[str, Any]] = field(default_factory=list)
    frames: List[Snapshot] = field(default_factory=list)
    raw_frames: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def car(self) -> str:
        return self.header.get("car", "")

    @property
    def track(self) -> str:
        return self.header.get("track", "")

    def __len__(self) -> int:
        return len(self.frames)

    def summary(self) -> str:
        best = min((l["last_ms"] for l in self.laps if l.get("last_ms")), default=0)
        return (
            f"{self.path.name}\n"
            f"  car     {self.car or '?'}\n"
            f"  track   {self.track or '?'}\n"
            f"  frames  {len(self.frames)}\n"
            f"  laps    {len(self.laps)}\n"
            f"  best    {fmt_laptime(best)}"
        )


def iter_frames(path: PathLike) -> Iterator[Snapshot]:
    """Stream Snapshots from a recording without loading it all into memory.

    A truncated final line is skipped rather than raising: a recording cut short
    by a crash is still worth reading up to the break.
    """
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated tail
            if obj.get("type"):
                continue  # marker line, not a frame
            yield decode_frame(obj)


def read_session(path: PathLike) -> Session:
    """Load a whole recording: header, conditions, lap markers and frames."""
    p = Path(path)
    out = Session(path=p)
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "header":
                out.header = obj
            elif kind == "conditions":
                out.conditions = obj
            elif kind == "lap":
                out.laps.append(obj)
            elif kind is None:
                out.raw_frames.append(obj)
                out.frames.append(decode_frame(obj))
    return out
