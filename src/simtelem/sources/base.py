"""The source interface. Implement these three methods and the rest of the
package works with your sim."""
from __future__ import annotations

import abc
from typing import Optional

from ..models import Snapshot


class TelemetrySource(abc.ABC):
    """Abstract telemetry provider."""

    name: str = "base"

    @abc.abstractmethod
    def connect(self) -> None:
        """Open the underlying connection (shared memory, UDP, etc.)."""

    @abc.abstractmethod
    def read(self) -> Optional[Snapshot]:
        """Return the latest Snapshot, or None if no fresh data is available."""

    def close(self) -> None:  # optional override
        """Release resources."""

    def __enter__(self) -> "TelemetrySource":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
