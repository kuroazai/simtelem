"""Live F1 25 UDP probe -- proves the feed works and the byte layout is right.

    python -m simtelem.sources.f1_probe

Run it with the game running and sitting in the garage or on track. It reports
which packets are arriving, whether their sizes match the format we compiled for,
your current setup, and a rolling sample of inputs so you can sanity-check that
throttle/brake move when you press them.
"""
from __future__ import annotations

import sys
import time

from ..models import fmt_laptime
from .f1_source import DEFAULT_PORT, EXPECTED_SIZES, F1UdpSource

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants",
    5: "CarSetups", 6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification",
    9: "LobbyInfo", 10: "CarDamage", 11: "SessionHistory", 12: "TyreSets",
    13: "MotionEx", 14: "TimeTrial", 15: "LapPositions",
}


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    src = F1UdpSource(port=port)
    print(f"Listening on UDP {port} ... (Ctrl-C to stop)")
    try:
        src.connect()
    except OSError as e:
        print(f"Could not bind port {port}: {e}")
        print("Something else may already be listening (another telemetry app?).")
        return

    # Session (id 1) and CarSetups (id 5) broadcast far slower than the physics
    # packets, so wait for those two specifically rather than a packet count --
    # track name, temps and normalized position are all useless without Session.
    deadline = time.time() + 20.0
    while time.time() < deadline:
        src.read()  # pulls setup into src.setup as soon as the packet lands
        if 1 in src.packet_counts and 5 in src.packet_counts:
            break
        time.sleep(0.25)

    if not src.packet_counts:
        print("\nNO PACKETS RECEIVED.")
        print("  Check: Settings -> Telemetry Settings -> UDP Telemetry = On")
        print("         UDP IP = 127.0.0.1, UDP Port = 20777, Format = 2025")
        print("  Also check Windows Firewall isn't blocking Python.")
        src.close()
        return

    print("\n[PACKETS SEEN]")
    for pid in sorted(src.packet_counts):
        name = PACKET_NAMES.get(pid, f"id {pid}")
        note = ""
        if pid in src.unsupported:
            note = f"  <-- LAYOUT MISMATCH: {src.unsupported[pid]}"
        elif pid in EXPECTED_SIZES:
            note = "  (size OK)"
        print(f"   {pid:>3} {name:<20} x{src.packet_counts[pid]:<5}{note}")

    print(f"\n   packet format = {src._packet_format}  (expect 2025)")

    if src.unsupported:
        print("\n   ^ A mismatch means the struct changed this game year. The fields")
        print("     from those packets will be missing until the format is updated.")

    snap = src.read()
    if snap is None:
        print("\nPackets arriving but no player car data yet -- get in the car.")
        src.close()
        return

    print("\n[SESSION]")
    print(f"   track          = {snap.track}")
    print(f"   session        = {snap.session_type or '<unmapped>'}")
    print(f"   air / track    = {snap.air_temp}C / {snap.road_temp}C")
    print(f"   compound       = {snap.tyre_compound or '<unknown>'}")
    print(f"   fuel           = {snap.fuel:.1f}  ({snap.fuel_per_lap:.2f}/lap)")
    print(f"   TC level       = {snap.tc_level}   ABS level = {snap.abs_level}")
    print(f"   brake bias     = {snap.brake_bias}%")
    print(f"   best lap       = {fmt_laptime(snap.best_time_ms)}")

    if src.setup:
        s = src.setup
        print("\n[YOUR CURRENT SETUP]")
        print(f"   wing F/R          = {s.front_wing} / {s.rear_wing}")
        print(f"   diff on/off       = {s.on_throttle_diff}% / {s.off_throttle_diff}%")
        print(f"   camber F/R        = {s.front_camber:.2f} / {s.rear_camber:.2f}")
        print(f"   toe F/R           = {s.front_toe:.2f} / {s.rear_toe:.2f}")
        print(f"   suspension F/R    = {s.front_susp} / {s.rear_susp}")
        print(f"   anti-roll bar F/R = {s.front_arb} / {s.rear_arb}")
        print(f"   ride height F/R   = {s.front_height} / {s.rear_height}")
        print(f"   brake pressure    = {s.brake_pressure}%")
        print(f"   brake bias        = {s.brake_bias}%")
        print(f"   engine braking    = {s.engine_braking}%")
        print(f"   tyre psi FL/FR    = {s.tyre_pressure[0]:.1f} / {s.tyre_pressure[1]:.1f}")
        print(f"   tyre psi RL/RR    = {s.tyre_pressure[2]:.1f} / {s.tyre_pressure[3]:.1f}")
        print(f"   ballast / fuel    = {s.ballast} / {s.fuel_load:.1f}")
    else:
        print("\n[SETUP] not received yet -- open the setup screen once, then re-run.")

    print("\n[LIVE INPUTS ~5s -- press the pedals, these should move]")
    print("   speed  gear  throttle  brake  steer   pos     slipF/R")
    for _ in range(10):
        s2 = src.read()
        if s2:
            fl, fr, rl, rr = s2.wheel_slip
            slip_f = (abs(fl) + abs(fr)) / 2
            slip_r = (abs(rl) + abs(rr)) / 2
            print(f"   {s2.speed_kmh:5.0f}  {s2.gear_label:>4}  "
                  f"{s2.gas:8.2f}  {s2.brake:5.2f}  {s2.steer:5.2f}  "
                  f"{s2.normalized_car_position:5.3f}   {slip_f:.2f}/{slip_r:.2f}")
        time.sleep(0.5)

    src.close()

    print("\n[VERDICT]")
    print("   Throttle/brake should read 0.00-1.00 and track your pedals.")
    print("   'pos' should climb 0->1 around the lap (needs the Session packet).")
    print("   If any column looks like nonsense, the struct for that packet is off.")


if __name__ == "__main__":
    main()
