"""Tabular export for analysis in pandas, Polars, or anything reading Parquet.

JSONL is the right capture format: append-only and crash-tolerant. It is the
wrong analysis format, because a one-hour stint is several hundred thousand lines
and reading one column means parsing all of them. Convert once, analyse cheaply.

Needs the tabular extra: ``pip install simtelem[tabular]``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Union

from .jsonl import read_session
from .schema import frame_keys

PathLike = Union[str, Path]

# Corner fields explode into one column per wheel so the output stays flat.
# Nested lists defeat most columnar query engines.
_CORNERS = ("fl", "fr", "rl", "rr")


def _flatten(frame: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in frame.items():
        if not isinstance(value, list):
            out[key] = value
        elif key == "wxz":
            out["world_x"], out["world_z"] = value
        elif key == "rh":
            out["ride_height_f"], out["ride_height_r"] = value
        elif key == "g":
            out["g_lat"], out["g_vert"], out["g_lon"] = value
        elif len(value) == 4:
            for suffix, v in zip(_CORNERS, value):
                out[key + "_" + suffix] = v
        else:
            for i, v in enumerate(value):
                out[key + "_" + str(i)] = v
    return out


def to_records(path: PathLike) -> List[Dict[str, Any]]:
    """Flatten a recording into one flat dict per frame."""
    return [_flatten(f) for f in read_session(path).raw_frames]


def to_dataframe(path: PathLike):
    """Load a recording as a pandas DataFrame."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "to_dataframe needs pandas: pip install simtelem[tabular]"
        ) from exc
    return pd.DataFrame(to_records(path))


def to_parquet(path: PathLike, out_path: PathLike) -> Path:
    """Convert a JSONL recording to Parquet. Returns the written path."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    to_dataframe(path).to_parquet(out, index=False)
    return out


def columns() -> List[str]:
    """Column names a flattened export produces, in schema order."""
    names: List[str] = []
    for key in frame_keys():
        if key == "wxz":
            names += ["world_x", "world_z"]
        elif key == "rh":
            names += ["ride_height_f", "ride_height_r"]
        elif key == "g":
            names += ["g_lat", "g_vert", "g_lon"]
        else:
            names.append(key)
    return names
