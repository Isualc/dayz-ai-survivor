#!/usr/bin/env python3
"""IsuSurvivor MCP-Server — stellt die DayZ-Bridge als Werkzeuge fuer Claude bereit.

Wird von Claude Code als stdio-MCP-Server gestartet (siehe run_agent.py).
Alle Werkzeuge blockieren bis zum Ergebnis (mit Zeitdeckel) und geben
deutschen Klartext zurueck.
"""

import argparse
import difflib
import json
import math
import os
import random
import threading
import time

from mcp.server.fastmcp import FastMCP

import bridge as bridge_mod
from bridge import Bridge, format_observation, inventory_signature, DEFAULT_PROFILE
import tactics

# Mitspieler-Registry (Schnittstelle 5): loest Funk-/Alias-Namen auf den
# DayZ-Profilnamen auf. Abhaengigkeitsfrei; fehlt sie, faellt der Namens-
# Resolver auf das bestehende Isualc->Clausi-Verhalten zurueck.
try:
    import players_registry
except ImportError:
    players_registry = None

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDSETS_FILE = os.path.join(REPO_DIR, "voice", "soundsets.json")
# VOICE_OUTBOX wird nach dem Argument-Parsing pro Agent gesetzt (jeder Bot
# hat seine eigene Sprech-Warteschlange in seinem agent_home)
VOICE_OUTBOX = os.path.join(REPO_DIR, "agent_home", "voice_outbox.jsonl")
_VOICE_CATALOG: dict | None = None


def _agent_home(npc_id: str) -> str:
    import agent_paths
    return agent_paths.agent_home_dir(npc_id)


