"""Command line interface: record, inspect, replay, convert.

Deliberately thin. Everything here is a few lines over the public API, because a
CLI that can do things the library cannot is a design smell.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .sources import REGISTRY, importable, make_source
from .storage import JsonlWriter, read_session


def _cmd_sources(_: argparse.Namespace) -> int:
    ok = set(importable())
    print("source          importable here")
    print("-" * 32)
    for name in sorted(REGISTRY):
        mark = "yes" if name in ok else "no"
        print(f"{name:<16}{mark}")
    if len(ok) < len(REGISTRY):
        print("\nMissing sources usually mean an optional extra is not installed,")
        print("or you are not on Windows (ACC shared memory is Windows-only).")
        print("Try: pip install 'simtelem[acc]'")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    source = make_source(args.source)
    writer = JsonlWriter(args.out)
    deadline = time.monotonic() + args.seconds if args.seconds else None
    interval = 1.0 / args.rate if args.rate > 0 else 0.0

    print(f"recording from {args.source!r} at {args.rate:g} Hz -> {args.out}")
    print("ctrl-c to stop")
    source.connect()
    try:
        while True:
            snap = source.read()
            if snap is not None:
                writer.log(snap)
                if writer.frames and writer.frames % 200 == 0:
                    print(f"  {writer.frames} frames", end="\r", flush=True)
            if deadline and time.monotonic() >= deadline:
                break
            if interval:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        source.close()
        writer.close()

    if writer.frames == 0:
        print("no frames captured — is the sim running and on track?", file=sys.stderr)
        return 1
    print(f"saved {writer.frames} frames -> {writer.path}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"no such recording: {path}", file=sys.stderr)
        return 1
    session = read_session(path)
    print(session.summary())
    if session.conditions:
        air = session.conditions.get("air_temp")
        road = session.conditions.get("road_temp")
        grip = session.conditions.get("grip") or "?"
        print(f"  air {air}C  road {road}C  grip {grip}")
    if args.laps and session.laps:
        from .models import fmt_laptime

        print("\n  lap   time")
        for lap in session.laps:
            print(f"  {lap.get('lap', '?'):<5} {fmt_laptime(lap.get('last_ms', 0))}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from .sources.replay import ReplaySource

    source = ReplaySource(args.path, realtime=args.realtime)
    source.connect()
    count = 0
    try:
        while True:
            snap = source.read()
            if snap is None:
                break
            count += 1
            if args.print_every and count % args.print_every == 0:
                print(
                    f"{count:>6}  lap {snap.completed_laps}  "
                    f"{snap.speed_kmh:6.1f} km/h  gear {snap.gear_label}  "
                    f"thr {snap.gas:.2f}  brk {snap.brake:.2f}"
                )
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    print(f"replayed {count} frames")
    return 0


def _cmd_convert(args: argparse.Namespace) -> int:
    try:
        from .storage.parquet import to_parquet
    except ImportError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    src = Path(args.path)
    out = Path(args.out) if args.out else src.with_suffix(".parquet")
    try:
        written = to_parquet(src, out)
    except ImportError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"wrote {written}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simtelem",
        description="Uniform telemetry capture and storage for sim racing.",
    )
    parser.add_argument("--version", action="version", version=f"simtelem {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="list telemetry sources and whether they import here")
    p.set_defaults(func=_cmd_sources)

    p = sub.add_parser("record", help="record a stint to JSONL")
    p.add_argument("-s", "--source", default="acc", choices=sorted(REGISTRY))
    p.add_argument("-o", "--out", default="recordings", help="output directory")
    p.add_argument("-r", "--rate", type=float, default=20.0, help="poll rate in Hz")
    p.add_argument("--seconds", type=float, default=0,
                   help="stop after N seconds (0 = until ctrl-c)")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("inspect", help="summarise a recording")
    p.add_argument("path")
    p.add_argument("--laps", action="store_true", help="list lap times")
    p.set_defaults(func=_cmd_inspect)

    p = sub.add_parser("replay", help="replay a recording through the source interface")
    p.add_argument("path")
    p.add_argument("--realtime", action="store_true", help="pace to the original capture rate")
    p.add_argument("--print-every", type=int, default=100, metavar="N")
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser("convert", help="convert a recording to Parquet")
    p.add_argument("path")
    p.add_argument("-o", "--out")
    p.set_defaults(func=_cmd_convert)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
