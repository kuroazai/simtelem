# simtelem

**Uniform telemetry capture and storage for sim racing.** One documented data
model across Assetto Corsa Competizione, Assetto Corsa and F1 — recorded
durably, readable back, and replayable as if it were live.

```bash
pip install simtelem
```

---

## What this is

Every sim publishes telemetry in its own shape. ACC uses a Windows shared-memory
block whose field names drift between releases. F1 streams packed UDP. Assetto
Corsa does something else again. If you want to build anything on top — a coach,
a dashboard, a strategy tool, a dataset — you spend your first week on byte
layouts instead of the thing you actually wanted to build.

`simtelem` does that week for you and then gets out of the way.

**What it deliberately does not do: interpret.** There is no lap analysis, no
coaching, no setup advice, no opinion about your driving. It unpacks, normalises
and stores. What you build on top is yours.

## 60-second start

No sim required — the mock source drives a synthetic race:

```python
from simtelem import make_source, JsonlWriter

with make_source("mock", step_seconds=0.05) as source:
    with JsonlWriter("recordings") as recording:
        for _ in range(1000):
            snap = source.read()
            if snap:
                recording.log(snap)

print(recording.path)
```

Read it back:

```python
from simtelem import read_session

session = read_session("recordings/20261001_120000_mock_gt3_mock_ring.jsonl")
print(session.summary())

for snap in session.frames[:5]:
    print(snap.speed_kmh, snap.gear_label, snap.tyre_temp)
```

Against a real sim, change one string:

```python
with make_source("acc") as source:       # ACC running, you on track
    ...
```

From the shell:

```bash
simtelem sources                      # what can this machine talk to?
simtelem record --source acc          # record until ctrl-c
simtelem inspect recordings/x.jsonl --laps
simtelem replay recordings/x.jsonl --realtime
simtelem convert recordings/x.jsonl   # -> Parquet
```

## Sources

| Name | Sim | Transport | Notes |
|------|-----|-----------|-------|
| `acc` | ACC, Assetto Corsa | shared memory | Windows only. Needs `simtelem[acc]` |
| `acc_broadcast` | ACC | UDP | Position and timing for every car on track |
| `f1` | F1 series | UDP | Packed telemetry packets |
| `mock` | — | synthetic | Fake race. No sim, no rig, no Windows |
| `replay` | — | recorded file | A past stint, served as a live source |

Every one implements the same three-method interface:

```python
class TelemetrySource(abc.ABC):
    def connect(self) -> None: ...
    def read(self) -> Optional[Snapshot]: ...
    def close(self) -> None: ...
```

So consumers never branch on which sim they're reading. That is the whole point.

## The data model

`Snapshot` is one flat dataclass with **units documented on every field** and a
consistent corner ordering — **front-left, front-right, rear-left, rear-right**,
everywhere, no exceptions.

```python
snap.speed_kmh          # km/h
snap.gas                # 0..1
snap.brake              # 0..1
snap.steer              # -1..1, left..right
snap.tyre_temp          # (FL, FR, RL, RR) core °C
snap.tyre_pressure      # (FL, FR, RL, RR) psi
snap.suspension_travel  # (FL, FR, RL, RR) metres — near 0 means bottoming out
snap.g_lat              # lateral G
snap.world_pos          # (x, y, z) metres
snap.normalized_car_position  # 0..1 around the lap
```

Plus derived properties that are cheap to compute and easy to get wrong:

```python
snap.gear_label        # "R", "N", "3" — raw sim gear is offset by two
snap.laps_of_fuel      # None when the sim hasn't estimated consumption yet
snap.understeer_rad    # front slip angle minus rear; positive = understeer
snap.on_straight       # light steering, on power, off the brakes
```

### Fields the sim will lie to you about

This is the part that costs people days, so it's documented rather than
discovered:

- **ACC zeroes some physics fields depending on session and mode.**
  `wheel_load` and the tyre-contact vectors are the usual casualties. Check
  they're non-zero on a live stint before drawing conclusions from them.
- **`understeer_rad` is meaningless at low lateral load.** Check `abs(g_lat)` is
  appreciable first, or you'll read understeer into a straight line.
- **`laps_of_fuel` returns `None`, not zero,** when consumption is unknown. The
  difference matters if you're making a pit call.
- **`pyaccsharedmemory` field names have changed across releases.** `sources/acc.py`
  reads defensively and normalises; if a value reads as 0, check the installed
  package's names against `_pull()` there before assuming the sim is at fault.

## Storage

Two formats, for two different jobs.

**JSONL for capture.** Append-only and truncation-tolerant: if the game crashes
or the machine reboots mid-stint, every line already written is still valid. Each
completed lap is `fsync`ed immediately, because losing an hour of driving isn't
recoverable by re-running anything. A single JSON document would be unreadable
after a crash.

**Parquet for analysis.** A one-hour stint is several hundred thousand lines, and
reading one column out of JSONL means parsing all of them. Convert once:

```python
from simtelem.storage.parquet import to_dataframe

df = to_dataframe("recordings/x.jsonl")   # needs simtelem[tabular]
df.groupby("lap")["spd"].max()
```

Corner fields explode into `_fl`, `_fr`, `_rl`, `_rr` columns so the output is
flat — nested lists defeat most columnar query engines.

### One schema, two directions

The on-disk format is declared once in `storage/schema.py` and used by both the
writer and the reader. This isn't tidiness: if encode and decode ever disagree
about a key, every recording on disk silently becomes wrong and nothing raises to
tell you. Sharing one declaration makes that class of bug impossible, and the
round-trip is covered by tests.

Recordings are also forward-compatible. Unknown keys are ignored rather than
raising, so a file written by a newer version still loads in an older one.

## Testing without a sim

This is the design property worth stealing even if you don't use the package.

Because `ReplaySource` implements the same interface as a live sim, anything you
build on top can be tested against a real recorded stint with no game running:

```python
from simtelem.sources.replay import ReplaySource

with ReplaySource("recordings/spa_wet_stint.jsonl") as source:
    my_analysis.run(source)      # identical code path to live
```

Record the weird edge case once — the wet race, the safety car, the spin — and it
becomes a regression test forever. No rig, no Windows, no sim licence on CI.

## Install options

```bash
pip install simtelem                 # core: models, mock, replay, JSONL
pip install "simtelem[acc]"          # + ACC shared memory (Windows)
pip install "simtelem[tabular]"      # + pandas / Parquet export
pip install "simtelem[all]"          # everything
```

The base install has **no dependencies**. ACC support is an extra specifically so
the package stays importable on Linux, which is where analysis and CI usually run.

## Development

```bash
git clone https://github.com/kuroazai/simtelem
cd simtelem
pip install -e ".[tabular,dev]"

pytest                  # 25 tests, no sim needed
ruff check src tests
mypy
```

CI runs the suite on Python 3.9 through 3.12 without the ACC extra — if the
package stops importing on Linux, the build fails.

## Contributing

Adding a sim is the most useful contribution. See [AGENTS.md](AGENTS.md) for the
architecture, the invariants that must hold, and a worked example of adding a
source — it's written for coding agents but it's the clearest guide either way.

## Licence

MIT. See [LICENSE](LICENSE).
