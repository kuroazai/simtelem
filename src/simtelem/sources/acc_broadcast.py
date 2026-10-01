"""ACC Broadcasting API client — the live timing tower.

Shared memory only ever describes YOUR car. Every rival timing field
(position, lap times, gaps, driver names, car model) comes from this separate
UDP protocol, which ACC serves on the port in Config/broadcasting.json.

Protocol notes:
  * Register, then the sim pushes REALTIME_UPDATE and one REALTIME_CAR_UPDATE
    per car at the requested interval.
  * A car update arrives for cars we have not seen in the entry list yet; the
    correct response is to re-request the entry list, not to guess.
  * Strings are uint16 length + UTF-8. Everything is little-endian.
  * Packets are parsed defensively: ACC has added trailing fields between
    releases, so we read what we need and ignore the rest rather than
    demanding an exact length.

    python -m simtelem.sources.acc_broadcast
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from typing import Dict, List, Optional

# --- outbound message types ---
REGISTER = 1
UNREGISTER = 9
REQUEST_ENTRY_LIST = 10
REQUEST_TRACK_DATA = 11

# --- inbound message types ---
REGISTRATION_RESULT = 1
REALTIME_UPDATE = 2
REALTIME_CAR_UPDATE = 3
ENTRY_LIST = 4
TRACK_DATA = 5
ENTRY_LIST_CAR = 6
BROADCASTING_EVENT = 7

PROTOCOL_VERSION = 4

CAR_LOCATION = {0: "none", 1: "track", 2: "pitlane", 3: "pit entry", 4: "pit exit"}
SESSION_TYPE = {0: "practice", 4: "qualifying", 9: "superpole", 10: "race",
                11: "hotlap", 12: "hotstint", 13: "hotlap superpole", 14: "replay"}

# ACC car model ids. Only needs to be right enough to answer "same car as me?" -
# that comparison is numeric; the names are for display.
CAR_MODELS = {
    0: "Porsche 991 GT3 R", 1: "Mercedes-AMG GT3", 2: "Ferrari 488 GT3",
    3: "Audi R8 LMS", 4: "Lamborghini Huracan GT3", 5: "McLaren 650S GT3",
    6: "Nissan GT-R Nismo 2018", 7: "BMW M6 GT3", 8: "Bentley Continental 2018",
    9: "Porsche 991 II GT3 Cup", 10: "Nissan GT-R Nismo 2015",
    11: "Bentley Continental 2015", 12: "Aston Martin V12 Vantage GT3",
    13: "Lamborghini Gallardo R-EX", 14: "Jaguar G3", 15: "Lexus RC F GT3",
    16: "Lamborghini Huracan Evo 2019", 17: "Honda NSX GT3",
    18: "Lamborghini Huracan SuperTrofeo", 19: "Audi R8 LMS Evo 2019",
    20: "Aston Martin V8 Vantage 2019", 21: "Honda NSX Evo 2019",
    22: "McLaren 720S GT3 2019", 23: "Porsche 911 II GT3 R 2019",
    24: "Ferrari 488 GT3 Evo 2020", 25: "Mercedes-AMG GT3 Evo 2020",
    26: "Ferrari 488 Challenge Evo", 27: "BMW M2 CS Racing",
    28: "Porsche 911 GT3 Cup 2021", 29: "Lamborghini Huracan ST Evo2",
    30: "BMW M4 GT3", 31: "Audi R8 LMS GT3 Evo II", 32: "Ferrari 296 GT3",
    33: "Lamborghini Huracan Evo2", 34: "Porsche 992 GT3 R",
    35: "McLaren 720S GT3 Evo 2023", 36: "Ford Mustang GT3",
}


class _Reader:
    """Little-endian cursor with ACC's uint16-length-prefixed strings."""

    def __init__(self, data: bytes) -> None:
        self.d, self.i = data, 0

    def _take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise EOFError("packet truncated")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def u8(self) -> int:   return self._take(1)[0]
    def i8(self) -> int:   return struct.unpack("<b", self._take(1))[0]
    def u16(self) -> int:  return struct.unpack("<H", self._take(2))[0]
    def i16(self) -> int:  return struct.unpack("<h", self._take(2))[0]
    def i32(self) -> int:  return struct.unpack("<i", self._take(4))[0]
    def f32(self) -> float: return struct.unpack("<f", self._take(4))[0]

    def string(self) -> str:
        n = self.u16()
        return self._take(n).decode("utf-8", errors="replace")

    def lap(self) -> Dict:
        ms = self.i32()
        car = self.u16()
        drv = self.u16()
        splits = [self.i32() for _ in range(self.u8())]
        invalid = bool(self.u8())
        valid_for_best = bool(self.u8())
        out_lap = bool(self.u8())
        in_lap = bool(self.u8())
        # ACC uses int32 max for "no lap"
        if ms > 3_600_000 or ms <= 0:
            ms = None
        return {"ms": ms, "car": car, "driver": drv, "splits": splits,
                "invalid": invalid, "valid_for_best": valid_for_best,
                "out_lap": out_lap, "in_lap": in_lap}


