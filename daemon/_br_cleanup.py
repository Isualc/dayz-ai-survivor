"""Verwaiste Agenten-Koerper despawnen (nach hartem Prozess-Kill)."""

import json
import os
import sys

sys.path.insert(0, r"daemon")
from bridge import Bridge, DEFAULT_PROFILE

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ids():
    """Aktive Agenten aus arena/active_roster.json; Fallback: die Stamm-Vier."""
    try:
        with open(os.path.join(_REPO, "arena", "active_roster.json"), "r",
                  encoding="utf-8") as f:
            ids = [a["id"] for a in json.load(f)["agents"]]
        if ids:
            return ids
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return ["viktor", "birgit", "igor", "konrad"]


for npc_id in _ids():
    bridge = Bridge(DEFAULT_PROFILE, npc_id)
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    if npc.get("spawned"):
        r = bridge.run("despawn", timeout=15)
        print(f"{npc_id}: despawn -> {r.get('status')} {r.get('detail') or ''}")
    else:
        print(f"{npc_id}: kein Koerper in der Welt")
