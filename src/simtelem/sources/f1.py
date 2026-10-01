"""F1 25 (EA/Codemasters) telemetry via the UDP broadcast feed.

The game pushes a set of fixed-layout binary packets to a UDP port (default 20777)
when Settings -> Telemetry Settings -> UDP Telemetry is On. Unlike ACC's shared
memory this is a one-way stream: we can read the car and the driver's setup, but
there is no write path -- setups still have to be typed in by hand in the garage.

Layout notes (important):
  * Every packet starts with a 29-byte header (F1 23 format onward).
  * Codemasters append fields to the END of structs between game years but rarely
    reorder the front. So each struct below is parsed as a PREFIX -- we read only
    the leading fields we actually need and ignore the tail. That survives a
    format bump; a full-struct parse would not.
  * Sizes are asserted against the real packets at runtime. `unsupported` on the
    source lists any packet whose size disagreed with the spec we compiled for,
    so a mismatch shows up as a diagnostic instead of silent garbage numbers.

Run `python -m simtelem.sources.f1_probe` to verify the feed live.
"""
from __future__ import annotations

import socket
import struct
import threading

from ..models import Snapshot
from .base import TelemetrySource

DEFAULT_PORT = 20777
MAX_CARS = 22

# --- packet ids ---
PID_MOTION = 0
PID_SESSION = 1
PID_LAP_DATA = 2
PID_CAR_SETUPS = 5
PID_CAR_TELEMETRY = 6
PID_CAR_STATUS = 7
PID_SESSION_HISTORY = 11
PID_MOTION_EX = 13

# --- struct formats (little-endian, prefix parses) ---
# uint16 packetFormat, uint8 gameYear/major/minor/packetVersion/packetId,
# uint64 sessionUID, float sessionTime, uint32 frameId, uint32 overallFrameId,
# uint8 playerCarIndex, uint8 secondaryPlayerCarIndex
_HEADER = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = _HEADER.size  # 29

# CarTelemetryData -- full struct, 60 bytes/car
_TELEMETRY = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")
# LapData -- full struct, 57 bytes/car
_LAP = struct.Struct("<IIHBHBHBHBfff15BHHBfB")
# CarStatusData -- full struct, 55 bytes/car
_STATUS = struct.Struct("<5BfffHHBBHBBBbfffBfffB")
# CarSetupData -- full struct, 50 bytes/car. Layout confirmed empirically against
# a live F1 25 stream (packet 1133 = 29 header + 22*50 + 4 trailing float, which is
# the pending front-wing value). Tyre pressures arrive RL, RR, FL, FR.
_SETUP = struct.Struct("<4B4f8BB4fBf")
# PacketSessionData -- PREFIX only, through sessionDuration (13 bytes)
_SESSION_PREFIX = struct.Struct("<BbbBHBbBHH")
# PacketMotionExData -- PREFIX only: suspensionPosition/Velocity/Acceleration,
# wheelSpeed, wheelSlipRatio, wheelSlipAngle (6 x 4 floats = 96 bytes)
_MOTION_EX_PREFIX = struct.Struct("<24f")

# PacketSessionHistoryData: carIdx, numLaps, numTyreStints, then the lap numbers
# holding the best lap and each best sector.
_HISTORY_HEAD = struct.Struct("<7B")
# LapHistoryData -- 14 bytes/lap, 100 laps. Sector times are split into a
# minutes byte and a milliseconds uint16 (a sector can exceed 65s).
_LAP_HISTORY = struct.Struct("<IHBHBHBB")

EXPECTED_SIZES = {
    PID_LAP_DATA: HEADER_SIZE + MAX_CARS * _LAP.size + 2,        # 1285
    PID_CAR_TELEMETRY: HEADER_SIZE + MAX_CARS * _TELEMETRY.size + 3,  # 1352
    PID_CAR_STATUS: HEADER_SIZE + MAX_CARS * _STATUS.size,       # 1239
}

# Wheel order differs: F1 sends RL, RR, FL, FR. Our Snapshot uses FL, FR, RL, RR.
def _reorder(vals) -> tuple:
    rl, rr, fl, fr = vals
    return (fl, fr, rl, rr)


