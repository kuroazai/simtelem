# AGENTS.md

Guidance for AI coding agents working in or on top of this repository. Humans
will find it useful too — it's the architecture doc.

## What this package is for

`simtelem` turns sim-racing telemetry from several games into one documented data
model, records it durably, and reads it back. That's the whole scope.

**It does not interpret.** No lap analysis, no coaching, no setup advice, no
driving opinions. If you are asked to add anything that draws a conclusion about
driving, it belongs in a package that depends on this one, not here. Keeping the
boundary sharp is what makes this library safe to reuse.

## The one invariant

> **`Snapshot` is the contract. Nothing downstream may branch on which sim a
> frame came from.**

Every source normalises into `Snapshot`. Every consumer reads only `Snapshot`. If
you find yourself writing `if source.name == "acc"` outside `sources/`, the
abstraction has sprung a leak — fix the source, not the consumer.

Two consequences worth internalising:

- A recorded session replays through the same interface as a live sim, so every
  consumer is testable with no game running.
- Adding a sim never requires touching consumer code.

## Layout

```
src/simtelem/
├── models.py          Snapshot + formatting helpers. The contract.
├── sources/
│   ├── base.py        TelemetrySource ABC — connect / read / close
│   ├── acc.py         ACC + Assetto Corsa shared memory (Windows)
│   ├── acc_broadcast.py  ACC UDP broadcast: all cars, positions, timing
│   ├── f1.py          F1 UDP packets
│   ├── f1_probe.py    standalone diagnostic for the F1 feed
│   ├── mock.py        synthetic race; supports a virtual clock
│   ├── replay.py      a recording, served as a live source
│   └── __init__.py    lazy registry + make_source()
├── storage/
│   ├── schema.py      the on-disk frame schema, declared ONCE
│   ├── jsonl.py       JsonlWriter + read_session + iter_frames
│   └── parquet.py     flatten to tabular for analysis
└── cli.py             record / inspect / replay / convert
```

## Rules that matter

### Changing a recorded field? `schema.py` is the only place.

`FRAME_SCHEMA` is a list of `FieldSpec`, each carrying its own `pack` and
`unpack`. Writer and reader both consume it.

**Never add a key to the writer directly.** If encode and decode disagree about a
key, every recording on disk silently becomes wrong and nothing raises. That is
the worst failure mode this package has, and the shared schema is the only thing
preventing it.

To add a field:

```python
# in storage/schema.py, append to FRAME_SCHEMA
_scalar("oil_t", "oil_temp", 1, "degrees C"),
```

That's it — writer, reader, replay and the Parquet column list all pick it up.
Then add a round-trip assertion in `tests/test_schema_roundtrip.py`.

Removing or renaming a key is a **breaking change** to existing recordings. Bump
the `schema` number in the header and handle the old value on read.

### Corner ordering is FL, FR, RL, RR. Always.

Every four-element tuple in `Snapshot` uses that order. A silent swap here
invalidates every per-wheel conclusion anyone draws downstream and will not
raise. `tests/test_schema_roundtrip.py::test_roundtrip_preserves_corner_ordering`
guards it. Don't weaken that test.

### Units go in the docstring or comment, next to the field.

`speed_kmh` not `speed`. `brake_temp` in °C, `suspension_travel` in metres,
`gap_ahead_ms` in milliseconds. If you add a field without stating its unit, you
have added a bug with a delay on it.

### Optional dependencies stay optional.

The base install has **zero dependencies**. ACC needs `pyaccsharedmemory` and
Windows; pandas and pyarrow are for export only. Import them *inside* the
function that needs them and raise a message naming the extra:

```python
try:
    import pandas as pd
except ImportError as exc:
    raise ImportError("to_dataframe needs pandas: pip install simtelem[tabular]") from exc
```

CI runs on Linux without the ACC extra. If your change makes the package
unimportable there, the build fails — correctly.

### Don't trust the sim.

ACC zeroes fields depending on session and mode; `wheel_load` and the
tyre-contact vectors are the usual casualties. `pyaccsharedmemory` has renamed
fields across releases. Read defensively, normalise into `Snapshot`, and document
what can come back empty. See `_pull()` in `sources/acc.py` for the pattern.

## Adding a source: worked example

```python
# src/simtelem/sources/mysim.py
from __future__ import annotations

from typing import Optional

from ..models import Snapshot
from .base import TelemetrySource


class MySimSource(TelemetrySource):
    name = "mysim"

    def __init__(self, *, port: int = 20777) -> None:
        self.port = port
        self._sock = None

    def connect(self) -> None:
        ...  # open the socket / map the shared memory

    def read(self) -> Optional[Snapshot]:
        raw = ...                      # whatever this sim gives you
        if raw is None:
            return None                # None means "no fresh data", not an error
        return Snapshot(
            valid=True,
            speed_kmh=raw.speed * 3.6,          # convert at the boundary
            tyre_temp=(raw.t_fl, raw.t_fr, raw.t_rl, raw.t_rr),   # FL,FR,RL,RR
            ...
        )

    def close(self) -> None:
        ...
```

Then register it:

```python
# src/simtelem/sources/__init__.py
REGISTRY = {
    ...
    "mysim": ".mysim:MySimSource",
}
```

Lazy import strings are deliberate — a source needing an unavailable dependency
must not break `import simtelem`.

Finally, add a test. If the sim can't run on CI, record a short session and
replay it from a fixture.

### `read()` contract

- Return a `Snapshot` when there is fresh data.
- Return `None` when there isn't. **`None` is not an error** — it means "nothing
  new this poll", and callers loop on it.
- Set `valid=False` (or return `None`) when the sim is not live. Writers drop
  invalid frames; emitting garbage-but-valid frames corrupts recordings.
- Convert units at the source boundary. Never leak a sim's native units upward.

## Testing

```bash
pytest                                   # 25 tests, no sim needed
pytest tests/test_schema_roundtrip.py    # the contract tests
```

Use `MockSource(step_seconds=0.05)` for deterministic tests. Without it the mock
runs on wall time, and a tight read loop returns the same frame repeatedly —
which makes assertions pass vacuously. That footgun is why the virtual clock
exists.

For anything consuming telemetry, prefer a recorded fixture over a synthetic one:
record the awkward case once (wet race, safety car, a spin) and it's a regression
test forever.

**Windows note:** if `pytest` fails with `PermissionError` on a `pytest-of-*`
temp directory, that's a stale temp dir, not this package. Pass
`--basetemp=.pytest-tmp`.

## Things not to do

- Don't add analysis, coaching or setup logic. Wrong package.
- Don't add a required dependency. Use an extra.
- Don't hard-code frame keys outside `schema.py`.
- Don't change corner ordering, or weaken the test that guards it.
- Don't make `read()` raise on "no data yet". Return `None`.
- Don't commit recordings. `.gitignore` excludes `*.jsonl` and `*.parquet`; they
  are data, and real ones contain someone's driving.

## Using this library from an agent

If you're building on top rather than inside:

```python
from simtelem import make_source, JsonlWriter, read_session

# Live, or replayed — same code either way
with make_source("acc") as source:
    snap = source.read()
    if snap and snap.valid:
        print(snap.speed_kmh, snap.tyre_temp)

# Analyse a past stint
session = read_session("recordings/x.jsonl")
print(session.track, len(session), session.laps)
```

Check what the machine can actually talk to before assuming:

```python
from simtelem import importable
print(importable())    # e.g. ['mock', 'replay'] on a Linux box
```

Prefer `iter_frames()` over `read_session()` for long recordings — it streams
rather than loading every frame into memory.
