"""The source interface. Implement these three methods and the rest of the
package works with your sim."""
from __future__ import annotations

import abc

from ..models import Snapshot


class TelemetrySource(abc.ABC):
    """Abstract telemetry provider."""

    name: str = "base"

    @abc.abstractmethod
    def connect(self) -> None:
        """Open the underlying connection (shared memory, UDP, etc.)."""

    @abc.abstractmethod
    def read(self) -> Snapshot | None:
        """Return the latest Snapshot, or None if no fresh data is available."""

    def close(self) -> None:  # noqa: B027 - optional by design
        """Release resources.

        Deliberately not abstract: plenty of sources have nothing to release,
        and making every one write an empty override is noise.
        """

    def __enter__(self) -> TelemetrySource:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