TRACK_NAMES = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir", 4: "Catalunya",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi",
    15: "Texas", 16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico", 20: "Baku",
    21: "Sakhir Short", 22: "Silverstone Short", 23: "Texas Short", 24: "Suzuka Short",
    25: "Hanoi", 26: "Zandvoort", 27: "Imola", 28: "Portimao", 29: "Jeddah",
    30: "Miami", 31: "Las Vegas", 32: "Losail",
}

# Coarse mapping; F1 has renumbered the tail of this enum between years, so
# anything unrecognised falls through to "" rather than guessing wrong.
SESSION_TYPES = {
    1: "practice", 2: "practice", 3: "practice", 4: "practice",
    5: "qualify", 6: "qualify", 7: "qualify", 8: "qualify", 9: "qualify",
    10: "race", 11: "race", 12: "race", 13: "hotlap",
}

VISUAL_COMPOUNDS = {
    16: "soft", 17: "medium", 18: "hard", 7: "inter", 8: "wet",
    15: "wet", 19: "super soft", 20: "soft", 21: "medium", 22: "hard",
}


class F1Setup:
    """The driver's current garage setup, as broadcast by the game."""

    __slots__ = ("front_wing", "rear_wing", "on_throttle_diff", "off_throttle_diff",
                 "front_camber", "rear_camber", "front_toe", "rear_toe",
                 "front_susp", "rear_susp", "front_arb", "rear_arb",
                 "front_height", "rear_height", "brake_pressure", "brake_bias",
                 "engine_braking", "tyre_pressure", "ballast", "fuel_load")

    def __init__(self, values):
        (self.front_wing, self.rear_wing, self.on_throttle_diff, self.off_throttle_diff,
         self.front_camber, self.rear_camber, self.front_toe, self.rear_toe,
         self.front_susp, self.rear_susp, self.front_arb, self.rear_arb,
         self.front_height, self.rear_height, self.brake_pressure,
         self.brake_bias, self.engine_braking,
         p_rl, p_rr, p_fl, p_fr, self.ballast, self.fuel_load) = values
        self.tyre_pressure = (p_fl, p_fr, p_rl, p_rr)  # normalize to FL, FR, RL, RR

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self) -> str:
        return (f"F1Setup(wing {self.front_wing}/{self.rear_wing}, "
                f"diff {self.on_throttle_diff}/{self.off_throttle_diff}, "
                f"arb {self.front_arb}/{self.rear_arb}, "
                f"susp {self.front_susp}/{self.rear_susp}, "
                f"brake {self.brake_pressure}% @ {self.brake_bias}%)")