def _outbox(entry: dict) -> None:
    """Aeusserung fuer die Discord-Voice-Bruecke ablegen (best effort)."""
    try:
        with open(VOICE_OUTBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _voice_catalog() -> dict:
    global _VOICE_CATALOG
    if _VOICE_CATALOG is None:
        try:
            with open(SOUNDSETS_FILE, "r", encoding="utf-8") as f:
                _VOICE_CATALOG = json.load(f)
        except FileNotFoundError:
            _VOICE_CATALOG = {}
    return _VOICE_CATALOG

parser = argparse.ArgumentParser()
parser.add_argument("--profile", default=DEFAULT_PROFILE)
parser.add_argument("--npc-id", default="viktor")
parser.add_argument("--agent-name", default="Viktor")
parser.add_argument("--voice", default="", help="ElevenLabs-Stimme fuer Discord-TTS")
parser.add_argument("--language", default="de",
                    help="Ausgabe-Sprache der NPC; steuert die Bildschirm-Transliteration")
parser.add_argument("--outbox", default="",
                    help="voice_outbox.jsonl (Default: agent_home-Konvention)")
args = parser.parse_args()

BRIDGE = Bridge(args.profile, args.npc_id)
AGENT_NAME = args.agent_name
AGENT_VOICE = args.voice
# Ausgabe-Sprache: steuert, ob der ON-SCREEN-Text (Chat/Comic/Gedankenzeile)
# fuer nicht-lateinische Schriften (CJK/Arabisch/Hindi/Kyrillisch/Griechisch)
# auf ASCII latinisiert wird, weil der Stock-Font sie nicht zeichnet. Der
# Audio-/TTS-Pfad bekommt weiter den Originaltext. Latein bleibt unangetastet.
AGENT_LANG = (args.language or "de").strip().lower()
import transliterate
VOICE_OUTBOX = args.outbox or os.path.join(_agent_home(args.npc_id), "voice_outbox.jsonl")
# Funk-Inbox (Geschwister der Outbox): lange Aktionen brechen ab, sobald hier
# neuer Funk auftaucht, damit der Agent sofort auf den Spieler reagiert.
VOICE_INBOX = os.path.join(os.path.dirname(VOICE_OUTBOX), "voice_inbox.jsonl")
BRIDGE.voice_inbox = VOICE_INBOX
LAST_CHAT_ID = 0
LAST_INV_SIG = None  # Inventar-Kennung des letzten observe (Delta-Erkennung)

# Gemeinsamer Klartext-Funkkanal (alle NPCs): wird ein Funkspruch fuer den
# Bildschirm latinisiert (Nicht-Latein-Sprache), legen wir hier die NATIVE
# Fassung ab - so liest ein ANDERER NPC den Originaltext statt das verstuemmelte
# Transliterat (Pinyin/Buckwalter). Key = der latinisierte Text (exakte
# Zuordnung beim Empfaenger). Die Bildschirm-/Audio-Pfade bleiben unveraendert.
RADIO_NATIVE = os.path.join(REPO_DIR, "arena", "radio_native.jsonl")

# equip-Circuit-Breaker (geteilt von equip_best UND equip_melee, beide rufen
# denselben Mod-"equip"-Befehl): aufeinanderfolgende Fehlschlaege, damit der NPC
# nicht stur dieselbe scheiternde Aktion wiederholt - z.B. die Spawn-Race
# "Equip-Ziel verschwunden", die equip_melee bisher in Schleife lief.
_EQUIP_FAILS = 0
_EQUIP_LAST_FAIL = 0.0

# Bewegungs-Circuit-Breaker (gleiches Muster wie _EQUIP_FAILS): das Audit vom
# 03.07. zeigte 352-s-Zuege mit 13 LLM-Schritten aus flee/unstick/move_to-
# Retry-Schleifen (Igor 12:11). Das LLM soll solche Schleifen NICHT selbst
# fahren - nach 2 Fehlschlaegen/Versuchen liefert der dritte Aufruf nur noch
# eine Anleitung (travel_to hat das Wall-Following) statt ein Bridge-Kommando.
_MOVE_FAILS = 0        # move_to-"failed" in Folge (Fenster 120 s)
_MOVE_LAST_FAIL = 0.0
_UNSTICK_CALLS = 0     # unstick-AUFRUFE (nicht Fehlschlaege) im 90-s-Fenster
_UNSTICK_LAST = 0.0


def _radio_native(ascii_text: str, native_text: str) -> None:
    """Native Fassung eines latinisierten Funkspruchs in den gemeinsamen Kanal
    schreiben, damit andere NPCs das Original lesen. Nur bei echter Latinisierung."""
    try:
        os.makedirs(os.path.dirname(RADIO_NATIVE), exist_ok=True)
        # Wachstum begrenzen: bei >512 KB neu anfangen (reiner Info-Funk, per
        # ascii-Key zugeordnet - alte Zeilen sind danach hoechstens harmlos weg).
        try:
            if os.path.getsize(RADIO_NATIVE) > 512 * 1024:
                open(RADIO_NATIVE, "w", encoding="utf-8").close()
        except OSError:
            pass
        with open(RADIO_NATIVE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ascii": ascii_text, "native": native_text,
                                "sender": AGENT_NAME, "t": time.time()},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass

# Name des menschlichen Spielers - zentrale, konfigurierbare Variable.
# ISU_MIC_NAME: wie der Spieler im FUNK/Voice erscheint (gleiche Env wie
#   mic_listener.py/voice_router.py - Single Source of Truth, Default "Player").
# ISU_PLAYER_NAME: optional sein DayZ-PROFILNAME (GetIdentity, wie in observe),
#   falls er vom Funk-Namen abweicht ("Clausi"). Leer = live aus der Welt holen.
# Die Bots hoeren Befehle unter dem Funk-Namen, follow/regroup/give_to der Mod
# brauchen aber den Profilnamen - _resolve_player_name uebersetzt zwischen beiden.
PLAYER_RADIO_NAME = os.environ.get("ISU_MIC_NAME", "Player")
PLAYER_PROFILE_NAME = os.environ.get("ISU_PLAYER_NAME", "")

mcp = FastMCP("dayz")


# ------------------------------------------------------------ Alias-Normalizer
# Zentrale Stelle, die falsch geratene Parameternamen und triviale Wertfehler
# auffaengt, BEVOR ein Tool die Bridge anspricht. Vorbild ist das bestehende
# pickup-Alias-Muster (classname/item_name/item/name). Korrekte Aufrufe aendern
# sich nicht: _first nimmt einfach den ersten nicht-leeren Kandidaten, und
# _match_classname laesst einen Wert unveraendert, solange kein besserer
# Classname aus dem letzten State bekannt ist.

# Bekannte Classnames aus dem letzten observe (Inventar + Umgebung). Fuer die
# case-insensitive/Teilstring-Korrektur eines vertippten Classnames.
KNOWN_CLASSNAMES: set[str] = set()

# Uebersetzung falsch geratener Parameternamen auf den kanonischen Namen. Rein
# dokumentarisch/als Referenz - die Tools falten ihre Aliase ueber _first, diese
# Tabelle haelt die vereinbarte Zuordnung an einer Stelle fest.
ALIAS_PARAMS = {
    "player_name": ("player_name", "player", "target", "name"),
    "classname": ("classname", "item_name", "item", "object", "name"),
    "message": ("message", "text", "content", "msg"),
}


def _refresh_known_classnames(state: dict | None) -> None:
    """KNOWN_CLASSNAMES aus einem frischen State neu aufbauen (Inventar +
    sichtbare Bodenitems). Best effort - fehlt etwas, bleibt die Menge klein."""
    if not state:
        return
    names: set[str] = set()
    for it in state.get("inventory", []):
        cn = it.get("classname")
        if cn:
            names.add(cn)
    for e in state.get("nearby", []):
        cn = e.get("classname")
        if cn:
            names.add(cn)
    if names:
        KNOWN_CLASSNAMES.clear()
        KNOWN_CLASSNAMES.update(names)


def _first(*cands: str) -> str:
    """Ersten nicht-leeren Kandidaten liefern (Alias-Faltung)."""
    for c in cands:
        if c:
            return c.strip() if isinstance(c, str) else c
    return ""


def _match_classname(needle: str) -> str:
    """Einen (evtl. vertippten) Classname gegen die bekannten Classnames aus dem
    letzten State abgleichen: erst exakt, dann case-insensitiv, dann eindeutiger
    Teilstring. Findet sich nichts Eindeutiges, bleibt der Wert unveraendert -
    die Mod macht ohnehin ihr eigenes Teilstring-Matching, wir wollen korrekte
    Aufrufe nicht verschlimmbessern."""
    n = (needle or "").strip()
    if not n or not KNOWN_CLASSNAMES:
        return n
    if n in KNOWN_CLASSNAMES:
        return n
    low = n.lower()
    for c in KNOWN_CLASSNAMES:          # case-insensitiver Exakttreffer
        if c.lower() == low:
            return c
    subs = [c for c in KNOWN_CLASSNAMES if low in c.lower()]
    if len(subs) == 1:                  # eindeutiger Teilstring -> korrigieren
        return subs[0]
    if not subs:                        # umgekehrt: bekannter Name im needle
        contained = [c for c in KNOWN_CLASSNAMES if c.lower() in low]
        if len(contained) == 1:
            return contained[0]
    return n                            # mehrdeutig/unbekannt -> unveraendert


def _needle(*cands: str) -> str:
    """Zentrale Item-Argument-Aufloesung: Alias falten + Classname korrigieren."""
    raw = _first(*cands)
    return _match_classname(raw) if raw else raw


def _nearest_classnames(needle: str, n: int = 3) -> list[str]:
    """Die n naechstliegenden bekannten Classnames (difflib) zu einem Wert."""
    if not needle or not KNOWN_CLASSNAMES:
        return []
    return difflib.get_close_matches(needle, sorted(KNOWN_CLASSNAMES),
                                     n=n, cutoff=0.3)


def _did_you_mean(needle: str) -> str:
    """Anhaengsel ' Meintest du: a, b, c?' fuer eine unaufloesbare Item-Angabe,
    oder "" wenn es keine nahen Kandidaten gibt."""
    cands = _nearest_classnames(needle)
    if not cands:
        return ""
    return " Meintest du: " + ", ".join(cands) + "?"


# ------------------------------------------------------- Krankheit / Medizin
# Erreger -> passendes Vanilla-Medikament (verifiziert gegen
# reference/dayz-vanilla .../transmissionagents/agents/*.c):
#   cholera/influenza/wound: ANTIBIOTICS-Resistenz 0 (bzw. 0.5 ab Stufe 1) ->
#     TetracyclineAntibiotics wirkt.
#   salmonella: ANTIBIOTICS-Resistenz 1 -> Antibiotika nutzlos, aber
#     CharcoalTablets toeten Erreger generisch (CharcoalMdfr, ~2.85 Agents/s).
#   brain: ANTIBIOTICS-Resistenz 1, kein Medikament (nur Zeit/Ruhe).
DISEASE_MED = {
    "cholera": "TetracyclineAntibiotics",
    "influenza": "TetracyclineAntibiotics",
    "wound": "TetracyclineAntibiotics",
    "salmonella": "CharcoalTablets",
    "brain": None,
}


def _inv_classnames(state: dict | None) -> list[str]:
    if not state:
        return []
    return [i.get("classname", "") for i in state.get("inventory", [])]


def _have_item(state: dict | None, needle: str) -> bool:
    """True, wenn ein Inventar-Classname needle als Teilstring enthaelt."""
    low = (needle or "").lower()
    return any(low in cn.lower() for cn in _inv_classnames(state) if cn)


# --------------------------------------------------------------- Fernreise
# travel_to startet einen Hintergrund-Thread, der move_to-Segmente (~150 m)
# Richtung Fernziel kettet, damit der Zug SOFORT enden kann (Token-Disziplin).
# Der Thread haelt die intent-Datei aktuell und meldet Ankunft/Abbruch ueber
# travel_event_<id>.json (Schnittstelle 1). stop()/follow()/regroup()/move_to
# brechen eine laufende Reise ab; ein neues travel_to ersetzt das alte.
_TRAVEL_SEG = 150.0        # Segmentlaenge in m
_TRAVEL_ARRIVE = 6.0       # Ankunftsschwelle in m
_travel_lock = threading.Lock()
_travel_thread: threading.Thread | None = None
_travel_stop: threading.Event | None = None


def _write_intent_line(text: str) -> None:
    """intent_<id>.txt schreiben (Nameplate-Gedankenzeile), bildschirmtauglich
    latinisiert und auf Laenge gekappt. Best effort."""
    native = " ".join((text or "").split())
    if not native:
        return
    line = transliterate.to_screen(native, AGENT_LANG)
    if len(line) > 77:
        line = line[:74] + "..."
    path = os.path.join(BRIDGE.dir, f"intent_{BRIDGE.npc_id}.txt")
    try:
        os.makedirs(BRIDGE.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(line + "\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _write_travel_event(event: str, x: float, z: float, detail: str = "") -> None:
    """travel_event_<id>.json neben intent_<id>.txt ablegen (Schnittstelle 1).
    run_agent-EventWatcher liest, loescht die Datei und weckt den Agenten."""
    path = os.path.join(BRIDGE.dir, f"travel_event_{BRIDGE.npc_id}.json")
    payload = {"event": event, "x": float(x), "z": float(z), "detail": detail}
    try:
        os.makedirs(BRIDGE.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# Hindernis-Umgehung (Wall-Following): Die Luftlinien-Segmente scheitern an
# Grosshindernissen (See, Militaerzaun, Sumpf) - eAI-Pathfinding findet
# innerhalb EINES 150-m-Segments keinen Weg drumherum, meldet "45s ohne
# Fortschritt", und der NPC wirkt fuer den Spieler "festgeglitcht" (Igor
# 03.07. suedlich vom Livonia-Lager). Der Thread weicht deshalb selbst aus,
# wie Igor es manuell tat ("nach Sueden komme ich raus, jetzt westlich
# herum"): quer zur Ziellinie (90/135 Grad), bleibt der einmal gefundenen
# Seite treu und probt den Zielkurs mit wachsendem Abstand (jede blockierte
# Probe kostet im Spiel 45 s, darum exponentiell 1-2-4 Segmente).
_TRAVEL_DETOUR_ANGLES = (90.0, 135.0)   # Grad quer zur Ziellinie
_TRAVEL_DETOUR_SEG = 200.0              # laengere Ausweich-Segmente
_TRAVEL_SEG_TIMEOUT = 60.0    # s pro Zielkurs-Segment (150 m): die Mod meldet
                              # "45 s ohne Fortschritt" ohnehin frueher, 60 s
                              # decken den normalen Lauf plus Puffer (vorher 90 -
                              # blockierte Proben kosteten unnoetig lange).
_TRAVEL_DETOUR_TIMEOUT = 75.0  # s pro Ausweich-Segment: 200 m statt 150 m Weg,
                               # also proportional mehr Laufzeit-Puffer noetig.
_TRAVEL_MAX_SEGMENTS = 400              # Notbremse gegen Endlos-Pendeln
_TRAVEL_MAX_DETOUR = 2500.0             # max. Quergang pro Hindernis-SEITE (m);
                                        # grosse Livonia-Seen sind ~1-2 km breit.
                                        # Beide Seiten erschoepft -> stuck-Event:
                                        # das soll das GEHIRN entscheiden
                                        # (grosser Umweg/anderes Ziel).


def _travel_worker(gx: float, gz: float, stop: threading.Event) -> None:
    """Kettet move_to-Segmente Richtung (gx,gz). Fortschritt = der NPC hat
    sich >=30 m bewegt (auch seitlich - Umwege zaehlen). Bei Blockade: erst
    unstick, dann Wall-Following quer zur Ziellinie. Erst wenn beide Seiten
    scheitern, geht ein stuck-Event ans Gehirn. Hintergrund-Thread."""
    segments = 0
    committed = 0        # zuletzt erfolgreiche Hindernis-Seite (nur Reihenfolge)
    backoff = 1          # Ausweich-Segmente pro Zielkurs-Probe (1-2-4)
    straight_ok = 0      # Zielkurs-Erfolge in Folge
    unstick_used = False
    excluded: set = set()  # Seiten ohne Durchkommen (Netto-Versatz erschoepft)
    block_d = None       # Zieldistanz beim ERSTEN Block: das Hindernis gilt
                         # erst als ueberwunden, wenn wir DEUTLICH naeher am
                         # Ziel sind (Marge 150 m). Zwei freie Geradeaus-
                         # Segmente allein reichen nicht - unterhalb einer
                         # diagonalen Wand kann man endlos "erfolgreich"
                         # parallel laufen, ohne je vorbeizukommen.
    block_pos = None     # Position des ersten Blocks
    block_n = None       # Einheitsvektor quer zur Ziellinie am Block (Seite +1)
    d_best = None        # beste je erreichte Zieldistanz am Hindernis
    stagnation = 0       # Wall-Follow-Runden ohne Annaeherung (Taschen-Falle)

    def _lateral(px_: float, pz_: float) -> float:
        """Netto-Seitenversatz zur Blockstelle, quer zur Ziellinie. Immun
        gegen Zickzack: nur der ECHTE Querabstand zaehlt gegen das Budget,
        nicht der gelaufene Weg (der Zickzack aus Quergang + Probe frisst
        sonst das Budget der richtigen Seite auf, bevor sie herumfuehrt)."""
        return ((px_ - block_pos[0]) * block_n[0]
                + (pz_ - block_pos[1]) * block_n[1])

    def _pos():
        st = BRIDGE.read_state() or {}
        n = st.get("npc", {})
        if not (n.get("alive") and n.get("spawned")):
            return None
        return float(n.get("pos_x", 0.0)), float(n.get("pos_z", 0.0))

    def _segment(bearing: float, seg: float,
                 timeout: float = _TRAVEL_SEG_TIMEOUT) -> float:
        """Ein move_to-Segment fahren; liefert die zurueckgelegte Distanz.
        Fortschritt misst der WEG, nicht die Zieldistanz (ein Umweg um einen
        See entfernt sich zeitweise vom Ziel und zaehlt trotzdem)."""
        nonlocal segments
        segments += 1
        p = _pos()
        if p is None:
            return -1.0
        # stop_event: _abort_travel bricht ein LAUFENDES Segment sofort ab -
        # ohne das hing der Worker bis zu 75 s im Segment, waehrend das
        # naechste Tool schon sein eigenes Bewegungskommando feuerte (Zucken
        # zwischen zwei Zielen).
        BRIDGE.run("move_to", x=p[0] + math.cos(bearing) * seg,
                   z=p[1] + math.sin(bearing) * seg, timeout=timeout,
                   stop_event=stop)
        q = _pos()
        if q is None:
            return -1.0
        return math.hypot(q[0] - p[0], q[1] - p[1])

    while not stop.is_set():
        if segments > _TRAVEL_MAX_SEGMENTS:
            _write_travel_event("aborted", gx, gz,
                                "Segment-Limit erreicht (Endlos-Pendeln?)")
            return
        p = _pos()
        if p is None:
            _write_travel_event("aborted", gx, gz, "kein Koerper in der Welt")
            return
        px, pz = p
        d = math.hypot(gx - px, gz - pz)
        if d <= _TRAVEL_ARRIVE:
            _write_intent_line(f"angekommen bei {gx:.0f}/{gz:.0f}")
            _write_travel_event("arrived", gx, gz, f"Distanz {d:.0f} m")
            return
        goal_bearing = math.atan2(gz - pz, gx - px)

        # 1) Zielkurs (gerade aufs Ziel)
        _write_intent_line(f"unterwegs nach {gx:.0f}/{gz:.0f}, noch {d:.0f} m")
        moved = _segment(goal_bearing, min(_TRAVEL_SEG, d))
        if moved < 0:
            _write_travel_event("aborted", gx, gz, "kein Koerper in der Welt")
            return
        if stop.is_set():
            return
        if moved >= 30.0:
            straight_ok += 1
            unstick_used = False
            passed = block_d is None or d < block_d - 150.0
            if straight_ok >= 2 and passed:   # Hindernis liegt WIRKLICH hinter uns
                committed = 0
                backoff = 1
                excluded = set()
                block_d = None
                block_pos = None
                block_n = None
                d_best = None
                stagnation = 0
            continue
        straight_ok = 0
        if block_d is None:
            block_d = d
            block_pos = (px, pz)
            # Quer-Einheitsvektor (Seite +1 = goal_bearing + 90 Grad)
            block_n = (-math.sin(goal_bearing), math.cos(goal_bearing))
            d_best = d

        # 2) Blockiert: einmal pro Hindernis aufrichten/befreien und den
        #    Zielkurs wiederholen (haeufigster Fall: Gelaende-Verkeilung).
        if not unstick_used:
            unstick_used = True
            BRIDGE.run("unstick", timeout=10)
            continue

        # 3) Wall-Following: bekannte Seite (committed) zuerst, Sackgassen-
        #    Seiten (Budget erschoepft) sind ausgeschlossen; pro Seite 90 dann
        #    135 Grad. 'backoff' Segmente am Stueck, damit nicht nach jedem
        #    Quergang eine teure 45-s-Probe verpufft.
        sides = [committed] if committed else [1, -1]
        if committed:
            sides.append(-committed)
        sides = [s for s in sides if s not in excluded]
        success_side = 0
        for side in sides:
            for ang in _TRAVEL_DETOUR_ANGLES:
                bearing = goal_bearing + math.radians(side * ang)
                _write_intent_line(f"Hindernis voraus - weiche aus "
                                   f"({side * ang:+.0f} Grad)")
                moved = _segment(bearing, _TRAVEL_DETOUR_SEG,
                                 timeout=_TRAVEL_DETOUR_TIMEOUT)
                if moved < 0:
                    _write_travel_event("aborted", gx, gz,
                                        "kein Koerper in der Welt")
                    return
                if stop.is_set():
                    return
                if moved >= 30.0:
                    success_side = side
                    # restliche backoff-Segmente auf derselben Linie
                    for _ in range(backoff - 1):
                        if stop.is_set():
                            return
                        if _segment(bearing, _TRAVEL_DETOUR_SEG,
                                    timeout=_TRAVEL_DETOUR_TIMEOUT) < 30.0:
                            break
                    break
            if success_side:
                break
        # Bilanz der Wall-Follow-Runde: Netto-Seitenversatz und Annaeherung.
        q = _pos()
        if q is None:
            _write_travel_event("aborted", gx, gz, "kein Koerper in der Welt")
            return
        lat = _lateral(q[0], q[1])
        qd = math.hypot(gx - q[0], gz - q[1])
        if qd < (d_best or qd) - 50.0:
            d_best = qd
            stagnation = 0
        else:
            stagnation += 1
        if lat > _TRAVEL_MAX_DETOUR and 1 not in excluded:
            excluded.add(1)
            committed = 0
            backoff = 1
            stagnation = 0
            _write_intent_line("Sackgasse auf dieser Seite - drehe um")
        if lat < -_TRAVEL_MAX_DETOUR and -1 not in excluded:
            excluded.add(-1)
            committed = 0
            backoff = 1
            stagnation = 0
            _write_intent_line("Sackgasse auf dieser Seite - drehe um")
        if len(excluded) >= 2 or stagnation >= 15:
            success_side = 0   # aussichtslos -> stuck-Event unten
        if not success_side:
            q = _pos() or (px, pz)
            nd = math.hypot(gx - q[0], gz - q[1])
            _write_intent_line(f"komme nicht weiter Richtung {gx:.0f}/{gz:.0f}")
            _write_travel_event(
                "stuck", gx, gz,
                f"blockiert trotz Ausweichversuchen (beide Seiten, 90/135 "
                f"Grad), noch {nd:.0f} m. Grosses Hindernis (See/Zaun/Sumpf)? "
                f"Waehle einen deutlichen Umweg (travel_to mit Zwischenziel) "
                f"oder ein anderes Ziel.")
            return
        committed = success_side
        backoff = min(backoff * 2, 4)


def _abort_travel() -> None:
    """Laufende Reise abbrechen (stop/follow/regroup/move_to greifen darauf zu).

    Wartet kurz auf das ECHTE Ende des Reise-Threads: der steigt dank
    stop_event binnen ~1 s aus einem laufenden Segment aus. Ohne das Join
    feuerte das naechste Tool sein Bewegungskommando, waehrend der Worker
    noch eines in der Mailbox hatte."""
    global _travel_thread, _travel_stop
    with _travel_lock:
        t = _travel_thread
        if _travel_stop is not None:
            _travel_stop.set()
        _travel_thread = None
        _travel_stop = None
    if t is not None and t.is_alive():
        t.join(timeout=3.0)


def _observe_text(full: bool = False) -> str:
    global LAST_CHAT_ID, LAST_INV_SIG
    state = BRIDGE.read_state()
    _refresh_known_classnames(state)
    sig = inventory_signature(state)
    # Inventar nur ausschreiben, wenn es sich seit dem letzten observe
    # geaendert hat (oder full angefordert ist) - das spart spuerbar Tokens
    inv_unchanged = (not full) and LAST_INV_SIG is not None and sig == LAST_INV_SIG
    LAST_INV_SIG = sig
    text, LAST_CHAT_ID = format_observation(
        state, LAST_CHAT_ID, inv_unchanged=inv_unchanged, compact=not full)
    return text


def _outcome(result: dict, success_text: str, needle: str = "") -> str:
    status = result.get("status", "unbekannt")
    detail = result.get("detail") or ""
    if status == "done":
        return f"{success_text} {detail}".rstrip()
    if status == "interrupted":
        return ("ABGEBROCHEN: Der Spieler funkt dich gerade an. Hoer SOFORT zu "
                "und reagiere auf seinen Funk, bevor du weitermachst.")
    if status == "running":
        dist = result.get("dist_to_target", -1.0)
        if dist is not None and dist >= 0:
            return (f"Laeuft noch (Distanz {dist:.0f} m). Mit observe() pruefen "
                    f"oder wait() nutzen.")
        return "Laeuft noch. Mit observe() pruefen oder wait() nutzen."
    if status not in ("done", "failed", "interrupted"):
        # Timeout/keine Antwort: der Server fuehrt den Befehl evtl. NOCH aus.
        # Das frueher gemeldete "Fehlgeschlagen:" (ohne Detail) liess den
        # Agenten dieselbe Aktion sofort neu starten -> Doppelbefehle.
        return ("KEINE RUECKMELDUNG vom Server binnen der Wartezeit - die "
                "Aktion laeuft moeglicherweise noch. NICHT sofort wiederholen: "
                "pruefe erst mit observe, was daraus geworden ist.")
    # Unaufloesbare Item-Angabe: naechstliegende bekannte Classnames anbieten
    # (nur wenn der needle NICHT ohnehin ein bekannter Classname ist - dann
    # liegt der Fehler woanders und der Vorschlag waere nur Rauschen).
    hint = ""
    if needle and needle not in KNOWN_CLASSNAMES:
        hint = _did_you_mean(needle)
    return f"Fehlgeschlagen: {detail}{hint}"


@mcp.tool()
def observe(full: bool = False) -> str:
    """Aktuelle Lage ansehen: Position, Vitalwerte, Hand, Inventar, Umgebung,
    neue Chat-Nachrichten und Status der letzten Aktion. Immer zuerst aufrufen,
    wenn du die Lage nicht sicher kennst. Unveraendertes Inventar wird als
    Einzeiler gezeigt; observe(full=true) erzwingt die volle Liste."""
    return _observe_text(full=full)


@mcp.tool()
def intent(text: str) -> str:
    """Setze deine aktuelle Absicht in EINER kurzen deutschen Zeile (z.B.
    "Wasser knapp, ich gehe zum Brunnen"). Sie schwebt als Gedanke ueber
    deinem Kopf, ist KEIN hoerbarer Funkspruch. Echte Umlaute, hoechstens
    ~8 Woerter. Blockiert nicht - setze sie locker vor groesseren Schritten."""
    native = " ".join(text.split())
    # Bildschirm-Fassung latinisieren (Font kann kein CJK/...), dann auf Laenge
    # kappen. Die NATIVE Fassung legen wir separat ab (intent_native_<id>.txt),
    # damit der Orchestrator-Sitrep den Originalvorsatz an andere NPCs funkt,
    # nicht das verstuemmelte Transliterat.
    line = transliterate.to_screen(native, AGENT_LANG)
    if len(line) > 77:
        line = line[:74] + "..."
    path = os.path.join(BRIDGE.dir, f"intent_{BRIDGE.npc_id}.txt")
    try:
        os.makedirs(BRIDGE.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(line + "\n")
        os.replace(tmp, path)
        if line != native:
            npath = os.path.join(BRIDGE.dir, f"intent_native_{BRIDGE.npc_id}.txt")
            ntmp = npath + ".tmp"
            nline = native if len(native) <= 120 else native[:117] + "..."
            with open(ntmp, "w", encoding="utf-8") as f:
                f.write(nline + "\n")
            os.replace(ntmp, npath)
    except OSError:
        return "Absicht konnte nicht gesetzt werden."
    return f"Absicht gesetzt: {line}"


@mcp.tool()
def move_to(x: float, z: float) -> str:
    """Zu Map-Koordinaten laufen (x = West-Ost, z = Sued-Nord, wie auf iZurvive).
    Der NPC laeuft serverseitig WEITER, auch wenn "Laeuft noch" zurueckkommt.
    Bei "Laeuft noch" NICHT sofort wiederholen - beende deinen Zug. Du laeufst
    automatisch weiter und pruefst beim naechsten Aufwachen die neue Position.
    Mehrfaches move_to aufs selbe Ziel im selben Zug kostet nur Tokens und bringt
    nichts. Unerreichbare Ziele schlagen nach 45 s ohne Fortschritt fehl.
    Fuer WEITE Strecken oder wenn Hindernisse im Weg liegen: travel_to nehmen -
    das weicht Hindernissen selbst aus und laeuft im Hintergrund."""
    global _MOVE_FAILS, _MOVE_LAST_FAIL
    # Fenster abgelaufen -> Zaehler zuruecksetzen. Sonst bleibt er auf 2 stehen
    # und EIN weiterer Fehlschlag (auch Stunden spaeter) sperrt sofort wieder
    # fuer 120 s, obwohl es kein "in Folge" mehr ist.
    if _MOVE_FAILS and (time.time() - _MOVE_LAST_FAIL) >= 120.0:
        _MOVE_FAILS = 0
    # Circuit-Breaker (Muster _EQUIP_FAILS): nach 2 "failed" binnen 120 s NICHT
    # ein drittes Mal stur wiederholen - Anleitung statt Bridge-Kommando, sonst
    # verbrennt der NPC 300+-s-Zuege in move_to/unstick-Schleifen (Audit 03.07.).
    if _MOVE_FAILS >= 2 and (time.time() - _MOVE_LAST_FAIL) < 120.0:
        return ("move_to hakt hier - es ist gerade 2x in Folge fehlgeschlagen. "
                "Ruf es JETZT nicht noch einmal. Nutze travel_to (weicht "
                "Hindernissen selbst aus), unstick, oder waehle ein deutlich "
                "anderes Ziel. Beende deinen Zug.")
    # Schon am Ziel? Dann KEINEN neuen Marsch starten - sonst feuert das Gehirn
    # minutenlang identische move_to auf einen bereits erreichten Punkt (jeder
    # Lauf meldet sofort "Angekommen", das LLM wiederholt endlos). Schwelle 4 m
    # liegt knapp ueber der Mod-Ankunftsschwelle (3 m, BR 2 m).
    # WICHTIG: der Check laeuft VOR _abort_travel - ein move_to auf den eigenen
    # Standpunkt darf eine laufende Fernreise nicht still killen.
    snap = BRIDGE.read_state() or {}
    npc = snap.get("npc", {})
    if npc.get("alive") and npc.get("spawned"):
        dx = npc.get("pos_x", 0.0) - float(x)
        dz = npc.get("pos_z", 0.0) - float(z)
        if dx * dx + dz * dz <= 16.0:   # <= 4 m
            d = (dx * dx + dz * dz) ** 0.5
            return (f"Du bist bereits am Ziel (Distanz {d:.0f} m) - kein erneuter "
                    f"Marsch noetig. Mach etwas anderes oder beende den Zug.")
    # Manuelles move_to hat Vorrang vor einer laufenden Fernreise (sonst kaempfen
    # zwei Bewegungsbefehle um dieselbe Mailbox).
    _abort_travel()
    # Timeout 50 s: die Mod gibt nach 45 s ohne Fortschritt ohnehin auf; der
    # "Laeuft noch"-Fall blockiert den Agenten so 25 s kuerzer als frueher (75 s)
    # und er bleibt fuer den Spieler ansprechbar.
    result = BRIDGE.run("move_to", x=x, z=z, timeout=50, interruptible=True)
    status = result.get("status")
    if status == "failed":
        _MOVE_FAILS += 1
        _MOVE_LAST_FAIL = time.time()   # nach dem Lauf messen, nicht davor
    elif status == "done":
        _MOVE_FAILS = 0   # Erfolg -> Zaehler zuruecksetzen ("running" zaehlt nicht)
    return _outcome(result, "Angekommen.")


@mcp.tool()
def pickup(classname: str = "", item_name: str = "", item: str = "",
           name: str = "") -> str:
    """Bodenitem im Umkreis von 50 m aufheben (hinlaufen inklusive). Nimm den
    classname so wie in observe() unter UMGEBUNG (kind=item) - ein Teilstring
    reicht auch (z.B. "WolfSteak" fuer "WolfSteakMeat"). Beispiel:
    pickup(classname="Apple"). WARNUNG: NUR ganz ohne Argument wird das
    naechste BELIEBIGE Item genommen - gib also immer einen classname an, sonst
    hebst du irgendwas auf."""
    needle = _needle(classname, item_name, item, name)
    # Bewegungs-Tool uebernimmt die Beine - eine laufende Fernreise wuerde sonst
    # um die Mailbox konkurrieren und faelschlich "stecken geblieben" melden
    # (Birgits Fehlabbruch 03.07.). Das Gehirn startet travel_to danach neu.
    _abort_travel()
    result = BRIDGE.run("pickup", text=needle, timeout=60, interruptible=True)
    out = _outcome(result, "Aufgehoben:", needle=needle)
    # Munitionskiste sofort aufmachen - sonst zeigt sie "x0" und wirkt leer,
    # NPCs werfen sie dann irrtuemlich weg statt die Munition zu nutzen.
    detail = result.get("detail") or ""
    if result.get("status") == "done" and ("AmmoBox" in detail or needle.startswith("AmmoBox")):
        BRIDGE.run("unpack_ammo", text="", timeout=10)
        out = out + " (Munitionskiste gleich geoeffnet)"
    return out


@mcp.tool()
def eat(classname: str = "", item_name: str = "", item: str = "",
        name: str = "") -> str:
    """Etwas Essbares aus dem Inventar essen. OHNE Argument das erste Essbare;
    MIT classname gezielt dieses Item (Teilstring reicht, z.B. "Apple" oder
    "WolfSteak") - dann wird NUR das gegessen, nicht irgendwas anderes.
    Verschlossene Konserven werden automatisch geoeffnet (braucht Dosenoeffner
    oder Messer). Schlaegt fehl, wenn das gewuenschte Essen nicht da ist."""
    needle = _needle(classname, item_name, item, name)
    result = BRIDGE.run("eat", text=needle, timeout=30)
    return _outcome(result, "Gegessen:", needle=needle)


@mcp.tool()
def drink(classname: str = "", item_name: str = "", item: str = "",
          name: str = "") -> str:
    """Aus einem Getraenk im Inventar trinken. OHNE Argument das erste; MIT
    classname gezielt dieses (Teilstring reicht). Verschlossene Dosen werden
    vorher geoeffnet. Schlaegt fehl, wenn nichts Passendes da ist."""
    needle = _needle(classname, item_name, item, name)
    result = BRIDGE.run("drink", text=needle, timeout=30)
    return _outcome(result, "Getrunken:", needle=needle)


@mcp.tool()
def harvest(animal: str = "") -> str:
    """Tierkadaver verwerten (Jagd): laeuft zum naechsten toten Tier im
    Umkreis von 50 m (kind=animal_corpse in der Umgebung), zerlegt es und
    nimmt das Fleisch ins Inventar (rohes Fleisch vor dem Essen am Feuer
    braten!). Braucht ein Schneidwerkzeug (Messer/Machete/Axt) im Inventar.
    Optional animal=Teil des Klassennamens (z.B. "Cervus" fuer Hirsch)."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    result = BRIDGE.run("harvest", text=animal, timeout=90, interruptible=True)
    return _outcome(result, "Zerlegt:")


@mcp.tool()
def hunt(animal: str = "") -> str:
    """JAGEN (ganze Folgekette): lebendes Beutetier (Reh, Wildschwein, Kuh,
    Hase, Huhn - kind=animal in observe) anpirschen, erlegen und gleich
    zerlegen. Mit schussbereiter Feuerwaffe faellt das Tier auf 35 m; ohne
    musst du auf 4 m heran (klappt nur bei Huhn/Hase). Optional
    animal=Teil des Klassennamens (z.B. "Cervus" fuer Hirsch). Raubtiere
    (Wolf/Baer) sind Kampf: engage. Danach process_food oder cook_meal -
    rohes Fleisch macht krank."""
    _abort_travel()   # Pirsch uebernimmt die Beine - Fernreise abbrechen
    return tactics.hunt(BRIDGE, animal=animal, log=lambda m: None)


@mcp.tool()
def process_food() -> str:
    """NAHRUNG VERARBEITEN (ganze Folgekette): alle Tierkadaver in 50 m
    zerlegen (harvest) und dann alles Rohe am Feuer garen - Feuer wird bei
    Bedarf selbst gebaut (5x WoodenStick + 1x Rag) und angezuendet
    (Zuendmittel noetig). Der eine Aufruf nach Jagd (hunt) oder Angeln
    (fish). Danach ist das Fleisch essbar (eat)."""
    _abort_travel()   # laeuft zu Kadavern/Feuer - Fernreise abbrechen
    return tactics.process_food(BRIDGE, log=lambda m: None)


@mcp.tool()
def dress_best() -> str:
    """KLEIDUNG systematisch optimieren: prueft jeden Koerper-Slot (Jacke,
    Hose, Schuhe, Kopf, Handschuhe, Weste, Rucksack) und zieht das beste
    verfuegbare Stueck an - aus dem Inventar oder vom Boden in 10 m.
    FRIERST du (VITALS/Waerme), zaehlt WAERME; ist die Lage moderat, zaehlt
    STAURAUM (mehr Slots). Belegte Slots werden getauscht, Inhalt bleibt
    erhalten. Das Kleidungs-Gegenstueck zu equip_best."""
    _abort_travel()   # wear kann bis 10 m laufen - Fernreise abbrechen
    return tactics.dress_best(BRIDGE, log=lambda m: None)


@mcp.tool()
def combine(a: str = "", b: str = "", item_a: str = "", item_b: str = "") -> str:
    """Zwei Gegenstaende KOMBINIEREN (Herstellung): sucht das Rezept, das
    genau aus diesen beiden Materialien besteht, und craftet es. Beispiele:
    combine(a="WoodenStick", b="Rag") -> Torch; combine(a="LongWoodenStick",
    b="Rope") -> FishingRod; combine(a="SmallStone", b="SmallStone") ->
    StoneKnife. Kennt kein Rezept die Kombination, bekommst du passende
    Vorschlaege. Alle Rezepte: recipes; Neues lernen: learn_recipe."""
    na = _needle(a, item_a)
    nb = _needle(b, item_b)
    return tactics.combine_items(BRIDGE, na, nb, log=lambda m: None)


@mcp.tool()
def wear(classname: str = "", item_name: str = "", item: str = "",
         name: str = "") -> str:
    """Kleidungsstueck anziehen (gegen Kaelte - die VITALS zeigen, ob du
    frierst). Sucht im Inventar UND am Boden im Umkreis von 10 m, ein
    vorheriges pickup ist nicht noetig. Ist der Koerper-Slot belegt, wird
    automatisch getauscht: das alte Stueck landet am Boden."""
    needle = _needle(classname, item_name, item, name)
    if not needle:
        return "Bitte classname angeben, z.B. wear(classname=\"BeanieHat\")."
    _abort_travel()   # kann bis 10 m zum Bodenstueck laufen - Fernreise abbrechen
    result = BRIDGE.run("wear", text=needle, timeout=20)
    return _outcome(result, "Angezogen:", needle=needle)


@mcp.tool()
def equip_best() -> str:
    """Die beste verfuegbare Waffe aus dem Inventar in die Hand nehmen.
    Beruecksichtigt Waffenqualitaet und ob ein passendes Magazin da ist;
    ohne Munition wird notfalls eine Nahkampfwaffe gewaehlt."""
    global _EQUIP_FAILS, _EQUIP_LAST_FAIL
    now = time.time()
    # Circuit-Breaker: nach 2 Fehlschlaegen in Folge (innerhalb 90 s) NICHT ein
    # drittes Mal stur wiederholen, sondern zum Strategiewechsel zwingen - sonst
    # verbrennt der NPC Zuege mit derselben scheiternden Aktion (Konrad 20:28).
    if _EQUIP_FAILS >= 2 and (now - _EQUIP_LAST_FAIL) < 90.0:
        return ("equip_best ist gerade 2x in Folge gescheitert - ruf es JETZT nicht "
                "noch einmal. Loese es anders: observe (liegt die Waffe/Munition "
                "wirklich vor dir?), passende Munition suchen/nachladen, die Waffe "
                "per pickup in die Hand nehmen, oder funk dem Spieler dein Problem. "
                "Erst wieder equip_best, wenn sich die Lage geaendert hat.")
    # Waffenwahl macht die MOD (CmdEquipBest): sie kennt den ECHTEN
    # Munitionsstand (Expansion_HasAmmo statt Namens-Heuristik) und verifiziert
    # per Tick, dass die Waffe wirklich in der Hand liegt. Die Python-Heuristik
    # (tactics.pick_best_weapon) bleibt nur fuer die Loot-Bewertung im Einsatz.
    result = BRIDGE.run("equip_best", timeout=30)
    detail = result.get("detail") or ""
    if result.get("status") == "done":
        _EQUIP_FAILS = 0
        return f"Ausgeruestet: {detail}"
    if "keine brauchbare Waffe" in detail:
        _EQUIP_FAILS = 0   # kein transienter Glitch, sondern echte Leere
        return "Keine brauchbare Waffe im Inventar (ruinierte zaehlen nicht)."
    _EQUIP_FAILS += 1
    _EQUIP_LAST_FAIL = now
    return _outcome(result, "Ausgeruestet:")


@mcp.tool()
def equip_melee() -> str:
    """Die beste NAHKAMPFWAFFE (Machete, Axt, Messer ...) in die Hand nehmen -
    auch wenn du eine geladene Schusswaffe haettest. SO kaempfst du, wenn du
    ALLEIN bist und nur ein, zwei Infizierte da sind: Nahkampf ist leise, spart
    Munition und lockt KEINE weitere Horde an (ein Schuss zieht schnell 5-10
    weitere an, die dich allein toeten). Danach engage. Bei Uebermacht (mehrere
    gleichzeitig), gegen Raubtiere (Wolf/Baer) oder wenn dein Squad bei dir ist
    (macht ohnehin Laerm), nimm lieber equip_best (Schusswaffe)."""
    global _EQUIP_FAILS, _EQUIP_LAST_FAIL
    now = time.time()
    # Gleicher Circuit-Breaker wie equip_best: nach 2 Fehlschlaegen in Folge
    # (z.B. "Equip-Ziel verschwunden" direkt nach Spawn) nicht stur wiederholen.
    if _EQUIP_FAILS >= 2 and (now - _EQUIP_LAST_FAIL) < 90.0:
        return ("equip ist gerade 2x in Folge gescheitert - ruf es JETZT nicht "
                "noch einmal. Loese es anders: observe (liegt die Waffe wirklich "
                "vor dir?), die Waffe per pickup in die Hand nehmen, oder funk dem "
                "Spieler dein Problem. Erst wieder versuchen, wenn sich die Lage "
                "geaendert hat.")
    result = tactics.equip_melee(BRIDGE, log=lambda m: None)
    if "fehlgeschlagen" in result.lower():
        _EQUIP_FAILS += 1
        _EQUIP_LAST_FAIL = now
    else:
        _EQUIP_FAILS = 0
    return result


@mcp.tool()
def clean_weapon() -> str:
    """Waffe in der Hand reinigen: repariert sie auf neuwertig und loest eine
    Ladehemmung (Klemmer). Verbraucht ein WeaponCleaningKit aus dem Inventar,
    falls vorhanden. Vorher equip_best, damit die Waffe in der Hand ist."""
    result = BRIDGE.run("clean_weapon", timeout=20)
    return _outcome(result, "Waffe gereinigt:")


@mcp.tool()
def unpack_ammo(classname: str = "") -> str:
    """Eine Munitionskiste (AmmoBox_*) im Inventar aufmachen und die Munition
    herausnehmen. Ohne classname die erste gefundene Box. Die Munition landet
    als Stapel im Inventar (oder am Boden, wenn voll), die Box wird vernichtet.
    Du musst die Box NICHT in die Hand nehmen - das laeuft direkt."""
    result = BRIDGE.run("unpack_ammo", text=classname, timeout=15)
    return _outcome(result, "Entpackt:")


@mcp.tool()
def reload() -> str:
    """Lose Munition (Ammo_*-Stapel) in passende Magazine und in Waffen mit
    internem Magazin (Mosin, SKS, Flinten) umladen - fuer die Waffe in deiner
    Hand (sonst die beste im Inventar). SO wird aus gelooteter Munition
    Feuerkraft: AmmoBox erst mit unpack_ammo oeffnen, dann reload. Das
    Magazin-WECHSELN im Gefecht passiert automatisch, sobald ein gefuelltes
    Magazin im Inventar liegt. Sag dir observe, dass deine Waffe UNGELADEN
    ist? Dann reload."""
    result = BRIDGE.run("reload", timeout=20)
    return _outcome(result, "Nachgeladen:")


@mcp.tool()
def loot_area(max_items: int = 6) -> str:
    """Sichtbare lohnende Bodenitems in der Umgebung einsammeln (bis zu
    max_items, bewertet nach Nutzen: Waffen-Upgrades, Munition, Medizin,
    Nahrung, Werkzeug; Schrott bleibt liegen). Laeuft selbststaendig hin,
    sammelt ein und ruestet danach die beste Waffe aus. Kann mehrere Minuten
    dauern. Nur in sicherer Lage nutzen."""
    max_items = max(1, min(12, int(max_items)))
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    result = tactics.loot_area(BRIDGE, max_items=max_items, log=lambda m: None)
    haul = result["haul"]
    parts = []
    if result.get("aborted"):
        parts.append("ABGEBROCHEN: " + result["aborted"])
    if haul:
        parts.append("Eingesammelt: " + ", ".join(haul))
    elif not result.get("aborted"):
        parts.append("Nichts Lohnendes in Sichtweite gefunden.")
    if result["failed"]:
        parts.append("Nicht erreichbar: " + ", ".join(result["failed"]))
    # Aufgehobene Munitionskisten gleich aufmachen, sonst bleibt die Mun drin.
    boxes = [cn for cn in haul if cn.startswith("AmmoBox")]
    for cn in boxes:
        BRIDGE.run("unpack_ammo", text=cn, timeout=10)
    if boxes:
        parts.append(f"Munitionskisten geoeffnet: {len(boxes)}")
    # Folgekette Nachladen: frisch gelootete lose Munition gleich in Magazine/
    # Waffe umladen (best effort - ohne passende Muni meldet die Mod das nur).
    if boxes or any(cn.startswith("Ammo") or cn.startswith("Mag_") for cn in haul):
        rl = BRIDGE.run("reload", timeout=20)
        if rl.get("status") == "done":
            parts.append("Nachgeladen: " + (rl.get("detail") or ""))
    # Solo-Melee-Praeferenz respektieren: haelt der NPC bewusst eine
    # Nahkampfwaffe, sie NICHT ungefragt gegen die Schusswaffe tauschen
    # (das setzte die equip_melee-Wahl bei jedem Looten still zurueck).
    snap2 = BRIDGE.read_state() or {}
    hands = snap2.get("npc", {}).get("in_hands", "")
    if hands and tactics.classify_melee(hands) and not tactics.is_tool(hands):
        parts.append(f"Nahkampfwaffe bleibt in der Hand: {hands}")
    else:
        eq = BRIDGE.run("equip_best", timeout=30)
        parts.append(_outcome(eq, "Ausgeruestet:"))
    return "\n".join(parts)


@mcp.tool()
def engage() -> str:
    """Den naechsten Infizierten (max. 100 m) angreifen: hinlaufen, das
    Kampfsystem uebernimmt auf kurze Distanz. Blockiert bis das Ziel tot ist
    oder 3 Minuten vergangen sind. Nur einsetzen, wenn du kaempfen willst."""
    # Schutz gegen Selbstmord: mit leeren Haenden gegen Infizierte ist fast
    # sicherer Tod. Erst eine Waffe ziehen (equip_best) oder fliehen (flee).
    snap = BRIDGE.read_state() or {}
    if not snap.get("npc", {}).get("in_hands", ""):
        return ("ABGEBROCHEN: nichts in der Hand. Mit blossen Faeusten gegen "
                "Infizierte ist toedlich - erst equip_best (Waffe ziehen), "
                "und wenn keine Waffe da ist, flee.")
    # Leere Feuerwaffe in der Hand = Nahkampf mit dem Gewehrkolben = fast
    # sicherer Tod. Die Inventarliste traegt das in_hands-Flag + quantity.
    held = None
    for it in snap.get("inventory", []):
        if it.get("in_hands"):
            held = it
            break
    if held and tactics.classify_weapon(held.get("classname", "")) \
            and not tactics.classify_melee(held.get("classname", "")) \
            and held.get("quantity", 0) <= 0:
        return ("ABGEBROCHEN: deine " + held.get("classname", "Waffe") + " ist "
                "LEER. Hast du lose Munition/Magazine: reload. Sonst "
                "Nahkampfwaffe ziehen (equip_melee) - mit leerem Gewehr "
                "angreifen ist fast sicherer Tod.")
    _abort_travel()   # Kampf uebernimmt die Beine - Fernreise abbrechen
    result = BRIDGE.run("engage", timeout=180)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "kein Gegner" in detail:
        return ("Kein Gegner (Infizierter/Raubtier) in Sicht. NICHT blind engage "
                "wiederholen - erst mit observe pruefen, was in der Naehe ist. "
                "Ist nichts da, geh weiter (move_to/explore_step) oder mach etwas "
                "anderes.")
    out = _outcome(result, "Kampf beendet:")
    # Folgekette Nachladen: nach dem Gefecht lose Munition in Magazine/Waffe
    # umladen, solange es ruhig ist - im NAECHSTEN Kampf zaehlt jede Patrone.
    # Best effort: ohne Feuerwaffe/Munition meldet die Mod das nur.
    if result.get("status") == "done":
        rl = BRIDGE.run("reload", timeout=20)
        if rl.get("status") == "done":
            out += " Nachgeladen: " + (rl.get("detail") or "")
    return out


@mcp.tool()
def flee() -> str:
    """150 m vom naechsten Infizierten wegsprinten (oder in Blickrichtung,
    wenn keiner in der Naehe ist)."""
    _abort_travel()   # Flucht uebernimmt die Beine - Fernreise abbrechen
    result = BRIDGE.run("flee", timeout=90)
    return _outcome(result, "In Sicherheit.")


def _discord_active() -> bool:
    """True, wenn der Discord-Voice-Bot fuer diesen Agenten verbunden ist.
    Der Bot legt discord_active.flag neben die voice_outbox, sobald er im
    Sprachkanal ist. Dann sprechen die NPCs nur per Voice - der In-Game-Chat
    wuerde sonst dieselbe Zeile doppelt zeigen."""
    flag = os.path.join(os.path.dirname(VOICE_OUTBOX), "discord_active.flag")
    return os.path.exists(flag)


def _emit_bubble(text: str) -> None:
    """Comic-Sprechblase ans HUD funken (RPC-only, kein Chat). Im Discord-Pfad
    laeuft CmdSay nicht, also schickt nur dieser Aufruf die Blase; ohne Discord
    macht CmdSay das selbst mit. Das HUD zeigt sie nur, wenn Comic-Chat an ist."""
    try:
        BRIDGE.run("bubble", text=text, timeout=10)
    except Exception:
        pass


# Schutz gegen Doppel-Aeusserungen: das Gehirn ruft dieselbe Zeile manchmal
# zweimal kurz hintereinander (Begruessung doppelt). Identischer Text innerhalb
# von 15 s wird einmal unterdrueckt - gilt fuer say und say_voice gemeinsam.
_LAST_SAID = {"text": "", "t": 0.0}


def _is_repeat(text: str) -> bool:
    now = time.time()
    t = (text or "").strip()
    if t and t == _LAST_SAID["text"] and (now - _LAST_SAID["t"]) < 15.0:
        return True
    _LAST_SAID["text"] = t
    _LAST_SAID["t"] = now
    return False


# Funkregel: NUR Aeusserungen, die den SPIELER direkt ansprechen (seinen Namen
# nennen), werden vertont (ElevenLabs-TTS im Discord). Funk untereinander (NPC
# an NPC) bleibt Text im In-Game-Chat - das spart Stimm-Kontingent und Spam.
PLAYER_NAMES = [n.strip().lower() for n in os.environ.get(
    "ISU_PLAYER_NAMES",
    os.environ.get("ISU_MIC_NAME", "Player")).split(",") if n.strip()]


def _is_for_player(text: str) -> bool:
    """True, wenn die Aeusserung den Spieler direkt anspricht (Name im Text).
    Nur dann wird vertont; sonst stiller Text-Funk fuer die Gruppe."""
    t = (text or "").lower()
    return any(name in t for name in PLAYER_NAMES)


@mcp.tool()
def say(text: str = "", message: str = "", content: str = "") -> str:
    """Etwas laut sagen. Erscheint immer im In-Game-Chat (Spieler in 60 m).
    VERTONT (Discord-Stimme) wird es NUR, wenn du den SPIELER direkt ansprichst,
    also seinen Namen nennst (z.B. "Clausi, pass auf!"). Funk untereinander
    (an Viktor/Igor/Birgit/Konrad) bleibt absichtlich Text - das spart Stimme
    und Aufmerksamkeit. Willst du, dass der Spieler dich HOERT: nenne seinen
    Namen. Kurz und in deiner Rolle bleiben - du bist Viktor, kein Assistent."""
    text = text or message or content
    if not text:
        return "Bitte text angeben, z.B. say(text=\"Hallo\")."
    if _is_repeat(text):
        return "Schon gerade gesagt - nicht doppelt wiederholt."
    # Immer in den In-Game-Chat (CmdSay sendet Chat + Comic-Blase). Vertonen
    # (Discord-TTS) NUR bei Funk an den Spieler - NPC-untereinander bleibt Text.
    # Bildschirmtext latinisieren (Font kann kein CJK/Arabisch/...), Audio bleibt original.
    screen = transliterate.to_screen(text, AGENT_LANG)
    if screen != text:
        _radio_native(screen, text)   # andere NPCs sollen das Original lesen, nicht das Transliterat
    result = BRIDGE.run("say", text=screen, timeout=15)
    if _discord_active() and _is_for_player(text):
        _outbox({"type": "tts", "text": text, "agent": AGENT_NAME,
                 "voice": AGENT_VOICE})
    return _outcome(result, "Gesagt.")


@mcp.tool()
def voice_lines() -> str:
    """Katalog deiner hoerbaren Sprachzeilen (fuer say_voice). Nutze die id."""
    catalog = _voice_catalog()
    if not catalog:
        return "Keine Sprachzeilen verfuegbar (soundsets.json fehlt)."
    lines: list[str] = []
    by_cat: dict[str, list[str]] = {}
    for pid, info in catalog.items():
        by_cat.setdefault(info.get("category", "?"), []).append(
            f"  {pid}: \"{info.get('text')}\"")
    for cat in sorted(by_cat):
        lines.append(cat.upper() + ":")
        lines.extend(by_cat[cat])
    return "\n".join(lines)


def _speak_tts(txt: str, note: str) -> str:
    """Freitext live ueber ElevenLabs sprechen (wie say). Gemeinsamer Pfad
    fuer say und den say_voice-Fallback."""
    if _is_repeat(txt):
        return note + " (schon gesagt, nicht wiederholt)"
    screen = transliterate.to_screen(txt, AGENT_LANG)
    if screen != txt:
        _radio_native(screen, txt)
    result = BRIDGE.run("say", text=screen, timeout=15)
    if _discord_active() and _is_for_player(txt):
        _outbox({"type": "tts", "text": txt, "agent": AGENT_NAME,
                 "voice": AGENT_VOICE})
    return _outcome(result, note)


@mcp.tool()
def say_voice(phrase_id: str = "", text: str = "", line: str = "") -> str:
    """Eine Sprachzeile HOERBAR sagen. Mit gueltiger phrase_id aus dem Katalog
    (nur Viktor) als 3D-Sound, sonst als Live-TTS. Beispiel:
    say_voice(phrase_id="greet_01"). Fuer Freitext geht auch einfach say.
    Tolerant: Freitext in phrase_id/text/line wird live gesprochen statt zu
    scheitern."""
    catalog = _voice_catalog()
    info = catalog.get(phrase_id)
    free = (text or line or "").strip()
    # phrase_id war Freitext (Agent verwechselt say/say_voice) -> als Text werten
    if not info and not free and phrase_id and phrase_id not in catalog:
        free = phrase_id.strip()

    # Kein Katalog-Treffer: Freitext live sprechen (funktioniert fuer alle).
    if not info:
        if free:
            return _speak_tts(free, "Gesagt (Freitext live).")
        return ("Nichts zu sagen. Fuer Freitext nutze say(text=...), fuer "
                "Katalog-Zeilen say_voice(phrase_id=...) - Liste mit voice_lines().")

    # Katalog-Oggs existieren bisher nur fuer Viktor - andere sprechen den
    # Katalogtext live mit eigener Stimme statt zu scheitern.
    if AGENT_NAME.lower() != "viktor":
        return _speak_tts(info["text"], f"Gesagt (live): \"{info['text']}\"")

    if _is_repeat(info["text"]):
        return f"Schon gerade gesagt: \"{info['text']}\" - nicht wiederholt."
    # Immer in den Chat + Comic-Blase (via say). Audio: bei Discord die Ogg,
    # sonst der 3D-Spielsound an die nahen Spieler - nie beides (sonst doppelt).
    BRIDGE.run("say", text=info["text"], timeout=15)
    if _discord_active():
        if info.get("ogg"):
            _outbox({"type": "ogg", "path": info["ogg"], "text": info["text"],
                     "agent": AGENT_NAME, "voice": AGENT_VOICE})
    else:
        BRIDGE.run("say_voice", text=info["soundset"], timeout=15)
    return f"Gerufen.\nDu hast hoerbar gesagt: \"{info['text']}\""


_ROSTER_NAMES: set[str] | None = None


def _roster_names() -> set[str]:
    """KI-Kameraden-Namen (lower) aus dem aktiven Roster - damit follow/regroup
    einen Befehl auf einen KAMERADEN (z.B. 'Viktor') nicht faelschlich auf den
    menschlichen Spieler umbiegt."""
    global _ROSTER_NAMES
    if _ROSTER_NAMES is not None:
        return _ROSTER_NAMES
    names: set[str] = set()
    for fn in ("active_roster.json", "agents.json"):
        try:
            with open(os.path.join(REPO_DIR, "arena", fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            for a in data.get("agents", []):
                n = (a.get("name") or "").strip().lower()
                if n:
                    names.add(n)
        except (OSError, ValueError):
            pass
    _ROSTER_NAMES = names
    return names


def _resolve_player_name(requested: str, allow_empty: bool = False) -> str:
    """Funk-/Voice-Namen des Spielers auf seinen DayZ-Profilnamen normalisieren.

    Der menschliche Spieler heisst im Funk oft anders (PLAYER_RADIO_NAME,
    'Isualc') als im Spiel ('Clausi', GetIdentity). Die Mod vergleicht beim
    follow EXAKT (pb.GetIdentity().GetName() != cmd.text), darum scheitert
    follow('Isualc') mit "weder Spieler noch Kamerad in 180 m", waehrend
    follow('Clausi') klappt. Aufloesungsreihenfolge:
      1. Leer / KI-Kamerad (Roster): unveraendert lassen.
      2. Exakter Treffer auf einen sichtbaren Spieler: dessen echte Schreibweise.
      3. Genau EIN Mensch in Sicht: der ist eindeutig gemeint.
      4. Funk- oder konfigurierter Profilname des Spielers gemeint: den
         konfigurierten Profilnamen nehmen, sonst (allow_empty) leer -> die Mod
         folgt dem naechsten Spieler (NPCs haben keine Identity, der Mensch ist
         der einzige echte Spieler).
      5. Sonst unveraendert (Mod-Fallback entscheidet)."""
    requested = (requested or "").strip()
    if not requested:
        return requested
    if requested.lower() in _roster_names():
        return requested  # KI-Kamerad - nie auf den Menschen umbiegen
    state = BRIDGE.read_state() or {}
    humans = [e.get("name") for e in state.get("nearby", [])
              if e.get("kind") == "player" and e.get("name")]
    for h in humans:                       # exakter Treffer -> echte Schreibweise
        if h.lower() == requested.lower():
            return h
    uniq = list(dict.fromkeys(humans))
    if len(uniq) == 1:                      # genau ein Mensch sichtbar
        return uniq[0]
    # 4a. players_registry (Schnittstelle 5): Funk-/Alias-Name -> DayZ-Profilname.
    # Ersetzt den hartkodierten Isualc->Clausi-Pfad, wenn die Datei vorhanden ist.
    if players_registry is not None:
        try:
            rec = players_registry.resolve(requested)
        except Exception:
            rec = None
        if rec:
            dayz = (rec.get("dayz") or "").strip()
            if dayz:
                return dayz                 # aufgeloester Profilname
            if allow_empty:
                return ""                   # bekannt, aber kein Profilname -> Mod folgt naechstem
    # 4b. Fallback (Registry fehlt/kennt den Namen nicht): meint der Befehl
    # nachweislich den Menschen (sein Funk- oder konfigurierter Profilname)?
    aliases = {PLAYER_RADIO_NAME.lower()}
    if PLAYER_PROFILE_NAME:
        aliases.add(PLAYER_PROFILE_NAME.lower())
    if requested.lower() in aliases:
        if PLAYER_PROFILE_NAME:
            return PLAYER_PROFILE_NAME      # explizit konfigurierter Profilname
        if allow_empty:
            return ""                       # Mod folgt dem naechsten Spieler
    return requested


@mcp.tool()
def follow(player_name: str = "") -> str:
    """Einem Spieler folgen (Gruppenbeitritt, du laeufst automatisch hinterher).
    Ohne player_name: der naechste Spieler in 100 m. Endet automatisch, sobald
    du selbst irgendwohin gehst (move_to/flee/engage) oder unfollow nutzt."""
    _abort_travel()   # Folgen bricht eine laufende Fernreise ab
    target = _resolve_player_name(player_name, allow_empty=True)
    result = BRIDGE.run("follow", text=target, timeout=15)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "weder Spieler noch Kamerad" in detail:
        if not (player_name or "").strip():
            return ("Fehlgeschlagen: kein Spieler/Kamerad in Reichweite und du "
                    "hast KEINEN Namen genannt. Gib einen Namen an, z.B. "
                    "follow(player_name=\"Clausi\"), oder geh mit move_to naeher "
                    "ran. NICHT mit leerem follow() wiederholen.")
        return (f"Fehlgeschlagen: '{player_name}' ist nicht in 180 m. Pruefe den "
                f"Namen (Schreibweise wie in observe) oder geh mit move_to naeher "
                f"ran, statt follow zu wiederholen.")
    return _outcome(result, "Folge aufgenommen:")


@mcp.tool()
def unfollow() -> str:
    """Aufhoeren, einem Spieler zu folgen, und stehenbleiben."""
    result = BRIDGE.run("unfollow", timeout=15)
    return _outcome(result, "Folge beendet.")


@mcp.tool()
def stop() -> str:
    """Sofort stehenbleiben und aktuelle Bewegung abbrechen (beendet auch follow)."""
    _abort_travel()   # bricht auch eine laufende Fernreise ab
    result = BRIDGE.run("stop", timeout=15)
    return _outcome(result, "Stehe still.")


# --------------------------------------------------------------- Wissensbasis
# Kuratiertes Survival-Wissen fuer research(): verhindert, dass das Gehirn
# Spielmechanik halluziniert, und nennt die richtige Werkzeug-Folgekette.
KNOWLEDGE = {
    "jagd": (
        "JAGD: Beutetiere (Reh/Cervus, Wildschwein/SusScrofa, Kuh/BosTaurus, "
        "Ziege, Schaf, Hase/Lepus, Huhn) stehen auf Wiesen und an "
        "Waldraendern, kind=animal in observe. Folgekette: hunt (pirscht, "
        "erlegt, zerlegt gleich mit) -> process_food (gart am Feuer) -> eat. "
        "Mit schussbereiter Feuerwaffe faellt das Tier auf 35 m, ohne musst "
        "du auf 4 m ran (nur Huhn/Hase realistisch). Zerlegen braucht ein "
        "Schneidwerkzeug (Messer/Axt/Machete). Wolf/Baer sind KEIN Jagdwild: "
        "engage mit Schusswaffe, oder flee."),
    "fischen": (
        "FISCHEN: braucht eine FishingRod (craft fishing_rod: 1x "
        "LongWoodenStick + 1x Rope - fish craftet sie selbst nach, wenn "
        "Material da ist). Folgekette: fish (sucht Wasser, laeuft hin, "
        "angelt; ~60% Chance, mit Koeder Worm/Bait ~80%) -> process_food/"
        "cook_meal (garen!) -> eat. Am Weiher gibt es Carp, an der Kueste "
        "Mackerel. Mehrere Versuche sind normal."),
    "kochen": (
        "KOCHEN/VERARBEITEN: Rohes Fleisch, roher Fisch machen krank - "
        "IMMER erst garen. process_food erledigt die ganze Kette: Kadaver "
        "in 50 m zerlegen, Feuer suchen/bauen (5x WoodenStick + 1x Rag + "
        "Zuendmittel: Matchbox/Lighter/HandDrillKit) und alles Rohe garen. "
        "Nach dem Garen vom Feuer wegtreten - es verbrennt dich."),
    "kleidung": (
        "KLEIDUNG: dress_best optimiert alle Koerper-Slots systematisch - "
        "beim FRIEREN (Waerme in VITALS unter etwa -0.15) zaehlt "
        "Waermeisolierung, in moderater Lage der Stauraum (Cargo-Slots). "
        "Nasse Kleidung (wet) am Feuer trocknen. Kaelte kostet dauerhaft "
        "Gesundheit; warme, trockene Kleidung schlaegt die dritte Waffe. "
        "wear tauscht Slots selbst und rettet den Inhalt - gefuellte "
        "Kleidung NIE vorher droppen."),
    "munition": (
        "MUNITION/NACHLADEN: AmmoBox_* mit x0 ist VERPACKT, nicht leer -> "
        "unpack_ammo macht sie zum Ammo_*-Stapel. Lose Munition wird erst "
        "durch reload zu Feuerkraft (fuellt Magazine und interne Magazine "
        "wie Mosin/Flinte). Folgekette nach dem Looten und nach jedem "
        "Kampf: unpack_ammo (falls Boxen) -> reload. Zeigt observe die "
        "Waffe UNGELADEN und du hast Munition: reload. Magazinwechsel im "
        "Gefecht laeuft automatisch, solange ein gefuelltes Magazin im "
        "Inventar liegt. Waffe klemmt/beschaedigt: clean_weapon."),
    "medizin": (
        "MEDIZIN: Blutung -> SOFORT bandage (Bandage/Rag). Krankheit -> "
        "treat_illness (Cholera/Grippe/Wundinfekt: Tetracycline; "
        "Salmonellen: CharcoalTablets; Gehirninfekt: nur Ruhe). Andere "
        "behandeln: treat_other (Bandage/Splint/SalineBagIV/BloodBagIV). "
        "Krank wird man durch rohes Fleisch, schmutziges Wasser, "
        "Unterkuehlung und Zombie-Treffer."),
    "wasser": (
        "WASSER: drink_at_well erledigt die ganze Kette am Brunnen "
        "(kind=water, stehen in Doerfern): hinlaufen, trinken, Behaelter "
        "fuellen. Unterwegs: drink aus Flasche/Dose. Teich-/Flusswasser "
        "kann krank machen - Brunnen ist sicher."),
    "feuer": (
        "FEUER: Lagerfeuer = craft fireplace (2x WoodenStick + 1x Rag) + "
        "3x WoodenStick Brennholz + Zuendmittel (Matchbox, Lighter, "
        "HandDrillKit aus combine(a=\"Bark_Oak\", b=\"WoodenStick\")). "
        "cook_meal/process_food bauen und zuenden selbst. Feuer waermt "
        "(gegen Frieren) und trocknet - aber Abstand halten, es verbrennt."),
    "herstellung": (
        "HERSTELLUNG: recipes zeigt alle Rezepte, craft baut sie, combine "
        "kombiniert zwei Teile direkt (Stick+Rag=Torch, Stein+Stein="
        "StoneKnife, LongStick+Rope=FishingRod, Bark+Stick=HandDrillKit, "
        "2xStick+Rag=Splint/Fireplace). Neues Rezept von Spielern: "
        "learn_recipe - bleibt dauerhaft."),
}
KNOWLEDGE["nachladen"] = KNOWLEDGE["munition"]
KNOWLEDGE["waffen"] = KNOWLEDGE["munition"]
KNOWLEDGE["nahrung"] = KNOWLEDGE["kochen"]
KNOWLEDGE["kaelte"] = KNOWLEDGE["kleidung"]
KNOWLEDGE["krankheit"] = KNOWLEDGE["medizin"]
KNOWLEDGE["crafting"] = KNOWLEDGE["herstellung"]
KNOWLEDGE["angeln"] = KNOWLEDGE["fischen"]


@mcp.tool()
def research(topic: str = "") -> str:
    """Survival-Wissen nachschlagen, BEVOR du raetst oder etwas Falsches
    versuchst. Themen: jagd, fischen, kochen/nahrung, kleidung/kaelte,
    munition/nachladen/waffen, medizin/krankheit, wasser, feuer,
    herstellung/crafting. Liefert die Spielregeln UND die richtige
    Werkzeug-Folgekette. Ohne topic: Themenliste."""
    t = (topic or "").strip().lower()
    if not t:
        return ("Themen: jagd, fischen, kochen, kleidung, munition, medizin, "
                "wasser, feuer, herstellung. Beispiel: research(topic=\"jagd\").")
    if t in KNOWLEDGE:
        return KNOWLEDGE[t]
    hits = [k for k in KNOWLEDGE if t in k or k in t]
    if hits:
        return KNOWLEDGE[hits[0]]
    return ("Kein Eintrag zu '" + t + "'. Themen: jagd, fischen, kochen, "
            "kleidung, munition, medizin, wasser, feuer, herstellung.")


@mcp.tool()
def recipes() -> str:
    """Liste deiner Crafting-Rezepte mit Materialbedarf (eingebaute und
    selbst gelernte)."""
    lines = []
    for name, r in sorted(tactics.all_recipes().items()):
        mats = ", ".join(f"{n}x {m}" for m, n in r["mats"].items())
        mark = " [GELERNT]" if r.get("learned") else ""
        lines.append(f"{name}: {mats} -> {r['result']} ({r['desc']}){mark}")
    lines.append("Hinweis: cook_meal braucht zusaetzlich 3x WoodenStick Brennholz "
                 "und ein Zuendmittel (Matchbox/PetrolLighter).")
    lines.append("Neues Rezept von Spielern lernen: learn_recipe.")
    return "\n".join(lines)


@mcp.tool()
def learn_recipe(name: str, materials: str, result: str,
                 place: bool = False, desc: str = "") -> str:
    """Ein neues Rezept DAUERHAFT lernen (z.B. wenn ein Spieler es dir
    erklaert). Beispiel: learn_recipe(name="holzkiste",
    materials="2x WoodenPlank + 4x Nail", result="WoodenCrate").
    place=True, wenn das Ergebnis vor dir am Boden platziert werden soll
    (Bauwerke), sonst landet es im Inventar. Materials sind Classnames!"""
    return tactics.learn_recipe(name, materials, result, place, desc)


@mcp.tool()
def loot_corpse() -> str:
    """Die naechste Leiche (kind=corpse in observe, max. 50 m) looten:
    hinlaufen und die Sachen uebernehmen. Tote Infizierte tragen oft
    Brauchbares - nach einem Kampf immer eine Ueberlegung wert."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    result = BRIDGE.run("loot_corpse", timeout=60, interruptible=True)
    return _outcome(result, "Geloote:")


# Skip-Cache: kuerzlich erfolglos durchsuchte Container merken (Key = classname
# + grob gerundete NPC-Position; der NPC steht beim Looten am Container). So
# faehrt das Gehirn denselben leeren Behaelter nicht 18x an.
_LOOT_EMPTY: dict[str, float] = {}
_LOOT_EMPTY_TTL = 90.0


def _loot_key(classname: str) -> str:
    snap = BRIDGE.read_state() or {}
    npc = snap.get("npc", {})
    gx = round(float(npc.get("pos_x", 0.0)) / 5.0)   # 5-m-Raster
    gz = round(float(npc.get("pos_z", 0.0)) / 5.0)
    return f"{(classname or '*').strip().lower()}@{gx},{gz}"


@mcp.tool()
def loot_container(classname: str = "") -> str:
    """Naechsten Behaelter MIT INHALT ausraeumen (max. 50 m): Leichen,
    liegende Rucksaecke, Kleidung mit Sachen drin, Kisten. ZELTE werden NICHT
    gepluendert - das Lager-Zelt ist euer gemeinsamer Stauraum, da legst du mit
    store_container hinein (sonst raeumst du den anderen ihr Depot leer). In
    observe erkennst du Loot am Marker [enthaelt N]. Beispiel:
    loot_container(classname="TaloonBag") oder ohne Filter den naechsten."""
    key = _loot_key(classname)
    now = time.time()
    for k, t in list(_LOOT_EMPTY.items()):   # abgelaufene Eintraege raeumen
        if now - t > _LOOT_EMPTY_TTL:
            _LOOT_EMPTY.pop(k, None)
    if key in _LOOT_EMPTY:
        return ("UEBERSPRUNGEN: Diesen Behaelter hast du gerade eben schon "
                "erfolglos durchsucht (leer/nur getragene Kleidung). NICHT erneut "
                "anfahren - geh weiter oder suche mit observe einen Marker "
                "[enthaelt N] an einer ANDEREN Stelle.")
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    result = BRIDGE.run("loot_container", text=classname, timeout=60, interruptible=True)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "nur getragene Kleidung" in detail:
        _LOOT_EMPTY[key] = now
        return ("Leer (nur getragene Kleidung) - durchsucht. Diesen Behaelter "
                "NICHT erneut looten; die [enthaelt N]-Marker zaehlen oft nur "
                "Zombie-Kleidung mit. Weiter zum naechsten oder andere Aufgabe.")
    return _outcome(result, "Ausgeraeumt:")


@mcp.tool()
def store_container(classname: str = "") -> str:
    """Lose Items aus deinem Inventar IN einen nahen Container legen (Zelt,
    Kiste, Fass, Rucksack am Boden; max. 50 m) - das Gegenstueck zu
    loot_container. SO verstaust du Ueberschuss richtig, statt ihn mit drop auf
    den BODEN zu werfen. Getragene Kleidung und die Waffe in der Hand bleiben
    dran (nur lose Sachen aus deinem Cargo werden verstaut). Optionaler Filter:
    store_container(classname="Ammo") verstaut nur Munition; ohne Filter alles
    Lose. Geh vorher mit move_to zum Zelt/Container. Findet sich KEIN Container
    in der Naehe und dein Inventar ist voll, darfst du als Notloesung mit drop
    Platz schaffen."""
    # Bewegungs-Tool (laeuft bis 50 m zum Container): laufende Fernreise
    # abbrechen - war als EINZIGES der 16 Bewegungs-Tools ohne diesen Guard.
    _abort_travel()
    result = BRIDGE.run("store_container", text=classname, timeout=60, interruptible=True)
    out = _outcome(result, "Verstaut:")
    if "kein Container" in out:
        out += (" Fallback: kein Container in der Naehe - wenn dein Inventar voll"
                " ist, mit drop Platz schaffen, sonst spaeter am Lager verstauen.")
    return out


@mcp.tool()
def craft(recipe: str) -> str:
    """Etwas herstellen. Beispiel: craft(recipe="fireplace") - Rezeptnamen
    zeigt recipes(). Prueft Material, verbraucht es und erzeugt das Ergebnis
    (Lagerfeuer wird vor dir platziert, alles andere landet im Inventar)."""
    return tactics.craft(BRIDGE, recipe, log=lambda m: None)


@mcp.tool()
def cook_meal() -> str:
    """Rohes Essen im Inventar garen - erledigt die ganze Kette selbststaendig:
    brennendes Feuer suchen, notfalls Lagerfeuer bauen (5x WoodenStick + 1x Rag)
    und anzuenden (Zuendmittel noetig), dann alles Rohe garen. Rohes Fleisch zu
    essen macht krank - immer erst garen!"""
    _abort_travel()   # laeuft zum Feuer - laufende Fernreise abbrechen
    return tactics.cook_meal(BRIDGE, log=lambda m: None)


@mcp.tool()
def drink_at_well() -> str:
    """Zum naechsten sichtbaren Brunnen laufen (kind=water in observe), dort
    trinken und einen Fluessigkeitsbehaelter auffuellen. Brunnen stehen in
    Doerfern - wenn keiner sichtbar ist, erst in eine Ortschaft gehen."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    return tactics.water_run(BRIDGE, log=lambda m: None)


@mcp.tool()
def find_item(pattern: str = "", item_name: str = "", item_type: str = "",
              item: str = "", name: str = "") -> str:
    """Pruefen, ob du etwas Bestimmtes hast oder in Sichtweite liegt.
    Beispiel: find_item(pattern="Canister") - Teilstring im Classname.
    Fuer die Suche in der Welt: explore_step wiederholen."""
    # Bewusst nur Alias-Faltung, KEINE Classname-Korrektur: find_item ist eine
    # Teilstring-Suche, ein "korrigierter" Volltreffer wuerde die Suche verengen.
    needle = _first(pattern, item_name, item_type, item, name)
    if not needle:
        return "Bitte pattern angeben, z.B. find_item(pattern=\"Nail\")."
    return tactics.find_item(BRIDGE, needle)


@mcp.tool()
def explore_step() -> str:
    """Ein Such-/Erkundungsschritt: laeuft ~100 m in eine neue Richtung und
    sammelt dort Lohnendes ein. Fuer gezielte Item-Suche mehrfach wiederholen
    und zwischendurch mit find_item pruefen."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    return tactics.explore_step(BRIDGE, log=lambda m: None)


@mcp.tool()
def build_fence_frame() -> str:
    """EXPERIMENTELL: Einen Zaun-Rahmen vor dir bauen (braucht 2x WoodenLog
    im Inventar). Erster Baustein fuer eine Basis."""
    result = BRIDGE.run("build_fence_frame", timeout=30)
    return _outcome(result, "Gebaut:")


@mcp.tool()
def drop(classname: str = "", item_name: str = "", item: str = "",
         name: str = "") -> str:
    """Einen Gegenstand aus deinem Inventar auf den Boden legen (mit Animation).
    Beispiel: drop(classname="Mosin9130"); ohne Argument das Item in der Hand.
    Um einem MITSPIELER etwas zu geben, nutze besser give_to (direkt, ohne
    Bodenphase). drop ist fuer Ablegen oder Platzschaffen."""
    needle = _needle(classname, item_name, item, name)
    result = BRIDGE.run("drop", text=needle, timeout=30)
    return _outcome(result, "Abgelegt:", needle=needle)


@mcp.tool()
def give_to(player_name: str = "", classname: str = "", item_name: str = "",
            item: str = "", name: str = "", player: str = "",
            target: str = "") -> str:
    """Einem anderen Survivor (KI-Kollegen) einen Gegenstand DIREKT ins
    Inventar geben - zuverlaessiger als drop+pickup, das Item geht nicht am
    Boden verloren. Beide muessen nah beieinander sein (max 12 m).
    Beispiel: give_to(player_name="Konrad", classname="AmmoBox_545x39_20Rnd")."""
    who = _resolve_player_name(_first(player_name, player, target))
    needle = _needle(classname, item_name, item, name)
    if not who or not needle:
        return "Bitte player_name UND classname angeben."
    result = BRIDGE.run("hand_over", text=f"{who}|{needle}", timeout=15)
    return _outcome(result, "Uebergeben:", needle=needle)


@mcp.tool()
def sling() -> str:
    """Waffe schultern und schneller laufen. Auf langen Wegen ohne Gefahr
    sinnvoll - eine Waffe in der Hand bremst auf Gehtempo. Bei Gefahr ziehst
    du mit equip_best (oder unsling) automatisch wieder die Waffe."""
    result = BRIDGE.run("sling", timeout=15)
    return _outcome(result, "Geschultert:")


@mcp.tool()
def unsling() -> str:
    """Waffe wieder in Anschlag nehmen (entspricht equip_best). Bei Gefahr
    oder vor einem Kampf nutzen, nachdem du mit sling geschultert hast."""
    result = BRIDGE.run("unsling", timeout=15)
    return _outcome(result, "In Anschlag:")


@mcp.tool()
def regroup(player_name: str = "") -> str:
    """Zur AKTUELLEN Position des Spielers laufen, dem du folgst (gegen
    Verlaufen durch falsche Koordinaten). Ohne Namen: der gefolgte bzw.
    naechste Spieler. Nutze das statt move_to mit geratenen Zahlen, wenn du
    die Gruppe verloren hast."""
    _abort_travel()   # zur Gruppe zurueck heisst: laufende Fernreise abbrechen
    target = _resolve_player_name(player_name, allow_empty=True)
    result = BRIDGE.run("regroup", text=target, timeout=240)
    return _outcome(result, "Wieder bei der Gruppe.")


@mcp.tool()
def door(action: str = "open") -> str:
    """Die naechste Tuer (max. 5 m) oeffnen oder schliessen.
    Beispiel: door(action="open") oder door(action="close"). Beim normalen
    Laufen oeffnest du Tueren automatisch - das hier ist fuer gezieltes
    Schliessen (Sicherheit!) oder Oeffnen ohne hindurchzugehen."""
    if action not in ("open", "close"):
        return "action muss 'open' oder 'close' sein."
    result = BRIDGE.run("door", text=action, timeout=20)
    return _outcome(result, "Tuer:")


@mcp.tool()
def vehicle_exit() -> str:
    """Bewusst aus dem Fahrzeug aussteigen. Nur nutzen, wenn das Fahrzeug
    steht und es einen guten Grund gibt. Waehrend der Fahrt: sitzen bleiben."""
    result = BRIDGE.run("vehicle_exit", timeout=20)
    return _outcome(result, "Steige aus:")


@mcp.tool()
def unstick() -> str:
    """Selbstbefreiung, wenn du festhaengst: zwingt dich aufzustehen, bricht
    haengende Aktionen ab und loest Geometrie-Verkeilungen. Nutzen, wenn
    move_to wiederholt ohne Fortschritt fehlschlaegt oder du am Boden liegst."""
    global _UNSTICK_CALLS, _UNSTICK_LAST
    now = time.time()
    # Circuit-Breaker (Muster _EQUIP_FAILS, hier AUFRUF-gezaehlt): unstick hilft
    # nur gegen Verkeilungen - wer es 3x binnen 90 s ruft, steht vor einem
    # Hindernis, nicht in einer Geometrie-Falle (Audit 03.07., Igor-Schleife).
    if (now - _UNSTICK_LAST) >= 90.0:
        _UNSTICK_CALLS = 0
    if _UNSTICK_CALLS >= 2:
        return ("unstick wurde gerade schon 2x versucht - das Problem ist kein "
                "Verkeilen. Nutze travel_to (weicht Hindernissen selbst aus) "
                "oder ein anderes Ziel, beende deinen Zug.")
    _UNSTICK_CALLS += 1
    _UNSTICK_LAST = now
    result = BRIDGE.run("unstick", timeout=15)
    return _outcome(result, "Befreit:")


@mcp.tool()
def travel_to(x: float, z: float) -> str:
    """Zu einem FERNEN Ziel aufbrechen (lange Strecke, z.B. ein anderer Ort auf
    der Karte). Anders als move_to laeuft die Reise im Hintergrund weiter und
    weicht Hindernissen (See, Zaun, Sumpf) AUTOMATISCH aus - keine eigenen
    move_to/unstick-Ketten noetig. Dieser Aufruf kehrt SOFORT zurueck, damit
    dein Zug endet - du sparst Tokens und meldest dich erst wieder, wenn du
    angekommen bist oder festhaengst. Deine Gedankenzeile zeigt die Restdistanz.
    Ein neues travel_to ersetzt das alte; stop, follow oder regroup brechen die
    Reise ab."""
    global _travel_thread, _travel_stop
    gx, gz = float(x), float(z)
    _abort_travel()   # altes Ziel verwerfen
    snap = BRIDGE.read_state() or {}
    npc = snap.get("npc", {})
    if npc.get("alive") and npc.get("spawned"):
        d = math.hypot(gx - float(npc.get("pos_x", 0.0)),
                       gz - float(npc.get("pos_z", 0.0)))
        if d <= _TRAVEL_ARRIVE:
            return (f"Du bist schon so gut wie dort (Distanz {d:.0f} m) - "
                    f"keine Reise noetig.")
    stop = threading.Event()
    t = threading.Thread(target=_travel_worker, args=(gx, gz, stop), daemon=True)
    with _travel_lock:
        _travel_thread = t
        _travel_stop = stop
    t.start()
    return (f"Reise gestartet Richtung x={gx:.0f} z={gz:.0f} - ich weiche "
            f"Hindernissen unterwegs selbst aus und melde mich bei Ankunft oder "
            f"wenn ich wirklich nicht weiterkomme. Beende jetzt deinen Zug.")


def _sleep_interruptible(seconds: float) -> bool:
    """Sekunden warten, aber sofort abbrechen, wenn neuer Funk in der Inbox
    liegt (wie andere Langaktionen). Gibt True zurueck, wenn unterbrochen."""
    inbox = getattr(BRIDGE, "voice_inbox", None)
    base = -1
    if inbox and os.path.exists(inbox):
        try:
            base = os.path.getsize(inbox)
        except OSError:
            base = -1
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if base >= 0 and bridge_mod._inbox_should_interrupt(inbox, base):
            return True
        time.sleep(1.0)
    return False


@mcp.tool()
def fish() -> str:
    """Angeln, um an Nahrung zu kommen. Braucht eine FishingRod im Inventar (ein
    Wurm/Koeder ist optional, erhoeht aber die Fangchance). Sucht selbst das
    naechste Gewaesser, laeuft hin, wirft aus und wartet. Chance ~60% (mit Koeder
    ~80%). Bei Erfolg landet ein Fisch in deinem Inventar (am Weiher ein Carp, an
    der See ein Mackerel) - roh, also vor dem Essen am Feuer garen (cook_meal)."""
    state = BRIDGE.read_state() or {}
    rod_note = ""
    if not _have_item(state, "FishingRod"):
        # Folgekette: Rute selbst nachbauen, wenn das Material da ist
        crafted = tactics.craft(BRIDGE, "fishing_rod", log=lambda m: None)
        if not crafted.startswith("Hergestellt"):
            return ("Du hast keine Angelrute (FishingRod) und das Material fuer "
                    "eine neue reicht nicht (1x LongWoodenStick + 1x Rope): "
                    + crafted + " Erst Material/Rute besorgen, dann wieder fish.")
        rod_note = "Angelrute unterwegs gebaut. "
        state = BRIDGE.read_state() or state
    has_bait = (_have_item(state, "Worm") or _have_item(state, "Bait")
                or _have_item(state, "Dough"))

    _abort_travel()   # Angeln uebernimmt die Beine - laufende Fernreise abbrechen
    water = BRIDGE.run("find_water", max_dist=300)
    if water.get("status") != "done":
        return ("Kein Gewaesser in der Naehe gefunden (oder die Servermod kennt "
                "find_water noch nicht). Stell dich selbst ans Wasser (move_to an "
                "einen Weiher/die Kueste) und ruf fish dort erneut auf.")
    # detail = "x z kind" (kind=pond|sea)
    parts = (water.get("detail") or "").split()
    kind = "pond"
    wx = wz = None
    try:
        wx = float(parts[0])
        wz = float(parts[1])
        if len(parts) >= 3:
            kind = parts[2].strip().lower()
    except (IndexError, ValueError):
        return ("Wasserstelle gemeldet, aber unlesbar. Stell dich selbst ans "
                "Wasser und ruf fish erneut auf.")

    steps: list[str] = []
    if rod_note:
        steps.append(rod_note.strip())
    # Frischen State lesen: der Snapshot von oben ist nach _abort_travel +
    # find_water Sekunden alt. Und pos-Default NICHT auf die Wasserkoordinate
    # setzen - das ergab d=0 und der NPC "angelte" auf dem Trockenen.
    state = BRIDGE.read_state() or state
    npc = state.get("npc", {})
    px = npc.get("pos_x")
    pz = npc.get("pos_z")
    if px is None or pz is None:
        return ("Eigene Position unbekannt - observe aufrufen und fish danach "
                "erneut versuchen.")
    d = math.hypot(wx - float(px), wz - float(pz))
    if d > 3.5:
        mv = BRIDGE.run("move_to", x=wx, z=wz, timeout=90, interruptible=True)
        if mv.get("status") == "interrupted":
            return _outcome(mv, "")
        if mv.get("status") != "done":
            # Nicht ganz hingekommen - trotzdem versuchen, wenn nah genug
            state = BRIDGE.read_state() or state
            npc = state.get("npc", {})
            d = math.hypot(wx - float(npc.get("pos_x", wx)),
                           wz - float(npc.get("pos_z", wz)))
            if d > 8.0:
                return (f"Komme nicht ans Wasser ({mv.get('detail') or 'blockiert'}). "
                        f"Versuch es selbst mit move_to zum Ufer, dann fish.")
        steps.append("Am Wasser.")

    _write_intent_line("angelt am Wasser")
    interrupted = _sleep_interruptible(random.uniform(60, 120))
    if interrupted:
        return ("ABGEBROCHEN beim Angeln: Der Spieler funkt dich an. Hoer SOFORT "
                "zu und reagiere, bevor du weitermachst.")

    chance = 0.6 + (0.2 if has_bait else 0.0)
    if has_bait:
        # Den tatsaechlich vorhandenen Koeder verbrauchen (frueher stur "Worm",
        # auch wenn der Koeder Bait/Dough war -> nichts wurde verbraucht).
        bait_cn = "Worm"
        for cand in ("Worm", "Bait", "Dough"):
            if _have_item(state, cand):
                bait_cn = cand
                break
        BRIDGE.run("consume_item", text=bait_cn, y=1, timeout=10)  # best effort
    if random.random() < chance:
        fish_class = "Mackerel" if kind == "sea" else "Carp"
        res = BRIDGE.run("give_item", text=fish_class, timeout=15)
        if res.get("status") == "done":
            steps.append(f"Fisch gefangen: {fish_class} (roh - vor dem Essen garen!).")
        else:
            steps.append(f"Ein Fisch biss an, aber kein Platz im Inventar "
                         f"({res.get('detail') or ''}).")
    else:
        steps.append("Kein Biss diesmal. Nochmal fish versuchen (mit Koeder "
                     "steigt die Chance).")
    return " ".join(steps)


@mcp.tool()
def treat_illness() -> str:
    """Dich SELBST gegen eine Krankheit behandeln. Liest deine Symptome und
    nimmt das passende Medikament aus deinem Inventar: gegen Cholera, Grippe und
    Wundinfektion TetracyclineAntibiotics, gegen Salmonellen CharcoalTablets.
    Gegen eine Gehirninfektion hilft kein Medikament (nur Zeit/Ruhe). Meldet
    klar, wenn dir nichts fehlt oder das noetige Medikament fehlt."""
    state = BRIDGE.read_state() or {}
    npc = state.get("npc", {})
    disease = npc.get("disease")
    agents = {}
    if isinstance(disease, dict) and isinstance(disease.get("agents"), dict):
        agents = {k: v for k, v in disease["agents"].items()
                  if isinstance(v, (int, float)) and v > 0}
    if not agents:
        if isinstance(disease, dict) and disease.get("sick"):
            return ("Du fuehlst dich unwohl, aber es ist kein konkreter Erreger "
                    "eingetragen. Beobachte dich weiter (observe).")
        return "Dir fehlt nichts - keine Krankheit erkennbar."

    # Behandelbaren Erreger mit vorhandenem Medikament suchen (schwerster zuerst).
    missing: list[str] = []
    for erk in sorted(agents, key=lambda k: -agents[k]):
        med = DISEASE_MED.get(erk)
        if med is None:
            continue  # z.B. brain - kein Medikament
        if _have_item(state, med):
            result = BRIDGE.run("eat", text=med, timeout=30)
            return _outcome(result, f"Gegen {erk} eingenommen: {med}.")
        missing.append(f"{erk} braucht {med}")

    present = ", ".join(sorted(agents))
    if not missing:
        return (f"Du bist krank ({present}), aber dagegen hilft kein Medikament "
                f"(z.B. Gehirninfektion) - nur Ruhe und Zeit.")
    return (f"Du bist krank ({present}), hast aber das noetige Medikament nicht "
            f"dabei: {', '.join(missing)}. Suche es (loot_area/explore_step) oder "
            f"lass es dir vom Sani geben.")


@mcp.tool()
def treat_other(player_name: str = "", classname: str = "", item_name: str = "",
                item: str = "", name: str = "", player: str = "",
                target: str = "") -> str:
    """Einen anderen Survivor (max. 3 m) mit einem Medizin-Item aus DEINEM
    Inventar versorgen: Bandage/Rag (Wunden), Splint (Bruch), SalineBagIV oder
    BloodBagIV (Fluessigkeit/Blut). Das Item wird dabei verbraucht. Beispiel:
    treat_other(player_name="Igor", classname="BandageDressing"). So spielt der
    Sani seine Rolle, ohne dem anderen das Zeug nur in die Hand zu druecken."""
    who = _resolve_player_name(_first(player_name, player, target, name))
    state = BRIDGE.read_state() or {}
    needle = _needle(classname, item_name, item)
    if not who or not needle:
        return "Bitte player_name UND classname (das Medizin-Item) angeben."
    if not _have_item(state, needle):
        return (f"Du hast '{needle}' nicht im Inventar - kannst {who} damit nicht "
                f"behandeln.{_did_you_mean(needle)}")
    result = BRIDGE.run("treat_other", target=who, item=needle, timeout=20)
    return _outcome(result, f"{who} behandelt mit {needle}:", needle=needle)


@mcp.tool()
def bandage() -> str:
    """DICH SELBST verbinden, wenn du blutest (Weckruf "DU BLUTEST" oder
    VITALS-Blutverlust). Nimmt automatisch BandageDressing oder Rag aus deinem
    Inventar (wird verbraucht). Blutung stoppt sofort - unbehandelt verblutest
    du. Kein Verbandsmaterial? Looten, Kameraden fragen oder Rags aus
    Ersatzkleidung reissen."""
    state = BRIDGE.read_state() or {}
    needle = ""
    for cand in ("BandageDressing", "Rag"):
        if _have_item(state, cand):
            needle = cand
            break
    if not needle:
        return ("Kein Verbandsmaterial (BandageDressing/Rag) im Inventar! "
                "SOFORT besorgen: looten, einen Kameraden per Funk um eine "
                "Bandage bitten (give_to), oder Rags craften.")
    # Selbstbehandlung ueber den treat_other-Pfad der Mod: die findet den
    # eigenen Survivor per Name (Distanz 0), prueft IsBleeding und schliesst
    # die staerkste Blutungsquelle wie Vanilla-ApplyBandage.
    result = BRIDGE.run("treat_other", target=AGENT_NAME, item=needle, timeout=20)
    detail = result.get("detail") or ""
    if result.get("status") == "failed" and "blutet nicht" in detail:
        return "Du blutest (nicht mehr) - kein Verband noetig."
    return _outcome(result, f"Verbunden mit {needle}:")


@mcp.tool()
def wait(seconds: int = 15) -> str:
    """Abwarten und beobachten (1-60 Sekunden). Danach kommt automatisch eine
    frische Lagebeschreibung. Spieler-Funk bricht das Warten sofort ab."""
    seconds = max(1, min(60, int(seconds)))
    # Unterbrechbar warten (wie fish): Funk soll den NPC sofort wecken,
    # nicht erst nach Ablauf der vollen Wartezeit. True = unterbrochen.
    if _sleep_interruptible(seconds):
        return ("Warten ABGEBROCHEN: Der Spieler funkt dich an - hoer zu und "
                "reagiere zuerst.\n\n" + _observe_text())
    return f"{seconds}s gewartet.\n\n" + _observe_text()


if __name__ == "__main__":
    mcp.run()