def _write_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def default_config() -> Dict:
    p = os.path.join(os.path.expanduser("~"), "Documents",
                     "Assetto Corsa Competizione", "Config", "broadcasting.json")
    try:
        with open(p, encoding="utf-8") as f:
            j = json.loads(f.read().replace("\x00", ""))
        return {"port": int(j.get("updListenerPort", 9000)),
                "password": j.get("connectionPassword", ""),
                "command_password": j.get("commandPassword", "")}
    except Exception:
        return {"port": 9000, "password": "asd", "command_password": ""}


class ACCBroadcast:
    """Live classification for the whole field."""

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None,
                 password: Optional[str] = None,
                 name: str = "KuroRaceEngineer", interval_ms: int = 250) -> None:
        cfg = default_config()
        self.addr = (host, port or cfg["port"])
        self.password = cfg["password"] if password is None else password
        self.command_password = cfg["command_password"]
        self.name = name
        self.interval = interval_ms

        self.lock = threading.Lock()
        self.connected = False
        self.connection_id = -1
        self.error = ""
        self.session: Dict = {}
        self.entries: Dict[int, Dict] = {}     # carIndex -> static info
        self.cars: Dict[int, Dict] = {}        # carIndex -> live state
        self.focused_car: Optional[int] = None
        self._sock: Optional[socket.socket] = None
        self._stop = False
        self._last_entry_req = 0.0

    # ---------- lifecycle ----------
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        try:
            if self._sock and self.connection_id >= 0:
                self._sock.sendto(bytes([UNREGISTER]) +
                                  struct.pack("<i", self.connection_id), self.addr)
        except Exception:
            pass

    def _run(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(1.0)
            self._register()
        except Exception as e:
            self.error = f"socket: {e}"
            return
        last_ping = time.monotonic()
        while not self._stop:
            try:
                data, _ = self._sock.recvfrom(65536)
                self._handle(data)
            except socket.timeout:
                # nothing arriving: the sim may have restarted, so re-register
                if time.monotonic() - last_ping > 5:
                    self.connected = False
                    self._register()
                    last_ping = time.monotonic()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                time.sleep(0.5)

    def _register(self) -> None:
        msg = (bytes([REGISTER, PROTOCOL_VERSION])
               + _write_string(self.name)
               + _write_string(self.password)
               + struct.pack("<i", self.interval)
               + _write_string(self.command_password))
        self._sock.sendto(msg, self.addr)

    def _request_entry_list(self) -> None:
        now = time.monotonic()
        if self.connection_id < 0 or now - self._last_entry_req < 2.0:
            return
        self._last_entry_req = now
        # Only REGISTER carries the protocol-version byte. Follow-up commands
        # are msgType + connectionId; sending a version byte here makes ACC
        # silently discard the request and no entry list ever arrives.
        self._sock.sendto(bytes([REQUEST_ENTRY_LIST])
                          + struct.pack("<i", self.connection_id), self.addr)

    # ---------- parsing ----------
    def _handle(self, data: bytes) -> None:
        r = _Reader(data)
        try:
            kind = r.u8()
            if kind == REGISTRATION_RESULT:
                self.connection_id = r.i32()
                ok = bool(r.u8())
                r.u8()                      # read-only flag
                err = r.string()
                self.connected = ok
                self.error = "" if ok else (err or "registration refused")
                if ok:
                    self._request_entry_list()
            elif kind == REALTIME_UPDATE:
                self._realtime(r)
            elif kind == REALTIME_CAR_UPDATE:
                self._car(r)
            elif kind == ENTRY_LIST:
                r.i32()
                for _ in range(r.u16()):
                    idx = r.u16()
                    with self.lock:
                        self.entries.setdefault(idx, {"index": idx})
            elif kind == ENTRY_LIST_CAR:
                self._entry_car(r)
        except EOFError:
            pass                            # short packet: ignore this one
        except Exception as e:
            self.error = f"parse: {type(e).__name__}: {e}"

    def _realtime(self, r: _Reader) -> None:
        ev, si = r.u16(), r.u16()
        stype, phase = r.u8(), r.u8()
        s_time, s_end = r.f32(), r.f32()
        focus = r.i32()
        r.string(); r.string(); r.string()   # camera set, camera, hud page
        if r.u8():                           # replay playing
            r.f32(); r.f32()
        tod = r.f32()
        amb, trk = r.u8(), r.u8()
        best = None
        try:
            r.u8(); r.u8(); r.u8()           # clouds, rain, wetness
            best = r.lap()
        except EOFError:
            pass
        with self.lock:
            self.focused_car = focus
            self.session = {
                "type": SESSION_TYPE.get(stype, str(stype)),
                "phase": phase, "time_s": round(s_time / 1000, 1),
                "remaining_s": round(s_end / 1000, 1),
                "time_of_day": tod, "air_temp": amb, "track_temp": trk,
                "best_lap_ms": (best or {}).get("ms"),
                "best_lap_car": (best or {}).get("car"),
            }

    def _car(self, r: _Reader) -> None:
        idx, drv = r.u16(), r.u16()
        r.u8()                               # driver count
        gear = r.i8() - 1
        r.f32(); r.f32()                     # world x, y (shared memory is better)
        r.f32()                              # yaw
        loc = r.u8()
        kmh = r.u16()
        pos, cup, track_pos = r.u16(), r.u16(), r.u16()
        spline = r.f32()
        laps = r.u16()
        delta = r.i32()
        best, last, cur = r.lap(), r.lap(), r.lap()
        with self.lock:
            if idx not in self.entries:
                self._request_entry_list()
            self.cars[idx] = {
                "index": idx, "driver_index": drv, "gear": gear,
                "location": CAR_LOCATION.get(loc, str(loc)),
                "kmh": kmh, "position": pos, "cup_position": cup,
                "track_position": track_pos, "spline": round(spline, 4),
                "laps": laps, "delta_ms": delta,
                "best_ms": best["ms"], "last_ms": last["ms"],
                "last_invalid": last["invalid"],
                "current_ms": cur["ms"], "updated": time.monotonic(),
            }

    def _entry_car(self, r: _Reader) -> None:
        idx = r.u16()
        model = r.u8()
        team = r.string()
        number = r.i32()
        r.u8()                               # cup category
        # currentDriverIndex is a BYTE. Reading it as int32 eats three extra
        # bytes and misaligns every driver name that follows.
        cur_drv = r.u8()
        r.u16()                              # nationality
        drivers = []
        try:
            for _ in range(r.u8()):
                first, last, short = r.string(), r.string(), r.string()
                r.u8(); r.u16()              # category, nationality
                drivers.append({"first": first, "last": last, "short": short})
        except EOFError:
            pass
        with self.lock:
            self.entries[idx] = {
                "index": idx, "model": model,
                "car": CAR_MODELS.get(model, f"model {model}"),
                "team": team, "number": number,
                "driver_index": cur_drv, "drivers": drivers,
                "driver": (f"{drivers[cur_drv]['first']} {drivers[cur_drv]['last']}"
                           if 0 <= cur_drv < len(drivers) else ""),
            }

    # ---------- consumption ----------
    def classification(self) -> List[Dict]:
        """Whole field, sorted by position. Stale cars are dropped."""
        now = time.monotonic()
        with self.lock:
            rows = []
            for idx, c in self.cars.items():
                if now - c["updated"] > 5:
                    continue
                e = self.entries.get(idx, {})
                rows.append({**c,
                             "car": e.get("car", ""), "model": e.get("model"),
                             "driver": e.get("driver", ""), "team": e.get("team", ""),
                             "number": e.get("number"),
                             "is_me": idx == self.focused_car})
        rows.sort(key=lambda x: (x["position"] if x["position"] > 0 else 999))
        return rows

    def state(self) -> Dict:
        with self.lock:
            sess = dict(self.session)
            n = len(self.cars)
        return {"connected": self.connected, "error": self.error,
                "session": sess, "car_count": n,
                "focused_car": self.focused_car,
                "classification": self.classification()}


def main() -> None:
    b = ACCBroadcast()
    print(f"connecting to {b.addr} …")
    b.start()
    try:
        for _ in range(40):
            time.sleep(0.5)
            st = b.state()
            if not st["connected"]:
                continue
            cls = st["classification"]
            print(f"\n[{st['session'].get('type','?')}] cars={st['car_count']}")
            print(f"{'P':>3} {'#':>4} {'driver':<22} {'car':<28} {'best':>10} {'last':>10}")
            for c in cls[:12]:
                fmt = lambda v: (f"{v//60000}:{(v%60000)/1000:06.3f}" if v else "-")
                print(f"{c['position']:>3} {c['number'] or '':>4} "
                      f"{c['driver'][:22]:<22} {c['car'][:28]:<28} "
                      f"{fmt(c['best_ms']):>10} {fmt(c['last_ms']):>10}"
                      f"{'  <= you' if c['is_me'] else ''}")
            if cls:
                break
        else:
            print("no data — is ACC running with a session loaded?")
            print("error:", b.error or "(none)")
    finally:
        b.stop()


if __name__ == "__main__":
    main()