class F1UdpSource(TelemetrySource):
    """Listens on a UDP port and assembles F1 25 packets into a Snapshot.

    A background thread drains the socket so a slow consumer never backs up the
    kernel buffer and starts dropping frames; `read()` just snapshots whatever
    the most recent packets said.
    """

    name = "f1_25"

    def __init__(self, port: int = DEFAULT_PORT, bind: str = "0.0.0.0") -> None:
        self.port = port
        self.bind = bind
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._latest: dict[int, bytes] = {}   # packet id -> raw payload
        # SessionHistory rotates through one car per frame, so it cannot share
        # the single-slot _latest cache -- key it by car index instead.
        self._history: dict[int, bytes] = {}
        self._player_idx = 0
        self._track_length = 0
        self._packet_format = 0
        self._best_ms = 0
        self.packet_counts: dict[int, int] = {}
        self.unsupported: dict[int, str] = {}  # packet id -> why we skipped it
        self.setup: F1Setup | None = None
        # Raw positional fields that Snapshot has no slot for, refreshed by read().
        # Distance in metres is what corner analysis needs -- the normalized 0..1
        # position loses resolution exactly where corners are tightest.
        self.lap_distance: float = 0.0
        self.sector: int = 0
        self.lap_num: int = 0

    # --- lifecycle ---

    def connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(0.5)
        s.bind((self.bind, self.port))
        self._sock = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, name="f1-udp", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            self._sock.close()
            self._sock = None

    def _pump(self) -> None:
        assert self._sock is not None, "connect() before _pump()"
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if len(data) < HEADER_SIZE:
                continue
            (fmt, _year, _maj, _min, _pver, pid, _uid, _stime,
             _fid, _ofid, player, _second) = _HEADER.unpack_from(data)
            with self._lock:
                self._packet_format = fmt
                self._player_idx = player
                self._latest[pid] = data
                if pid == PID_SESSION_HISTORY and len(data) > HEADER_SIZE:
                    self._history[data[HEADER_SIZE]] = data
                self.packet_counts[pid] = self.packet_counts.get(pid, 0) + 1
                if pid in EXPECTED_SIZES and len(data) != EXPECTED_SIZES[pid]:
                    self.unsupported[pid] = (
                        f"size {len(data)}, expected {EXPECTED_SIZES[pid]}")
                elif pid in self.unsupported:
                    del self.unsupported[pid]

    # --- per-packet slicing ---

    def _car_slice(self, pid: int, item: struct.Struct):
        """Unpack the player's entry out of a 22-car array packet."""
        data = self._latest.get(pid)
        if data is None or pid in self.unsupported:
            return None
        off = HEADER_SIZE + self._player_idx * item.size
        if off + item.size > len(data):
            return None
        return item.unpack_from(data, off)

    def _read_setup(self) -> F1Setup | None:
        data = self._latest.get(PID_CAR_SETUPS)
        if data is None:
            return None
        # Stride is the full CarSetupData size, which we infer from the packet
        # rather than hardcode -- that is the field that moved in F1 24.
        stride = (len(data) - HEADER_SIZE) // MAX_CARS
        off = HEADER_SIZE + self._player_idx * stride
        if stride < _SETUP.size or off + _SETUP.size > len(data):
            return None
        return F1Setup(_SETUP.unpack_from(data, off))

    def read_history(self, car_idx: int | None = None) -> dict | None:
        """Every completed lap for a car, with per-sector splits.

        Returns {laps: [...], best_lap_num, best_s1_lap, best_s2_lap, best_s3_lap}.
        Each lap is {lap, total_ms, s1_ms, s2_ms, s3_ms, valid}. Laps the game has
        not filled in yet (total_ms == 0) are dropped.
        """
        with self._lock:
            idx = self._player_idx if car_idx is None else car_idx
            data = self._history.get(idx)
        if data is None or len(data) < HEADER_SIZE + _HISTORY_HEAD.size:
            return None

        (_car, num_laps, _stints, best_lap, best_s1, best_s2,
         best_s3) = _HISTORY_HEAD.unpack_from(data, HEADER_SIZE)
        base = HEADER_SIZE + _HISTORY_HEAD.size

        laps = []
        for i in range(min(num_laps, 100)):
            off = base + i * _LAP_HISTORY.size
            if off + _LAP_HISTORY.size > len(data):
                break
            (total, s1_ms, s1_min, s2_ms, s2_min, s3_ms, s3_min,
             flags) = _LAP_HISTORY.unpack_from(data, off)
            if not total:
                continue
            laps.append({
                "lap": i + 1,
                "total_ms": int(total),
                "s1_ms": int(s1_ms) + int(s1_min) * 60_000,
                "s2_ms": int(s2_ms) + int(s2_min) * 60_000,
                "s3_ms": int(s3_ms) + int(s3_min) * 60_000,
                # bit 0 = whole lap valid; bits 1-3 = each sector valid
                "valid": bool(flags & 0x01),
            })
        return {"laps": laps, "best_lap_num": best_lap, "best_s1_lap": best_s1,
                "best_s2_lap": best_s2, "best_s3_lap": best_s3}

    def _read_motion_ex(self):
        data = self._latest.get(PID_MOTION_EX)
        if data is None or len(data) < HEADER_SIZE + _MOTION_EX_PREFIX.size:
            return None
        return _MOTION_EX_PREFIX.unpack_from(data, HEADER_SIZE)

    def _read_session(self):
        data = self._latest.get(PID_SESSION)
        if data is None or len(data) < HEADER_SIZE + _SESSION_PREFIX.size:
            return None
        return _SESSION_PREFIX.unpack_from(data, HEADER_SIZE)

    # --- assembly ---

    def read(self) -> Snapshot | None:
        with self._lock:
            tel = self._car_slice(PID_CAR_TELEMETRY, _TELEMETRY)
            lap = self._car_slice(PID_LAP_DATA, _LAP)
            sts = self._car_slice(PID_CAR_STATUS, _STATUS)
            sess = self._read_session()
            mex = self._read_motion_ex()
            self.setup = self._read_setup() or self.setup
            player = self._player_idx

        if tel is None and lap is None:
            return None

        s = Snapshot(valid=True)

        if tel is not None:
            (speed, throttle, steer, brake, clutch, gear, rpm, _drs, _rl, _rlb,
             bt0, bt1, bt2, bt3,
             _st0, _st1, _st2, _st3,
             it0, it1, it2, it3,
             _engine_temp,
             tp0, tp1, tp2, tp3,
             _sf0, _sf1, _sf2, _sf3) = tel
            s.speed_kmh = float(speed)
            s.gas = float(throttle)
            s.brake = float(brake)
            s.steer = float(steer)
            s.clutch = float(clutch) / 100.0
            s.gear = int(gear) + 1        # F1: -1=R,0=N,1..8 -> Snapshot: 0=R,1=N,2=1st
            s.rpm = int(rpm)
            s.brake_temp = _reorder((float(bt0), float(bt1), float(bt2), float(bt3)))
            s.tyre_temp = _reorder((float(it0), float(it1), float(it2), float(it3)))
            s.tyre_pressure = _reorder((tp0, tp1, tp2, tp3))

        if lap is not None:
            last_ms, cur_ms = lap[0], lap[1]
            s.last_time_ms = int(last_ms)
            s.current_time_ms = int(cur_ms)
            lap_distance = float(lap[10])
            self.lap_distance = lap_distance
            self.sector = int(lap[17])
            self.lap_num = int(lap[14])
            s.position = int(lap[13])
            s.completed_laps = max(0, int(lap[14]) - 1)
            pit_status = int(lap[15])
            s.is_in_pit = pit_status == 2       # 0 none, 1 pit lane, 2 in box
            s.is_in_pit_lane = pit_status >= 1
            s.valid_lap = int(lap[18]) == 0
            if self._track_length > 0 and lap_distance >= 0:
                s.normalized_car_position = min(1.0, lap_distance / self._track_length)
            if last_ms and (self._best_ms == 0 or last_ms < self._best_ms):
                self._best_ms = int(last_ms)
            s.best_time_ms = self._best_ms

        if sts is not None:
            (tc_level, abs_level, _fuel_mix, brake_bias, _limiter,
             fuel, _fuel_cap, fuel_laps, max_rpm, _idle, _gears, _drs_ok, _drs_dist,
             _actual_compound, visual_compound, _age, _flags,
             _p_ice, _p_mguk, _ers, _ers_mode, _h_k, _h_h, _deployed, _paused) = sts
            s.tc_level = int(tc_level)
            s.abs_level = int(abs_level)
            s.brake_bias = float(brake_bias)
            s.fuel = float(fuel)
            s.max_rpm = int(max_rpm)
            s.tyre_compound = VISUAL_COMPOUNDS.get(int(visual_compound), "")
            if fuel_laps and fuel_laps > 0:
                s.fuel_per_lap = float(fuel) / float(fuel_laps)

        if sess is not None:
            (_weather, track_temp, air_temp, _laps, track_len,
             session_type, track_id, _formula, time_left, _duration) = sess
            self._track_length = int(track_len)
            s.road_temp = float(track_temp)
            s.air_temp = float(air_temp)
            s.track = TRACK_NAMES.get(int(track_id), f"track#{track_id}")
            s.session_type = SESSION_TYPES.get(int(session_type), "")
            s.session_time_left_ms = int(time_left) * 1000

        if mex is not None:
            s.suspension_travel = _reorder(mex[0:4])
            s.wheel_slip = _reorder(mex[16:20])
            s.slip_angle = _reorder(mex[20:24])

        s.car_model = f"F1 car (idx {player})"
        return s
