"""Battle-Royale-Monitor: pollt die state-Dateien und loggt Kampf-Telemetrie.

Endet, wenn hoechstens einer der gespawnten Agenten lebt (Sieger) oder
--max-seconds erreicht ist (Zwischenstand).
"""

import argparse
import json
import math
import os
import time
from datetime import datetime

_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
PROFILE = os.path.join(_SERVER_DIR, "profiles", "IsuSurvivor")
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_ids():
    """Aktive Agenten aus arena/active_roster.json (der Supervisor schreibt
    sie bei jedem Start); Fallback: die vier Stamm-Agenten. So erfasst die
    BR-Telemetrie auch dynamische Zusatz-Slots (npc5..npc10)."""
    try:
        path = os.path.join(REPO_DIR, "arena", "active_roster.json")
        with open(path, "r", encoding="utf-8") as f:
            ids = tuple(a["id"] for a in json.load(f)["agents"])
        if ids:
            return ids
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return ("viktor", "birgit", "igor", "konrad")


IDS = _load_ids()


def read_state(npc_id):
    try:
        with open(f"{PROFILE}\\state_{npc_id}.json", "r", encoding="utf-8",
                  errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def stamp():
    return datetime.now().strftime("%H:%M:%S")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-seconds", type=int, default=500)
    args = parser.parse_args()

    last = {i: {} for i in IDS}      # letzter npc-Block je Agent
    was_alive = {i: False for i in IDS}
    dead_order = []                  # (zeit, id) in Todesreihenfolge
    near = set()                     # Paare aktuell im Nahkontakt
    deadline = time.monotonic() + args.max_seconds

    print(f"[{stamp()}] Monitor laeuft (max {args.max_seconds}s)...")
    while time.monotonic() < deadline:
        npcs = {}
        for i in IDS:
            state = read_state(i)
            if state:
                npcs[i] = state.get("npc", {})

        for i, npc in npcs.items():
            prev = last.get(i) or {}
            spawned = bool(npc.get("spawned"))
            alive = bool(npc.get("alive"))

            if spawned and alive and not was_alive[i]:
                was_alive[i] = True
                print(f"[{stamp()}] SPAWN  {i.capitalize()} bei "
                      f"x={npc.get('pos_x', 0):.0f} z={npc.get('pos_z', 0):.0f}")

            if was_alive[i] and prev:
                hands_prev = prev.get("in_hands", "")
                hands_now = npc.get("in_hands", "")
                if hands_now and hands_now != hands_prev:
                    print(f"[{stamp()}] WAFFE  {i.capitalize()}: {hands_now}")

                hp_prev = prev.get("health", 100.0)
                hp_now = npc.get("health", 100.0)
                bl_prev = prev.get("blood", 5000.0)
                bl_now = npc.get("blood", 5000.0)
                if alive and (hp_prev - hp_now > 1.0 or bl_prev - bl_now > 50.0):
                    print(f"[{stamp()}] TREFFER {i.capitalize()}: "
                          f"HP {hp_prev:.0f}->{hp_now:.0f}, "
                          f"Blut {bl_prev:.0f}->{bl_now:.0f}")

                if prev.get("alive") and spawned and not alive:
                    dead_order.append((stamp(), i))
                    print(f"[{stamp()}] +++ {i.capitalize().upper()} IST "
                          f"GEFALLEN (Platz {5 - len(dead_order)}) +++")

            last[i] = dict(npc)

        # Nahkontakte (Paardistanzen der Lebenden)
        live = {i: n for i, n in npcs.items()
                if was_alive[i] and n.get("alive")}
        for a in live:
            for b in live:
                if a >= b:
                    continue
                dx = live[a].get("pos_x", 0) - live[b].get("pos_x", 0)
                dz = live[a].get("pos_z", 0) - live[b].get("pos_z", 0)
                d = math.hypot(dx, dz)
                key = (a, b)
                if d < 80 and key not in near:
                    near.add(key)
                    print(f"[{stamp()}] KONTAKT {a.capitalize()} <-> "
                          f"{b.capitalize()} ({d:.0f} m)")
                elif d > 120 and key in near:
                    near.discard(key)

        spawned_cnt = sum(1 for i in IDS if was_alive[i])
        live_cnt = len(live)
        if spawned_cnt >= 2 and live_cnt <= 1:
            if live_cnt == 1:
                winner = next(iter(live))
                n = live[winner]
                print(f"[{stamp()}] ### SIEGER: {winner.upper()} "
                      f"(HP {n.get('health', 0):.0f}, "
                      f"Blut {n.get('blood', 0):.0f}) ###")
            else:
                print(f"[{stamp()}] ### ALLE GEFALLEN - kein Sieger ###")
            for t, i in dead_order:
                print(f"    [{t}] gefallen: {i.capitalize()}")
            return 0

        time.sleep(10)

    print(f"[{stamp()}] --- Zwischenstand (Zeitlimit) ---")
    for i in IDS:
        npc = last.get(i) or {}
        if not was_alive[i]:
            print(f"    {i.capitalize()}: noch nicht gespawnt")
        elif npc.get("alive"):
            print(f"    {i.capitalize()}: LEBT, HP {npc.get('health', 0):.0f}, "
                  f"Blut {npc.get('blood', 0):.0f}, "
                  f"x={npc.get('pos_x', 0):.0f} z={npc.get('pos_z', 0):.0f}, "
                  f"Hand: {npc.get('in_hands', '-')}")
        else:
            print(f"    {i.capitalize()}: TOT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
