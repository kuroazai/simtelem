"""A synthetic telemetry source: drives a fake race so you can test with no sim.

It burns fuel, heats/cools tyres and brakes, completes laps, and occasionally
throws a flag — enough to exercise every event and callout end-to-end.
"""
from __future__ import annotations

import math
import time

from ..models import Snapshot
from .base import TelemetrySource

LAP_SECONDS = 40.0        # a short fake lap so events fire quickly
BASE_LAP_MS = 92_500      # ~1:32.5 baseline
FUEL_PER_LAP = 2.6


class MockSource(TelemetrySource):
    name = "mock"

    def __init__(self, *, step_seconds: float | None = None) -> None:
        """
        Args:
            step_seconds: when set, the mock runs on a virtual clock advancing by
                this much per `read()` instead of wall time. Tests need this: a
                tight read loop against a real clock returns the same frame over
                and over, so assertions either pass vacuously or need sleeps.
                Leave it None for real-time behaviour.
        """
        self.step_seconds = step_seconds
        self._virtual = 0.0
        self._t0 = 0.0
        self._laps = 0
        self._best_ms = 0
        self._last_ms = 0
        self._fuel = 22.0
        self._flag = "none"
        self._flag_until = 0.0

    def connect(self) -> None:
        self._t0 = time.monotonic()
        self._virtual = 0.0

    def _elapsed(self) -> float:
        """Seconds since connect, from whichever clock is in use."""
        if self.step_seconds is None:
            return time.monotonic() - self._t0
        self._virtual += self.step_seconds
        return self._virtual

    def read(self) -> Snapshot | None:
        elapsed = self._elapsed()
        lap_frac = (elapsed % LAP_SECONDS) / LAP_SECONDS
        laps = int(elapsed // LAP_SECONDS)

        # Lap rollover: record a slightly varying last-lap time & burn fuel.
        if laps > self._laps:
            self._laps = laps
            self._last_ms = BASE_LAP_MS + int(1500 * math.sin(laps))
            if self._best_ms == 0 or self._last_ms < self._best_ms:
                self._best_ms = self._last_ms
            self._fuel = max(0.0, self._fuel - FUEL_PER_LAP)

        # Occasional yellow flag around the 20s mark of every 3rd lap.
        if laps % 3 == 2 and 0.45 < lap_frac < 0.55:
            self._flag = "yellow"
        else:
            self._flag = "none"

        # Tyres warm up over the first lap, then oscillate; spike on lap 4.
        warm = min(1.0, elapsed / LAP_SECONDS)
        base_tyre = 55 + warm * 45 + 8 * math.sin(elapsed / 3.0)
        hot_bias = 15 if laps == 4 else 0
        tyres = (
            base_tyre + hot_bias + 4,
            base_tyre + hot_bias + 6,
            base_tyre + 2,
            base_tyre + 3,
        )
        brakes = tuple(300 + 350 * abs(math.sin(elapsed / 2.0)) for _ in range(4))

        speed = 120 + 90 * math.sin(lap_frac * math.tau)
        rpm = int(4000 + 3500 * abs(math.sin(lap_frac * math.tau * 4)))

        return Snapshot(
            valid=True,
            speed_kmh=max(40.0, speed),
            rpm=rpm,
            max_rpm=8500,
            gear=max(2, min(6, 2 + int(speed // 45))),
            gas=0.8,
            brake=0.0,
            clutch=1.0,
            steer=round(math.sin(lap_frac * math.tau * 3), 3),  # fake cornering inputs
            wheel_slip=(0.0, 0.0, 0.0, 0.0),
            brake_bias=51.0,
            car_damage_total=0.0,
            valid_lap=True,
            session_type="race",
            gap_ahead_ms=int(max(200, 1800 - (elapsed % 50) * 35)),   # catching ahead
            gap_behind_ms=int(max(300, 2200 - (elapsed % 40) * 50)),  # closing behind
            fuel=self._fuel,
            tyre_temp=(round(tyres[0], 1), round(tyres[1], 1),
                       round(tyres[2], 1), round(tyres[3], 1)),
            tyre_pressure=(27.4, 27.6, 27.2, 27.3),
            brake_temp=(round(brakes[0], 1), round(brakes[1], 1),
                        round(brakes[2], 1), round(brakes[3], 1)),
            tc=0.0,
            abs=0.0,
            completed_laps=self._laps,
            position=4,
            current_time_ms=int(lap_frac * BASE_LAP_MS),
            last_time_ms=self._last_ms,
            best_time_ms=self._best_ms,
            fuel_per_lap=FUEL_PER_LAP,
            session_time_left_ms=max(0, 1_800_000 - int(elapsed * 1000)),
            is_in_pit=False,
            is_in_pit_lane=False,
            flag=self._flag,
            tyre_compound="dry",
            normalized_car_position=lap_frac,
            car_model="mock_gt3",
            track="mock_ring",
            max_fuel=120.0,
        )
