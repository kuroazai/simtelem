"""simtelem - uniform telemetry capture and storage for sim racing.

Sims publish wildly different structures. This package normalises them into one
documented `Snapshot`, records them durably, and reads them back.

    from simtelem import make_source, JsonlWriter

    with make_source("acc") as source, JsonlWriter("recordings") as rec:
        while True:
            snap = source.read()
            if snap:
                rec.log(snap)

What this package deliberately does not do is interpret. There is no lap
analysis, no coaching, no setup advice. It unpacks, normalises and stores.
What you build on top is yours.
"""
from .models import Snapshot, fmt_gap, fmt_laptime
from .sources import TelemetrySource, importable, make_source
from .storage import JsonlWriter, Session, iter_frames, read_session

__version__ = "0.1.0"

__all__ = [
    "Snapshot", "TelemetrySource",
    "make_source", "importable",
    "JsonlWriter", "Session", "read_session", "iter_frames",
    "fmt_laptime", "fmt_gap",
    "__version__",
]
