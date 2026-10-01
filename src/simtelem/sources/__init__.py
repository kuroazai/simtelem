"""Telemetry sources. Each normalises one sim into the same `Snapshot`."""
from __future__ import annotations

import importlib
from typing import Any

from .base import TelemetrySource

#: name -> "module:class", imported lazily so that installing without the ACC or
#: F1 extras still leaves the rest of the package usable.
REGISTRY = {
    "acc": ".acc:ACCSource",
    "acc_broadcast": ".acc_broadcast:ACCBroadcastClient",
    "f1": ".f1:F1Source",
    "mock": ".mock:MockSource",
    "replay": ".replay:ReplaySource",
}


def make_source(name: str, **kwargs: Any) -> TelemetrySource:
    """Build a source by name. An unknown name lists the valid ones."""
    key = (name or "mock").lower()
    if key not in REGISTRY:
        valid = ", ".join(sorted(REGISTRY))
        raise ValueError("unknown source " + repr(name) + "; expected one of: " + valid)
    module_path, class_name = REGISTRY[key].split(":")
    module = importlib.import_module(module_path, package=__name__)
    return getattr(module, class_name)(**kwargs)


def importable() -> list[str]:
    """Source names whose module imports cleanly in this environment.

    Useful for a quick environment check: the ACC sources need Windows and the
    shared-memory package, so they will be missing on a Linux CI box.
    """
    ok: list[str] = []
    for name, target in sorted(REGISTRY.items()):
        module_path = target.split(":")[0]
        try:
            importlib.import_module(module_path, package=__name__)
            ok.append(name)
        except Exception:
            continue
    return ok


__all__ = ["TelemetrySource", "make_source", "importable", "REGISTRY"]
