"""The normalised telemetry frame every source produces.

One dataclass, documented units, consistent corner ordering. Sources differ;
 does not — that is the entire point of this package.
"""
from __future__ import annotations

from dataclasses import dataclass

# Tyre / brake arrays are ordered: Front-Left, Front-Right, Rear-Left, Rear-Right
Corners = tuple[float, float, float, float]


@dataclass
class Snapshot:
    """A single normalized telemetry frame. Units noted per field."""

    valid: bool = False  # False when the sim is not live / not driving

    # --- physics ---
    speed_kmh: float = 0.0
    rpm: int = 0
    max_rpm: int = 0
    gear: int = 0          # raw sim gear: 0=R, 1=N, 2=1st, ...
    gas: float = 0.0       # throttle 0..1
    brake: float = 0.0     # brake 0..1
    clutch: float = 0.0    # clutch 0..1 (1 = fully released / engaged)
    steer: float = 0.0     # steering input, normalized -1..1 (left..right)
    fuel: float = 0.0      # litres remaining
    tyre_temp: Corners = (0.0, 0.0, 0.0, 0.0)      # core °C
    tyre_pressure: Corners = (0.0, 0.0, 0.0, 0.0)  # psi
    brake_temp: Corners = (0.0, 0.0, 0.0, 0.0)     # °C
    tc: float = 0.0        # traction-control activity 0..1
    abs: float = 0.0       # ABS activity 0..1
    wheel_slip: Corners = (0.0, 0.0, 0.0, 0.0)  # slip ratio per wheel (lockup/spin)
    brake_bias: float = 0.0  # front brake bias %, e.g. 51.0
    tc_level: int = 0        # TC setting in car (not activity)
    abs_level: int = 0       # ABS setting in car
    car_damage_total: float = 0.0  # sum of damage points (0 = pristine; rises on contact)

    # --- chassis / setup diagnostics ---
    # NOTE: ACC zeroes some physics fields depending on session/mode — wheel_load
    # and the tyre-contact vectors are the usual casualties. Check they are
    # non-zero on a live stint before trusting any advice derived from them.
    suspension_travel: Corners = (0.0, 0.0, 0.0, 0.0)  # metres; ~0 = bottoming out
    ride_height_f: float = 0.0     # metres
    ride_height_r: float = 0.0     # metres
    slip_angle: Corners = (0.0, 0.0, 0.0, 0.0)   # rad; front>rear = understeer
    wheel_load: Corners = (0.0, 0.0, 0.0, 0.0)   # N (often zeroed by ACC)
    brake_pressure: Corners = (0.0, 0.0, 0.0, 0.0)
    pad_life: Corners = (0.0, 0.0, 0.0, 0.0)
    g_lat: float = 0.0     # lateral G
    g_lon: float = 0.0     # longitudinal G
    g_vert: float = 0.0    # vertical G — spikes on kerbs/bottoming
    pitch: float = 0.0
    roll: float = 0.0

    # --- weather / track state (drives push-vs-save strategy) ---
    air_temp: float = 0.0
    road_temp: float = 0.0
    wind_speed: float = 0.0
    track_grip: str = ""        # green|fast|optimum|greasy|damp|wet|flooded
    rain_intensity: str = ""    # no_rain|drizzle|light|medium|heavy|thunderstorm
    rain_in_10min: str = ""
    rain_in_30min: str = ""
    rain_tyres: bool = False

    # --- graphics / session ---
    session_type: str = ""   # practice | qualify | race | hotlap
    gap_ahead_ms: int = 0     # gap to car ahead (ms; 0 if none/unknown)
    gap_behind_ms: int = 0    # gap to car behind (ms)
    valid_lap: bool = True    # current lap still valid (off-track/cut invalidates it)
    completed_laps: int = 0
    position: int = 0
    current_time_ms: int = 0
    last_time_ms: int = 0
    best_time_ms: int = 0
    delta_ms: int = 0                 # live delta to best (if the sim provides it)
    fuel_per_lap: float = 0.0         # litres/lap (sim estimate)
    session_time_left_ms: int = 0
    is_in_pit: bool = False
    is_in_pit_lane: bool = False
    flag: str = "none"                # none|blue|yellow|white|checkered|penalty|black...
    tyre_compound: str = ""
    normalized_car_position: float = 0.0  # 0..1 around the lap

    # --- world position (drives the track map and the proximity spotter) ---
    # ACC publishes coordinates for every car; `world_pos` is the player's own,
    # picked out by matching car_id against player_car_id. `others` is every
    # rival as (distance_m, side) relative to the player's heading, where side
    # is negative for left and positive for right.
    world_pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    heading: float = 0.0        # radians; 0 = along the world +Z axis
    active_cars: int = 0
    others: tuple = ()          # ((dist_m, lateral_m, ahead_m), ...)

    # --- static ---
    car_model: str = ""
    track: str = ""
    max_fuel: float = 0.0

    @property
    def gear_label(self) -> str:
        if self.gear <= 0:
            return "R"
        if self.gear == 1:
            return "N"
        return str(self.gear - 1)

    @property
    def laps_of_fuel(self) -> float | None:
        """Estimated laps remaining on current fuel, or None if unknown."""
        if self.fuel_per_lap > 0.05:
            return self.fuel / self.fuel_per_lap
        return None

    @property
    def mode(self) -> str:
        """Session mode: practice | qualify | race | hotlap | unknown."""
        return self.session_type or "unknown"

    @property
    def understeer_rad(self) -> float:
        """Front slip angle minus rear. Positive = understeer, negative = oversteer.

        Only meaningful with real lateral load — check `abs(g_lat)` is
        appreciable before reading anything into it.
        """
        fl, fr, rl, rr = self.slip_angle
        return ((abs(fl) + abs(fr)) - (abs(rl) + abs(rr))) / 2.0

    @property
    def min_suspension_travel(self) -> float:
        """Closest any corner came to bottoming out (metres)."""
        return min(self.suspension_travel) if self.suspension_travel else 0.0

    @property
    def on_straight(self) -> bool:
        """Rough 'not mid-corner' test — light steering, on power, off the brakes.
        Used to time non-urgent callouts for the straights, not technical sections."""
        return abs(self.steer) < 0.12 and self.gas > 0.6 and self.brake < 0.05


def fmt_gap(ms: int) -> str:
    """Format a gap in milliseconds as '1.23s' (or '' when unknown)."""
    if not ms or ms <= 0:
        return ""
    return f"{ms / 1000:.2f}s"


def fmt_laptime(ms: int) -> str:
    """Format milliseconds as M:SS.mmm (or '--:--' when absent)."""
    if not ms or ms <= 0 or ms >= 999_999_000:
        return "--:--.---"
    m, rem = divmod(int(ms), 60_000)
    s, mmm = divmod(rem, 1000)
    return f"{m}:{s:02d}.{mmm:03d}"
