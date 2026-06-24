"""Verwaiste Agenten-Koerper despawnen (nach hartem Prozess-Kill)."""

import sys

sys.path.insert(0, r"daemon")
from bridge import Bridge, DEFAULT_PROFILE

for npc_id in ("viktor", "birgit", "igor", "konrad"):
    bridge = Bridge(DEFAULT_PROFILE, npc_id)
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    if npc.get("spawned"):
        r = bridge.run("despawn", timeout=15)
        print(f"{npc_id}: despawn -> {r.get('status')} {r.get('detail') or ''}")
    else:
        print(f"{npc_id}: kein Koerper in der Welt")
