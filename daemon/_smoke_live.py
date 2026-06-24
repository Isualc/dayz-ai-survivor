#!/usr/bin/env python3
"""Live-Smoke gegen den laufenden DayZ-Server ueber die File-Bridge.

Default (sicher, keine Welt-Wirkung):
  - State lesen + Eckdaten zeigen
  - Lebenszeichen: seq steigt zwischen zwei Lesungen (Mod tickt)
  - ping: voller Command-Round-trip (Mod liest Befehl, antwortet "done")

Mit Argument "active" zusaetzlich (reversibel, stupst aber einen evtl.
laufenden Agenten an):
  - stop: NPC bleibt stehen (Basis von PlayerHalt)

Aufruf:  python daemon/_smoke_live.py [npc_id] [active]
"""

import sys
import time

sys.path.insert(0, "daemon")
from bridge import Bridge, DEFAULT_PROFILE

NPC = "viktor"
ACTIVE = False
for a in sys.argv[1:]:
    if a == "active":
        ACTIVE = True
    else:
        NPC = a

bridge = Bridge(DEFAULT_PROFILE, NPC)
ok = True


def check(label, cond, detail=""):
    global ok
    mark = "OK  " if cond else "FAIL"
    if not cond:
        ok = False
    print(f"[{mark}] {label} {detail}".rstrip())


s1 = bridge.read_state() or {}
check("State lesbar", bool(s1))
seq1 = s1.get("seq", -1)
npc = s1.get("npc", {})
print(f"     seq={seq1} uptime={s1.get('uptime', 0):.0f}s | "
      f"npc spawned={npc.get('spawned')} alive={npc.get('alive')} "
      f"name={npc.get('name')} hp={npc.get('health', 0):.0f} "
      f"pos={npc.get('pos_x', 0):.0f}/{npc.get('pos_z', 0):.0f}")

time.sleep(2.5)
s2 = bridge.read_state() or {}
seq2 = s2.get("seq", -1)
check("Bridge lebt (seq steigt)", seq2 > seq1, f"({seq1} -> {seq2})")

r = bridge.run("ping", timeout=15)
check("ping round-trip", r.get("status") == "done",
      f"-> {r.get('status')} {r.get('detail') or ''}".rstrip())

if ACTIVE:
    if npc.get("spawned") and npc.get("alive"):
        r = bridge.run("stop", timeout=20)
        check("stop round-trip", r.get("status") in ("done", "running"),
              f"-> {r.get('status')} {r.get('detail') or ''}".rstrip())
    else:
        print("[--  ] stop uebersprungen (kein lebender NPC)")

print("SMOKE LIVE OK" if ok else "SMOKE LIVE: FEHLER")
sys.exit(0 if ok else 1)
