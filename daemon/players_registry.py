#!/usr/bin/env python3
"""IsuSurvivor Mitspieler-Registry — arena/players.json lesen und Namen aufloesen.

Kleine, abhaengigkeitsfreie Datei (nur stdlib), damit dayz_mcp.py und
voice_router.py sie ohne Risiko per try/except importieren koennen. Ein
Mensch kann im Funk anders heissen (funk, z.B. "Isualc") als im DayZ-Profil
(dayz, GetIdentity, z.B. "Clausi") und zusaetzliche Spitznamen haben
(aliases). resolve() matcht case-insensitiv gegen alle drei Felder.

Format arena/players.json: {"players": [{"funk":..., "dayz":..., "aliases":
[...], "discord_id":...}, ...]}. Fehlt die Datei oder ist sie kaputt/leer,
liefert alles hier einfach eine leere Liste - kein Crash, kein Log-Spam
(Schnittstellenvertrag: tolerant lesen).
"""

import json
import os

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
PLAYERS_FILE = os.path.join(REPO_DIR, "arena", "players.json")


def _load_raw() -> list:
    """Roheintraege aus players.json, oder [] bei Fehlen/Fehler. Akzeptiert
    sowohl {"players": [...]} als auch eine nackte Liste [...] als Root,
    falls die Datei mal von Hand anders angelegt wird."""
    try:
        with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("players", [])
    else:
        entries = []
    return [e for e in entries if isinstance(e, dict)]


def all_players() -> list:
    """Alle bekannten Mitspieler-Eintraege (Rohdaten, Reihenfolge wie Datei)."""
    return _load_raw()


def resolve(name: str):
    """Sucht 'name' case-insensitiv gegen funk, dayz und aliases eines
    Eintrags und liefert den ganzen Eintrag (dict) oder None. Leerer/None
    Name liefert immer None."""
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for entry in _load_raw():
        candidates = [entry.get("funk"), entry.get("dayz")]
        candidates.extend(entry.get("aliases") or [])
        for c in candidates:
            if isinstance(c, str) and c.strip().lower() == needle:
                return entry
    return None


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:] or ["Isualc", "Clausi", "Claus", "Nobody"]:
        print(f"{arg!r} -> {resolve(arg)}")
    print(f"all_players(): {all_players()}")
