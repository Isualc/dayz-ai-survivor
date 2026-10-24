#!/usr/bin/env python3
"""IsuSurvivor MCP-Server — stellt die DayZ-Bridge als Werkzeuge für Claude bereit.

Wird von Claude Code als stdio-MCP-Server gestartet (siehe run_agent.py).
Alle Werkzeuge blockieren bis zum Ergebnis (mit Zeitdeckel) und geben
deutschen Klartext zurück.
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
import survival

# Mitspieler-Registry (Schnittstelle 5): löst Funk-/Alias-Namen auf den
# DayZ-Profilnamen auf. Abhängigkeitsfrei; fehlt sie, fällt der Namens-
# Resolver auf das bestehende Isualc->Clausi-Verhalten zurück.
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
    """Äußerung für die Discord-Voice-Brücke ablegen (best effort)."""
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
parser.add_argument("--voice", default="", help="ElevenLabs-Stimme für Discord-TTS")
parser.add_argument("--language", default="de",
                    help="Ausgabe-Sprache der NPC; steuert die Bildschirm-Transliteration")
parser.add_argument("--outbox", default="",
                    help="voice_outbox.jsonl (Default: agent_home-Konvention)")
args = parser.parse_args()

BRIDGE = Bridge(args.profile, args.npc_id)
AGENT_NAME = args.agent_name
AGENT_VOICE = args.voice
# Ausgabe-Sprache: steuert, ob der ON-SCREEN-Text (Chat/Comic/Gedankenzeile)
# für nicht-lateinische Schriften (CJK/Arabisch/Hindi/Kyrillisch/Griechisch)
# auf ASCII latinisiert wird, weil der Stock-Font sie nicht zeichnet. Der
# Audio-/TTS-Pfad bekommt weiter den Originaltext. Latein bleibt unangetastet.
AGENT_LANG = (args.language or "de").strip().lower()
import transliterate
VOICE_OUTBOX = args.outbox or os.path.join(_agent_home(args.npc_id), "voice_outbox.jsonl")
# Funk-Inbox (Geschwister der Outbox): lange Aktionen brechen ab, sobald hier
# neuer Funk auftaucht, damit der Agent sofort auf den Spieler reagiert.
VOICE_INBOX = os.path.join(os.path.dirname(VOICE_OUTBOX), "voice_inbox.jsonl")
BRIDGE.voice_inbox = VOICE_INBOX
# Recipe and outcome memory belong to this survivor, across ALL model providers.
tactics.LEARNED_FILE = os.path.join(os.path.dirname(VOICE_OUTBOX), "learned_recipes.json")
SURVIVAL_MEMORY = os.path.join(os.path.dirname(VOICE_OUTBOX), "survival_learning.json")
# Eigene Ablagen (drop) pro Agent: loot_area/explore_step/survival lassen sie
# liegen, statt sie zwei Minuten später wieder einzusammeln (Viktor 08.09.).
tactics.DROP_LEDGER = os.path.join(os.path.dirname(VOICE_OUTBOX), "dropped_items.json")
# Kritische Welt-Ereignisse (run_agent-Watcher: Blutung/Gefahr/Bewusstlos)
# brechen laufende Langaktionen ab - wie Spieler-Funk, nur über diese Datei.
BRIDGE.urgent_flag = os.path.join(os.path.dirname(VOICE_OUTBOX), "urgent_event.json")
# Tod-Sperre (C1): run_agent schreibt beim Tod SOFORT death_epoch.txt ins
# agent_home. Ist diese Epoche jünger als der Start dieses MCP-Prozesses,
# gehört der Körper nicht mehr diesem Gehirn - Bridge.send wirft dann.
BRIDGE.death_epoch_file = os.path.join(os.path.dirname(VOICE_OUTBOX), "death_epoch.txt")
BRIDGE.session_start = time.time()


def _max_chat_id(state) -> int:
    """Höchste Chat-id im State (0 ohne Chat/State)."""
    try:
        return max([int(m.get("id", 0) or 0) for m in ((state or {}).get("chat") or [])
                    if isinstance(m, dict)] or [0])
    except (TypeError, ValueError, AttributeError):
        return 0


# VI-10: Jeder neue MCP-Prozess (Rotation, Respawn) begann mit 0 und zeigte im
# ersten observe bis zu 5 alte Zeilen aus dem 30er-Ringpuffer als CHAT (NEU).
# Neue Chats liefert der Runner ohnehin als Weckruf.
try:
    LAST_CHAT_ID = _max_chat_id(BRIDGE.read_state())
except Exception:
    LAST_CHAT_ID = 0
LAST_INV_SIG = None  # Inventar-Kennung des letzten observe (Delta-Erkennung)

# Gemeinsamer Klartext-Funkkanal (alle NPCs): wird ein Funkspruch für den
# Bildschirm latinisiert (Nicht-Latein-Sprache), legen wir hier die NATIVE
# Fassung ab - so liest ein ANDERER NPC den Originaltext statt das verstümmelte
# Transliterat (Pinyin/Buckwalter). Key = der latinisierte Text (exakte
# Zuordnung beim Empfänger). Die Bildschirm-/Audio-Pfade bleiben unverändert.
RADIO_NATIVE = os.path.join(REPO_DIR, "arena", "radio_native.jsonl")

# equip-Circuit-Breaker (geteilt von equip_best UND equip_melee, beide rufen
# denselben Mod-"equip"-Befehl): aufeinanderfolgende Fehlschläge, damit der NPC
# nicht stur dieselbe scheiternde Aktion wiederholt - z.B. die Spawn-Race
# "Equip-Ziel verschwunden", die equip_melee bisher in Schleife lief.
_EQUIP_FAILS = 0
_EQUIP_LAST_FAIL = 0.0
# Letztes equip_best/equip_melee-Ergebnis (MVS-4): ruft das Modell engage()
# im selben Schritt wie ein gescheitertes equip, warnt engage, womit der NPC
# wirklich kämpft (Konrad 24.10.: "Hand blockiert", Kampf lief mit Ruger).
_LAST_EQUIP = {"t": 0.0, "ok": True, "detail": ""}

# Bewegungs-Circuit-Breaker (gleiches Muster wie _EQUIP_FAILS): das Audit vom
# 03.07. zeigte 352-s-Züge mit 13 LLM-Schritten aus flee/unstick/move_to-
# Retry-Schleifen (Igor 12:11). Das LLM soll solche Schleifen NICHT selbst
# fahren - nach 2 Fehlschlägen/Versuchen liefert der dritte Aufruf nur noch
# eine Anleitung (travel_to hat das Wall-Following) statt ein Bridge-Kommando.
_MOVE_FAILS = 0        # move_to-"failed" in Folge (Fenster 120 s)
_MOVE_LAST_FAIL = 0.0
_UNSTICK_CALLS = 0     # unstick-AUFRUFE (nicht Fehlschläge) im 90-s-Fenster
_UNSTICK_LAST = 0.0


def _radio_native(ascii_text: str, native_text: str) -> None:
    """Native Fassung eines latinisierten Funkspruchs in den gemeinsamen Kanal
    schreiben, damit andere NPCs das Original lesen. Nur bei echter Latinisierung."""
    try:
        os.makedirs(os.path.dirname(RADIO_NATIVE), exist_ok=True)
        # Wachstum begrenzen: bei >512 KB neu anfangen (reiner Info-Funk, per
        # ascii-Key zugeordnet - alte Zeilen sind danach höchstens harmlos weg).
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
# Die Bots hören Befehle unter dem Funk-Namen, follow/regroup/give_to der Mod
# brauchen aber den Profilnamen - _resolve_player_name übersetzt zwischen beiden.
PLAYER_RADIO_NAME = os.environ.get("ISU_MIC_NAME", "Player")
PLAYER_PROFILE_NAME = os.environ.get("ISU_PLAYER_NAME", "")

mcp = FastMCP("dayz")


# ------------------------------------------------------------ Alias-Normalizer
# Zentrale Stelle, die falsch geratene Parameternamen und triviale Wertfehler
# auffängt, BEVOR ein Tool die Bridge anspricht. Vorbild ist das bestehende
# pickup-Alias-Muster (classname/item_name/item/name). Korrekte Aufrufe ändern
# sich nicht: _first nimmt einfach den ersten nicht-leeren Kandidaten, und
# _match_classname lässt einen Wert unverändert, solange kein besserer
# Classname aus dem letzten State bekannt ist.

# Bekannte Classnames aus dem letzten observe (Inventar + Umgebung). Für die
# case-insensitive/Teilstring-Korrektur eines vertippten Classnames.
KNOWN_CLASSNAMES: set[str] = set()

# Übersetzung falsch geratener Parameternamen auf den kanonischen Namen. Rein
# dokumentarisch/als Referenz - die Tools falten ihre Aliase über _first, diese
# Tabelle hält die vereinbarte Zuordnung an einer Stelle fest.
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
    Teilstring. Findet sich nichts Eindeutiges, bleibt der Wert unverändert -
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
    return n                            # mehrdeutig/unbekannt -> unverändert


def _needle(*cands: str) -> str:
    """Zentrale Item-Argument-Auflösung: Alias falten + Classname korrigieren."""
    raw = _first(*cands)
    return _match_classname(raw) if raw else raw


def _nearest_classnames(needle: str, n: int = 3) -> list[str]:
    """Die n nächstliegenden bekannten Classnames (difflib) zu einem Wert."""
    if not needle or not KNOWN_CLASSNAMES:
        return []
    return difflib.get_close_matches(needle, sorted(KNOWN_CLASSNAMES),
                                     n=n, cutoff=0.3)


def _did_you_mean(needle: str) -> str:
    """Anhängsel ' Meintest du: a, b, c?' für eine unauflösbare Item-Angabe,
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
#     CharcoalTablets töten Erreger generisch (CharcoalMdfr, ~2.85 Agents/s).
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
    """True, wenn ein Inventar-Classname needle als Teilstring enthält."""
    low = (needle or "").lower()
    return any(low in cn.lower() for cn in _inv_classnames(state) if cn)


# --------------------------------------------------------------- Fernreise
# travel_to startet einen Hintergrund-Thread, der move_to-Segmente (~150 m)
# Richtung Fernziel kettet, damit der Zug SOFORT enden kann (Token-Disziplin).
# Der Thread hält die intent-Datei aktuell und meldet Ankunft/Abbruch über
# travel_event_<id>.json (Schnittstelle 1). stop()/follow()/regroup()/move_to
# brechen eine laufende Reise ab; ein neues travel_to ersetzt das alte.
_TRAVEL_SEG = 150.0        # Segmentlänge in m
_TRAVEL_ARRIVE = 6.0       # Ankunftsschwelle in m
_travel_lock = threading.Lock()
_travel_thread: threading.Thread | None = None
_travel_stop: threading.Event | None = None
# Ziel der laufenden Reise (RESP-9): _abort_travel gibt es zurück, damit
# follow/regroup/engage sagen können, dass eine Reise abgebrochen wurde.
_travel_goal: tuple[float, float] | None = None
# Verdrängte Reise-Segmente (BRIDGE-1/MOD-10): so oft wird dasselbe Segment
# nach dem Verdrängen durch einen fremden Befehl neu gesendet.
_TRAVEL_DISPLACED_RETRIES = 3


def _hud_text(english: str, german: str) -> str:
    """Localize only built-in HUD labels; agent-authored text stays untouched."""
    return german if AGENT_LANG == "de" else english


def _write_intent_line(text: str) -> None:
    """intent_<id>.txt schreiben (Nameplate-Gedankenzeile), bildschirmtauglich
    latinisiert und auf Länge gekappt. Best effort."""
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
        _write_native_intent(native)
    except OSError:
        pass


def _write_native_intent(native: str) -> None:
    """intent_native_<id>.txt IMMER mitschreiben, NACH der Anzeige-Datei
    (HUD-5/VI-M2, 24.10.): Bisher entstand sie nur bei abweichender
    Bildschirmfassung, der Orchestrator las sie aber bevorzugt - eine alte
    lange Absicht überdeckte so minutenlang frische kurze Zeilen und die
    Reise-Fortschrittszeilen dieses Moduls. Wirft OSError an den Aufrufer."""
    npath = os.path.join(BRIDGE.dir, f"intent_native_{BRIDGE.npc_id}.txt")
    ntmp = npath + ".tmp"
    nline = native if len(native) <= 120 else native[:117] + "..."
    with open(ntmp, "w", encoding="utf-8") as f:
        f.write(nline + "\n")
    os.replace(ntmp, npath)


def _write_travel_event(event: str, x: float, z: float, detail: str = "") -> None:
    """travel_event_<id>.json neben intent_<id>.txt ablegen (Schnittstelle 1).
    run_agent-EventWatcher liest, löscht die Datei und weckt den Agenten."""
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
# Großhindernissen (See, Militärzaun, Sumpf) - eAI-Pathfinding findet
# innerhalb EINES 150-m-Segments keinen Weg drumherum, meldet "45s ohne
# Fortschritt", und der NPC wirkt für den Spieler "festgeglitcht" (Igor
# 03.07. südlich vom Livonia-Lager). Der Thread weicht deshalb selbst aus,
# wie Igor es manuell tat ("nach Süden komme ich raus, jetzt westlich
# herum"): quer zur Ziellinie (90/135 Grad), bleibt der einmal gefundenen
# Seite treu und probt den Zielkurs mit wachsendem Abstand (jede blockierte
# Probe kostet im Spiel 45 s, darum exponentiell 1-2-4 Segmente).
_TRAVEL_DETOUR_ANGLES = (90.0, 135.0)   # Grad quer zur Ziellinie
_TRAVEL_DETOUR_SEG = 200.0              # längere Ausweich-Segmente
_TRAVEL_SEG_TIMEOUT = 60.0    # s pro Zielkurs-Segment (150 m): die Mod meldet
                              # "45 s ohne Fortschritt" ohnehin früher, 60 s
                              # decken den normalen Lauf plus Puffer (vorher 90 -
                              # blockierte Proben kosteten unnötig lange).
_TRAVEL_DETOUR_TIMEOUT = 75.0  # s pro Ausweich-Segment: 200 m statt 150 m Weg,
                               # also proportional mehr Laufzeit-Puffer nötig.
_TRAVEL_MAX_SEGMENTS = 400              # Notbremse gegen Endlos-Pendeln
_TRAVEL_MAX_DETOUR = 2500.0             # max. Quergang pro Hindernis-SEITE (m);
                                        # große Livonia-Seen sind ~1-2 km breit.
                                        # Beide Seiten erschöpft -> stuck-Event:
                                        # das soll das GEHIRN entscheiden
                                        # (großer Umweg/anderes Ziel).


def _travel_worker(gx: float, gz: float, stop: threading.Event) -> None:
    """Hold ownership even between commands, visible to the Runner controller."""
    global _travel_goal
    path = os.path.join(BRIDGE.dir, f"travel_active_{BRIDGE.npc_id}.json")
    token = f"{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
    lease = {"pid": os.getpid(), "token": token, "x": gx, "z": gz}
    BRIDGE.survival_travel_active = True
    try:
        os.makedirs(BRIDGE.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(lease, stream)
        os.replace(tmp, path)
        _travel_body(gx, gz, stop)
    except RuntimeError as e:
        # Tod-Sperre (C1): das alte Gehirn darf den neuen Körper nicht mehr
        # laufen lassen - die Reise endet still, kein Traceback im MCP-Log.
        if bridge_mod.DEATH_TEXT not in str(e):
            raise
    finally:
        # Reise vorbei (Ankunft/stuck/Abbruch): kein Ziel mehr, das ein
        # späteres _abort_travel als "unterbrochen" melden dürfte.
        with _travel_lock:
            if _travel_stop is stop:
                _travel_goal = None
        try:
            with open(path, encoding="utf-8") as stream:
                current = json.load(stream)
            if current.get("token") == token:
                os.unlink(path)
                BRIDGE.survival_travel_active = False
        except (OSError, ValueError):
            BRIDGE.survival_travel_active = False


def _travel_body(gx: float, gz: float, stop: threading.Event) -> None:
    """Kettet move_to-Segmente Richtung (gx,gz). Fortschritt = der NPC hat
    sich >=30 m bewegt (auch seitlich - Umwege zählen). Bei Blockade: erst
    unstick, dann Wall-Following quer zur Ziellinie. Erst wenn beide Seiten
    scheitern, geht ein stuck-Event ans Gehirn. Hintergrund-Thread."""
    segments = 0
    committed = 0        # zuletzt erfolgreiche Hindernis-Seite (nur Reihenfolge)
    backoff = 1          # Ausweich-Segmente pro Zielkurs-Probe (1-2-4)
    straight_ok = 0      # Zielkurs-Erfolge in Folge
    unstick_used = False
    excluded: set = set()  # Seiten ohne Durchkommen (Netto-Versatz erschöpft)
    block_d = None       # Zieldistanz beim ERSTEN Block: das Hindernis gilt
                         # erst als überwunden, wenn wir DEUTLICH näher am
                         # Ziel sind (Marge 150 m). Zwei freie Geradeaus-
                         # Segmente allein reichen nicht - unterhalb einer
                         # diagonalen Wand kann man endlos "erfolgreich"
                         # parallel laufen, ohne je vorbeizukommen.
    block_pos = None     # Position des ersten Blocks
    block_n = None       # Einheitsvektor quer zur Ziellinie am Block (Seite +1)
    d_best = None        # beste je erreichte Zieldistanz am Hindernis
    stagnation = 0       # Wall-Follow-Runden ohne Annäherung (Taschen-Falle)

    def _lateral(px_: float, pz_: float) -> float:
        """Netto-Seitenversatz zur Blockstelle, quer zur Ziellinie. Immun
        gegen Zickzack: nur der ECHTE Querabstand zählt gegen das Budget,
        nicht der gelaufene Weg (der Zickzack aus Quergang + Probe frisst
        sonst das Budget der richtigen Seite auf, bevor sie herumführt)."""
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
        """Ein move_to-Segment fahren; liefert die zurückgelegte Distanz.
        Fortschritt misst der WEG, nicht die Zieldistanz (ein Umweg um einen
        See entfernt sich zeitweise vom Ziel und zählt trotzdem)."""
        nonlocal segments
        segments += 1
        p = _pos()
        if p is None:
            return -1.0
        # stop_event: _abort_travel bricht ein LAUFENDES Segment sofort ab -
        # ohne das hing der Worker bis zu 75 s im Segment, während das
        # nächste Tool schon sein eigenes Bewegungskommando feuerte (Zucken
        # zwischen zwei Zielen).
        tx = p[0] + math.cos(bearing) * seg
        tz = p[1] + math.sin(bearing) * seg
        for _ in range(_TRAVEL_DISPLACED_RETRIES + 1):
            # Abbruch VOR jedem Senden prüfen: _wait_slot_free kehrt bei stop
            # sofort zurück, und Bridge.send schreibt commands.json, bevor
            # wait_status das stop_event sieht - ohne diese Prüfung führte die
            # Mod nach _abort_travel noch ein verwaistes move_to aus.
            if stop.is_set():
                break
            res = BRIDGE.run("move_to", x=tx, z=tz, timeout=timeout,
                             stop_event=stop)
            if stop.is_set():
                break
            rdetail = str(res.get("detail") or "")
            if not (res.get("status") == "interrupted"
                    and rdetail.startswith("verdraengt")):
                break
            if bridge_mod.PLAYER_ORDER_MARK in rdetail:
                # Der Spieler hat per Befehlsrad übernommen - sein Befehl
                # schlägt die Reise, NICHT zurück-verdrängen.
                _write_travel_event("aborted", gx, gz,
                                    "Befehl des Spielers (Befehlsrad) hat "
                                    "Vorrang - Reise NICHT neu starten, außer "
                                    "er bittet darum")
                stop.set()
                break
            # Ein fremder Befehl (Tool-Thread: say/equip/reload/eat ...,
            # Runner) hat das Segment im Ein-Slot-Dispatch der Mod verdrängt
            # (BRIDGE-1/MOD-10). Das ist KEIN Hindernis: warten, bis er fertig
            # ist, und dasselbe Segment neu senden - sonst zählte es als
            # Blockade und löste unstick/Wall-Following aus.
            _wait_slot_free(stop, 20.0)
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
            _write_intent_line(_hud_text(
                f"Arrived at {gx:.0f}/{gz:.0f}",
                f"Angekommen bei {gx:.0f}/{gz:.0f}"))
            _write_travel_event("arrived", gx, gz, f"Distanz {d:.0f} m")
            return
        goal_bearing = math.atan2(gz - pz, gx - px)

        # 1) Zielkurs (gerade aufs Ziel)
        _write_intent_line(_hud_text(
            f"Heading to {gx:.0f}/{gz:.0f}, {d:.0f} m left",
            f"Unterwegs nach {gx:.0f}/{gz:.0f}, noch {d:.0f} m"))
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
        #    Zielkurs wiederholen (häufigster Fall: Gelände-Verkeilung).
        if not unstick_used:
            unstick_used = True
            BRIDGE.run("unstick", timeout=10)
            continue

        # 3) Wall-Following: bekannte Seite (committed) zuerst, Sackgassen-
        #    Seiten (Budget erschöpft) sind ausgeschlossen; pro Seite 90 dann
        #    135 Grad. 'backoff' Segmente am Stück, damit nicht nach jedem
        #    Quergang eine teure 45-s-Probe verpufft.
        sides = [committed] if committed else [1, -1]
        if committed:
            sides.append(-committed)
        sides = [s for s in sides if s not in excluded]
        success_side = 0
        for side in sides:
            for ang in _TRAVEL_DETOUR_ANGLES:
                bearing = goal_bearing + math.radians(side * ang)
                _write_intent_line(_hud_text(
                    f"Obstacle ahead - changing course ({side * ang:+.0f} degrees)",
                    f"Hindernis voraus - ändere Kurs ({side * ang:+.0f} Grad)"))
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
        # Bilanz der Wall-Follow-Runde: Netto-Seitenversatz und Annäherung.
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
            _write_intent_line(_hud_text(
                "Dead end on this side - turning around",
                "Sackgasse auf dieser Seite - drehe um"))
        if lat < -_TRAVEL_MAX_DETOUR and -1 not in excluded:
            excluded.add(-1)
            committed = 0
            backoff = 1
            stagnation = 0
            _write_intent_line(_hud_text(
                "Dead end on this side - turning around",
                "Sackgasse auf dieser Seite - drehe um"))
        if len(excluded) >= 2 or stagnation >= 15:
            success_side = 0   # aussichtslos -> stuck-Event unten
        if not success_side:
            q = _pos() or (px, pz)
            nd = math.hypot(gx - q[0], gz - q[1])
            _write_intent_line(_hud_text(
                f"Cannot make progress toward {gx:.0f}/{gz:.0f}",
                f"Komme nicht weiter Richtung {gx:.0f}/{gz:.0f}"))
            _write_travel_event(
                "stuck", gx, gz,
                f"blockiert trotz Ausweichversuchen (beide Seiten, 90/135 "
                f"Grad), noch {nd:.0f} m. Großes Hindernis (See/Zaun/Sumpf)? "
                f"Wähle einen deutlichen Umweg (travel_to mit Zwischenziel) "
                f"oder ein anderes Ziel.")
            return
        committed = success_side
        backoff = min(backoff * 2, 4)


def _wait_slot_free(stop: threading.Event, max_wait: float) -> None:
    """Warten, bis im Befehlsslot der Mod nichts mehr läuft (höchstens
    max_wait s, sofort raus bei stop). Für den Reise-Thread nach einem
    verdrängten Segment: erst den fremden Befehl fertig werden lassen,
    statt ihn seinerseits zu verdrängen."""
    end = time.monotonic() + max_wait
    while time.monotonic() < end and not stop.is_set():
        cmd = (BRIDGE.read_state() or {}).get("command") or {}
        if cmd.get("status") != "running":
            return
        time.sleep(0.5)


def _abort_travel() -> tuple[float, float] | None:
    """Laufende Reise abbrechen (stop/follow/regroup/move_to greifen darauf zu).

    Wartet kurz auf das ECHTE Ende des Reise-Threads: der steigt dank
    stop_event binnen ~1 s aus einem laufenden Segment aus. Ohne das Join
    feuerte das nächste Tool sein Bewegungskommando, während der Worker
    noch eines in der Mailbox hatte.

    Rückgabe (RESP-9): das Ziel (x, z) der WIRKLICH abgebrochenen Reise oder
    None. Aufrufer, die das nicht brauchen, ignorieren den Wert."""
    global _travel_thread, _travel_stop, _travel_goal
    with _travel_lock:
        t = _travel_thread
        goal = _travel_goal
        if _travel_stop is not None:
            _travel_stop.set()
        _travel_thread = None
        _travel_stop = None
        _travel_goal = None
    alive = t is not None and t.is_alive()
    if alive:
        t.join(timeout=3.0)
    return goal if alive else None


def _travel_abort_hint(goal, ended: bool = False) -> str:
    """Kurzer Zusatz, wenn ein Tool eine laufende Reise abgebrochen hat
    (RESP-9: Viktors Zusage "wir kommen zu dir" verfiel still). ended=True
    für follow/regroup (Planwechsel), sonst Unterbrechung (engage). Kein
    automatischer Neustart - das entscheidet das Gehirn."""
    if not goal:
        return ""
    gx, gz = goal
    if ended:
        return (f" Hinweis: deine Reise nach x={gx:.0f} z={gz:.0f} ist damit "
                f"beendet. Hast du jemandem zugesagt zu kommen, sag per say "
                f"kurz Bescheid.")
    return (f" Deine Reise nach x={gx:.0f} z={gz:.0f} ist unterbrochen - bei "
            f"Bedarf travel_to erneut.")


def _brain_dead() -> bool:
    """Tod-Sperre (C1) aktiv? Tolerant gegen Stub-/Test-Bridges ohne die
    Methode."""
    fn = getattr(BRIDGE, "died_since_start", None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def _squad_now(state) -> str:
    """squad_lines (C2) für die eigene Position aus diesem State, sonst ""."""
    npc = (state or {}).get("npc") or {}
    if not (npc.get("spawned") and npc.get("alive")):
        return ""
    try:
        return bridge_mod.squad_lines(getattr(BRIDGE, "npc_id", args.npc_id),
                                      float(npc.get("pos_x", 0.0)),
                                      float(npc.get("pos_z", 0.0)))
    except Exception:
        return ""


def _chat_names() -> set[str]:
    """Bekannte Namen (Kleinbuchstaben) für die Adressaten-Markierung im
    Chat: Roster-Kameraden plus alle Spieler-Namen/Aliase."""
    names = set(_roster_names())
    try:
        names.update(_player_name_variants())
    except Exception:
        pass
    return names


def _observe_text(full: bool = False, squad: bool = True) -> str:
    global LAST_CHAT_ID, LAST_INV_SIG
    if _brain_dead():
        return bridge_mod.DEATH_TEXT   # nicht den NEUEN Körper zeigen (DR-1)
    state = BRIDGE.read_state()
    _refresh_known_classnames(state)
    sig = inventory_signature(state)
    # Inventar nur ausschreiben, wenn es sich seit dem letzten observe
    # geändert hat (oder full angefordert ist) - das spart spürbar Tokens
    inv_unchanged = (not full) and LAST_INV_SIG is not None and sig == LAST_INV_SIG
    LAST_INV_SIG = sig
    text, LAST_CHAT_ID = format_observation(
        state, LAST_CHAT_ID, inv_unchanged=inv_unchanged, compact=not full,
        own_name=AGENT_NAME, known_names=_chat_names())
    # RESP-1: Kameraden außerhalb der 100-m-UMGEBUNG live (squad_state.json,
    # nur bei laufendem Orchestrator und frischer Datei).
    if squad:
        sq = _squad_now(state)
        if sq:
            text += "\nKAMERADEN (live): " + sq
    return text


def _safe_state() -> dict:
    """State lesen, ohne an einer Stub-/Test-Bridge zu scheitern (best effort)."""
    try:
        return BRIDGE.read_state() or {}
    except (AttributeError, OSError, ValueError, TypeError):
        return {}


def _outcome(result: dict, success_text: str, needle: str = "") -> str:
    status = result.get("status", "unbekannt")
    detail = result.get("detail") or ""
    if status == "done":
        return f"{success_text} {detail}".rstrip()
    if status == "interrupted":
        return bridge_mod.interrupt_text(result)
    if status == "running":
        # VI-M5: eigene AKTUELLE Position mitgeben - nach langen Märschen
        # rechnete das Gehirn mit der letzten observe-Position und schätzte
        # Entfernungen grob falsch (Birgit: "gut hundert Meter" statt 690 m).
        npc = _safe_state().get("npc") or {}
        here = ""
        if npc.get("spawned") and npc.get("alive") and "pos_x" in npc:
            here = (f"du bist jetzt bei x={float(npc.get('pos_x', 0.0)):.0f} "
                    f"z={float(npc.get('pos_z', 0.0)):.0f}")
        dist = result.get("dist_to_target", -1.0)
        if dist is not None and dist >= 0:
            sep = f", {here}" if here else ""
            return (f"Laeuft noch (Distanz {dist:.0f} m{sep}). Mit observe() "
                    f"prüfen oder wait() nutzen.")
        if here:
            return f"Laeuft noch ({here}). Mit observe() prüfen oder wait() nutzen."
        return "Laeuft noch. Mit observe() prüfen oder wait() nutzen."
    if status not in ("done", "failed", "interrupted"):
        # Timeout/keine Antwort: der Server führt den Befehl evtl. NOCH aus.
        # Das früher gemeldete "Fehlgeschlagen:" (ohne Detail) ließ den
        # Agenten dieselbe Aktion sofort neu starten -> Doppelbefehle.
        return ("KEINE RÜCKMELDUNG vom Server binnen der Wartezeit - die "
                "Aktion läuft möglicherweise noch. NICHT sofort wiederholen: "
                "prüfe erst mit observe, was daraus geworden ist.")
    # Unauflösbare Item-Angabe: nächstliegende bekannte Classnames anbieten
    # (nur wenn der needle NICHT ohnehin ein bekannter Classname ist - dann
    # liegt der Fehler woanders und der Vorschlag wäre nur Rauschen).
    hint = ""
    if needle and needle not in KNOWN_CLASSNAMES:
        hint = _did_you_mean(needle)
    return f"Fehlgeschlagen: {detail}{hint}"


@mcp.tool()
def observe(full: bool = False) -> str:
    """Aktuelle Lage ansehen: Position, Vitalwerte, Hand, Inventar, Umgebung,
    neue Chat-Nachrichten (mit Alter und Adressat) und Status der letzten
    Aktion. Immer zuerst aufrufen, wenn du die Lage nicht sicher kennst.
    Unverändertes Inventar wird als Einzeiler gezeigt; observe(full=true)
    erzwingt die volle Liste. Läuft das Lagezentrum, zeigt die Zeile
    "KAMERADEN (live)", wo die anderen JETZT sind - Koordinaten aus Chat oder
    Funk sind dagegen alte Momentaufnahmen."""
    return _observe_text(full=full)


@mcp.tool()
def survival_tick(explore: bool = True) -> str:
    """Einen kurzen autonomen Überlebensschritt starten, ohne LLM-Folgekette.
    Priorisiert Blutung, Gefahr, Durst, Hunger, Temperatur, Krankheit, Inventar,
    Loot und Jagd. Kehrt sofort zurück; nachfolgende Server-Snapshots prüfen
    Ergebnisse und lernen dauerhaft. Laufende Reise/Folgeaufträge bleiben aktiv.
    explore=False beschränkt auf sichtbare Versorgung, ohne Erkundungsmarsch.
    Bei needs_llm=true das genannte Problem gezielt selbst lösen."""
    return json.dumps(survival.survival_tick(
        BRIDGE, AGENT_NAME, SURVIVAL_MEMORY, allow_explore=explore), ensure_ascii=False)


@mcp.tool()
def survival_memory() -> str:
    """Messbare Survival-Erfahrungen, letzte Ergebnisse und echte Fähigkeiten.
    verified bedeutet durch Inventar/Vitalwerte/Position belegtes Ergebnis,
    nicht bloß einen Erfolgstext. Medikamenteinnahme beweist keine Heilung.
    Die Erfahrung bleibt bei einem Modellwechsel und nach Neustarts erhalten."""
    return json.dumps(survival.memory_summary(SURVIVAL_MEMORY), ensure_ascii=False)


@mcp.tool()
def harvest_crops() -> str:
    """Reife Pflanzen aus einem sichtbaren Garten ernten (max. 3 m).
    Nutzt echte PlantBase.Harvest der Servermod. Säen/Wässern ist damit nicht
    implementiert. Liegt der Garten weiter weg, zuerst move_to/travel_to nutzen;
    abgelegte Ernte danach mit pickup oder loot_area aufnehmen."""
    state = BRIDGE.read_state() or {}
    gardens = [e for e in state.get("nearby", []) if e.get("kind") == "garden"
               and e.get("harvestable") is True]
    if not gardens:
        return "Kein erntereifer Garten sichtbar (oder Servermod ohne Garten-Telemetrie)."
    if min(e.get("distance", 999) for e in gardens) > 3:
        garden = min(gardens, key=lambda e: e.get("distance", 999))
        return f"Erntereifer Garten bei x={garden.get('x')} z={garden.get('z')}; erst auf 3 m naehern."
    return _outcome(BRIDGE.run("harvest_crops", timeout=20), "Pflanze geerntet; Ertrag am Boden aufnehmen:")


@mcp.tool()
def intent(text: str) -> str:
    """Setze deine aktuelle Absicht in EINER kurzen deutschen Zeile (z.B.
    "Wasser knapp, ich gehe zum Brunnen"). Sie schwebt als Gedanke über
    deinem Kopf, ist KEIN hörbarer Funkspruch. Echte Umlaute, höchstens
    ~8 Wörter. Blockiert nicht - setze sie locker vor größeren Schritten."""
    native = " ".join(text.split())
    # Bildschirm-Fassung latinisieren (Font kann kein CJK/...), dann auf Länge
    # kappen. Die NATIVE Fassung legen wir separat ab (intent_native_<id>.txt),
    # damit der Orchestrator-Sitrep den Originalvorsatz an andere NPCs funkt,
    # nicht das verstümmelte Transliterat.
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
        # Native Fassung IMMER (nicht nur bei line != native) und NACH der
        # Anzeige schreiben - sonst überdeckte eine alte native Absicht die
        # frische kurze im Sitrep (HUD-5/VI-M2).
        _write_native_intent(native)
    except OSError:
        return "Absicht konnte nicht gesetzt werden."
    return f"Absicht gesetzt: {line}"


@mcp.tool()
def move_to(x: float, z: float) -> str:
    """Zu Map-Koordinaten laufen (x = West-Ost, z = Süd-Nord, wie auf iZurvive).
    Der NPC läuft serverseitig WEITER, auch wenn "Laeuft noch" zurückkommt.
    Bei "Laeuft noch" NICHT sofort wiederholen - beende deinen Zug. Du läufst
    automatisch weiter und prüfst beim nächsten Aufwachen die neue Position.
    Mehrfaches move_to aufs selbe Ziel im selben Zug kostet nur Tokens und bringt
    nichts. Unerreichbare Ziele schlagen nach 45 s ohne Fortschritt fehl.
    Für WEITE Strecken oder wenn Hindernisse im Weg liegen: travel_to nehmen -
    das weicht Hindernissen selbst aus und läuft im Hintergrund."""
    global _MOVE_FAILS, _MOVE_LAST_FAIL
    # Fenster abgelaufen -> Zähler zurücksetzen. Sonst bleibt er auf 2 stehen
    # und EIN weiterer Fehlschlag (auch Stunden später) sperrt sofort wieder
    # für 120 s, obwohl es kein "in Folge" mehr ist.
    if _MOVE_FAILS and (time.time() - _MOVE_LAST_FAIL) >= 120.0:
        _MOVE_FAILS = 0
    # Circuit-Breaker (Muster _EQUIP_FAILS): nach 2 "failed" binnen 120 s NICHT
    # ein drittes Mal stur wiederholen - Anleitung statt Bridge-Kommando, sonst
    # verbrennt der NPC 300+-s-Züge in move_to/unstick-Schleifen (Audit 03.07.).
    if _MOVE_FAILS >= 2 and (time.time() - _MOVE_LAST_FAIL) < 120.0:
        return ("move_to hakt hier - es ist gerade 2x in Folge fehlgeschlagen. "
                "Ruf es JETZT nicht noch einmal. Nutze travel_to (weicht "
                "Hindernissen selbst aus), unstick, oder wähle ein deutlich "
                "anderes Ziel. Beende deinen Zug.")
    # Schon am Ziel? Dann KEINEN neuen Marsch starten - sonst feuert das Gehirn
    # minutenlang identische move_to auf einen bereits erreichten Punkt (jeder
    # Lauf meldet sofort "Angekommen", das LLM wiederholt endlos). Schwelle 4 m
    # liegt knapp über der Mod-Ankunftsschwelle (3 m, BR 2 m).
    # WICHTIG: der Check läuft VOR _abort_travel - ein move_to auf den eigenen
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
    # Manuelles move_to hat Vorrang vor einer laufenden Fernreise (sonst kämpfen
    # zwei Bewegungsbefehle um dieselbe Mailbox).
    _abort_travel()
    # Timeout 50 s: die Mod gibt nach 45 s ohne Fortschritt ohnehin auf; der
    # "Laeuft noch"-Fall blockiert den Agenten so 25 s kürzer als früher (75 s)
    # und er bleibt für den Spieler ansprechbar.
    result = BRIDGE.run("move_to", x=x, z=z, timeout=50, interruptible=True)
    status = result.get("status")
    if status == "failed":
        _MOVE_FAILS += 1
        _MOVE_LAST_FAIL = time.time()   # nach dem Lauf messen, nicht davor
    elif status == "done":
        _MOVE_FAILS = 0   # Erfolg -> Zähler zurücksetzen ("running" zählt nicht)
    return _outcome(result, "Angekommen.")


@mcp.tool()
def pickup(classname: str = "", item_name: str = "", item: str = "",
           name: str = "") -> str:
    """Bodenitem im Umkreis von 50 m aufheben (hinlaufen inklusive). Nimm den
    classname so wie in observe() unter UMGEBUNG (kind=item) - ein Teilstring
    reicht auch (z.B. "WolfSteak" für "WolfSteakMeat"). Beispiel:
    pickup(classname="Apple"). WARNUNG: NUR ganz ohne Argument wird das
    nächste BELIEBIGE Item genommen - gib also immer einen classname an, sonst
    hebst du irgendwas auf."""
    needle = _needle(classname, item_name, item, name)
    # Bewegungs-Tool übernimmt die Beine - eine laufende Fernreise würde sonst
    # um die Mailbox konkurrieren und fälschlich "stecken geblieben" melden
    # (Birgits Fehlabbruch 03.07.). Das Gehirn startet travel_to danach neu.
    _abort_travel()
    result = BRIDGE.run("pickup", text=needle, timeout=60, interruptible=True)
    out = _outcome(result, "Aufgehoben:", needle=needle)
    if result.get("status") == "done":
        # Gezieltes Aufheben hebt die Ablage-Sperre auf: das Gehirn will das
        # Item ausdrücklich wieder (Ballast-Filter gilt nur für Auto-Loot).
        picked = (result.get("detail") or "").strip().split(" ")[0]
        for cn in {picked, needle}:
            if cn:
                tactics.forget_drop(cn)
    # Munitionskiste sofort aufmachen - sonst zeigt sie "x0" und wirkt leer,
    # NPCs werfen sie dann irrtümlich weg statt die Munition zu nutzen.
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
    Verschlossene Konserven werden automatisch geöffnet (braucht Dosenoeffner
    oder Messer). Schlägt fehl, wenn das gewünschte Essen nicht da ist."""
    needle = _needle(classname, item_name, item, name)
    result = BRIDGE.run("eat", text=needle, timeout=30)
    return _outcome(result, "Gegessen:", needle=needle)


@mcp.tool()
def drink(classname: str = "", item_name: str = "", item: str = "",
          name: str = "") -> str:
    """Aus einem Getränk im Inventar trinken. OHNE Argument das erste; MIT
    classname gezielt dieses (Teilstring reicht). Verschlossene Dosen werden
    vorher geöffnet. Schlägt fehl, wenn nichts Passendes da ist."""
    needle = _needle(classname, item_name, item, name)
    result = BRIDGE.run("drink", text=needle, timeout=30)
    return _outcome(result, "Getrunken:", needle=needle)


@mcp.tool()
def harvest(animal: str = "") -> str:
    """Tierkadaver verwerten (Jagd): läuft zum nächsten toten Tier im
    Umkreis von 50 m (kind=animal_corpse in der Umgebung), zerlegt es und
    nimmt das Fleisch ins Inventar (rohes Fleisch vor dem Essen am Feuer
    braten!). Braucht ein Schneidwerkzeug (Messer/Machete/Axt) im Inventar.
    Optional animal=Teil des Klassennamens (z.B. "Cervus" für Hirsch)."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    result = BRIDGE.run("harvest", text=animal, timeout=90, interruptible=True)
    return _outcome(result, "Zerlegt:")


@mcp.tool()
def hunt(animal: str = "") -> str:
    """JAGEN (ganze Folgekette): lebendes Beutetier (Reh, Wildschwein, Kuh,
    Hase, Huhn - kind=animal in observe) anpirschen, erlegen und gleich
    zerlegen. Mit schussbereiter Feuerwaffe fällt das Tier auf 35 m; ohne
    musst du auf 4 m heran (klappt nur bei Huhn/Hase). Optional
    animal=Teil des Klassennamens (z.B. "Cervus" für Hirsch). Raubtiere
    (Wolf/Bär) sind Kampf: engage. Danach process_food oder cook_meal -
    rohes Fleisch macht krank."""
    _abort_travel()   # Pirsch übernimmt die Beine - Fernreise abbrechen
    return tactics.hunt(BRIDGE, animal=animal, log=lambda m: None)


@mcp.tool()
def process_food() -> str:
    """NAHRUNG VERARBEITEN (ganze Folgekette): alle Tierkadaver in 50 m
    zerlegen (harvest) und dann alles Rohe am Feuer garen - Feuer wird bei
    Bedarf selbst gebaut (5x WoodenStick + 1x Rag) und angezündet
    (Zündmittel nötig). Der eine Aufruf nach Jagd (hunt) oder Angeln
    (fish). Danach ist das Fleisch essbar (eat)."""
    _abort_travel()   # läuft zu Kadavern/Feuer - Fernreise abbrechen
    return tactics.process_food(BRIDGE, log=lambda m: None)


@mcp.tool()
def dress_best() -> str:
    """KLEIDUNG systematisch optimieren: prüft jeden Körper-Slot (Jacke,
    Hose, Schuhe, Kopf, Handschuhe, Weste, Rucksack) und zieht das beste
    verfügbare Stück an - aus dem Inventar oder vom Boden in 10 m.
    FRIERST du (VITALS/Wärme), zählt WÄRME; ist die Lage moderat, zählt
    STAURAUM (mehr Slots). Belegte Slots werden getauscht, Inhalt bleibt
    erhalten. Das Kleidungs-Gegenstück zu equip_best."""
    _abort_travel()   # wear kann bis 10 m laufen - Fernreise abbrechen
    return tactics.dress_best(BRIDGE, log=lambda m: None)


@mcp.tool()
def combine(a: str = "", b: str = "", item_a: str = "", item_b: str = "") -> str:
    """Zwei Gegenstände KOMBINIEREN (Herstellung): sucht das Rezept, das
    genau aus diesen beiden Materialien besteht, und craftet es. Beispiele:
    combine(a="WoodenStick", b="Rag") -> Torch; combine(a="LongWoodenStick",
    b="Rope") -> FishingRod; combine(a="SmallStone", b="SmallStone") ->
    StoneKnife. Kennt kein Rezept die Kombination, bekommst du passende
    Vorschläge. Alle Rezepte: recipes; Neues lernen: learn_recipe."""
    na = _needle(a, item_a)
    nb = _needle(b, item_b)
    return tactics.combine_items(BRIDGE, na, nb, log=lambda m: None)


@mcp.tool()
def wear(classname: str = "", item_name: str = "", item: str = "",
         name: str = "") -> str:
    """Kleidungsstück anziehen (gegen Kälte - die VITALS zeigen, ob du
    frierst). Sucht im Inventar UND am Boden im Umkreis von 10 m, ein
    vorheriges pickup ist nicht nötig. Ist der Körper-Slot belegt, wird
    automatisch getauscht; der Inhalt des alten Stücks wandert vorher in
    deine anderen Taschen. Meldet das Ergebnis "AM BODEN: ..." oder
    "ACHTUNG: ... liegt mit N Item(s) am Boden", dann pickup/loot_container
    darauf - sonst ist das Zeug weg."""
    needle = _needle(classname, item_name, item, name)
    if not needle:
        return "Bitte classname angeben, z.B. wear(classname=\"BeanieHat\")."
    _abort_travel()   # kann bis 10 m zum Bodenstück laufen - Fernreise abbrechen
    result = BRIDGE.run("wear", text=needle, timeout=20)
    return _outcome(result, "Angezogen:", needle=needle)


def _vehicle_block() -> str:
    """C8/DR-3: Im Fahrzeug sofort ein klarer Text statt eines Mod-Befehls
    (equip lief dort 16 Retry-Ticks bis "Hand blockiert?"). "" = zu Fuß."""
    npc = _safe_state().get("npc") or {}
    if npc.get("in_vehicle"):
        return tactics.VEHICLE_BLOCK_TEXT
    return ""


def _note_equip(ok: bool, detail: str) -> None:
    _LAST_EQUIP.update(t=time.time(), ok=bool(ok), detail=str(detail or ""))


@mcp.tool()
def equip_best() -> str:
    """Die beste verfügbare Waffe aus dem Inventar in die Hand nehmen.
    Berücksichtigt Waffenqualität und ob ein passendes Magazin da ist;
    ohne Munition wird notfalls eine Nahkampfwaffe gewählt."""
    global _EQUIP_FAILS, _EQUIP_LAST_FAIL
    # Fahrzeug zuerst - VOR dem Circuit-Breaker, damit der nicht hochzählt
    # und equip_best nach dem Aussteigen nicht 90 s gesperrt bleibt.
    vb = _vehicle_block()
    if vb:
        return vb
    now = time.time()
    # Circuit-Breaker: nach 2 Fehlschlägen in Folge (innerhalb 90 s) NICHT ein
    # drittes Mal stur wiederholen, sondern zum Strategiewechsel zwingen - sonst
    # verbrennt der NPC Züge mit derselben scheiternden Aktion (Konrad 20:28).
    if _EQUIP_FAILS >= 2 and (now - _EQUIP_LAST_FAIL) < 90.0:
        return ("equip_best ist gerade 2x in Folge gescheitert - ruf es JETZT nicht "
                "noch einmal. Löse es anders: observe (liegt die Waffe/Munition "
                "wirklich vor dir?), passende Munition suchen/nachladen, die Waffe "
                "per pickup in die Hand nehmen, oder funk dem Spieler dein Problem. "
                "Erst wieder equip_best, wenn sich die Lage geändert hat.")
    # Waffenwahl macht die MOD (CmdEquipBest): sie kennt den ECHTEN
    # Munitionsstand (Expansion_HasAmmo statt Namens-Heuristik) und verifiziert
    # per Tick, dass die Waffe wirklich in der Hand liegt. Die Python-Heuristik
    # (tactics.pick_best_weapon) bleibt nur für die Loot-Bewertung im Einsatz.
    result = BRIDGE.run("equip_best", timeout=30)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "verschwunden" in detail.lower():
        # Expansion KLONT die Waffe beim Take-to-Hands (Original gelöscht);
        # ältere Mod-Builds verlieren damit den Zeiger und melden
        # "verschwunden", obwohl die Waffe in der Hand liegt (Igor 20:51).
        # Handinhalt prüfen, bevor das als Fehlschlag zählt.
        time.sleep(2.0)
        hands = (BRIDGE.read_state() or {}).get("npc", {}).get("in_hands", "")
        if hands and (tactics.classify_weapon(hands) or tactics.classify_melee(hands)):
            result = {"status": "done", "detail": hands}
            detail = hands
        else:
            result = BRIDGE.run("equip_best", timeout=30)
            detail = result.get("detail") or ""
    if result.get("status") == "done":
        _EQUIP_FAILS = 0
        _note_equip(True, detail)
        return f"Ausgerüstet: {detail}"
    _note_equip(False, detail or "keine Rückmeldung")
    if "keine brauchbare Waffe" in detail:
        _EQUIP_FAILS = 0   # kein transienter Glitch, sondern echte Leere
        return "Keine brauchbare Waffe im Inventar (ruinierte zaehlen nicht)."
    _EQUIP_FAILS += 1
    _EQUIP_LAST_FAIL = now
    return _outcome(result, "Ausgeruestet:")


@mcp.tool()
def equip_melee() -> str:
    """Die beste NAHKAMPFWAFFE (Machete, Axt, Messer ...) in die Hand nehmen -
    auch wenn du eine geladene Schusswaffe hättest. Nahkampf ist leise,
    SOLANGE das Messer in der Hand bleibt: ohne Nahkampf-Lock wechselt das
    Kampfsystem (Expansion) bei mehreren nahen Gegnern oder wenig HP oft
    selbst zur Schusswaffe. Für den leisen Solo-Kampf gegen EINEN Infizierten
    darum direkt engage(mode="melee") nehmen - das zieht die Nahkampfwaffe
    selbst und hält sie, solange der Kampf nicht eskaliert. equip_melee und
    engage nie im selben Schritt parallel aufrufen. Bei Übermacht (mehrere
    gleichzeitig), gegen Raubtiere (Wolf/Bär) oder wenn dein Squad bei dir
    ist (macht ohnehin Lärm), nimm lieber equip_best (Schusswaffe)."""
    global _EQUIP_FAILS, _EQUIP_LAST_FAIL
    # Fahrzeug zuerst, VOR dem Circuit-Breaker (C8).
    vb = _vehicle_block()
    if vb:
        return vb
    now = time.time()
    # Gleicher Circuit-Breaker wie equip_best: nach 2 Fehlschlägen in Folge
    # (z.B. "Equip-Ziel verschwunden" direkt nach Spawn) nicht stur wiederholen.
    if _EQUIP_FAILS >= 2 and (now - _EQUIP_LAST_FAIL) < 90.0:
        return ("equip ist gerade 2x in Folge gescheitert - ruf es JETZT nicht "
                "noch einmal. Löse es anders: observe (liegt die Waffe wirklich "
                "vor dir?), die Waffe per pickup in die Hand nehmen, oder funk dem "
                "Spieler dein Problem. Erst wieder versuchen, wenn sich die Lage "
                "geändert hat.")
    result = tactics.equip_melee(BRIDGE, log=lambda m: None)
    ok = (result.startswith("Nahkampfwaffe gezogen")
          or result.startswith("Nahkampfwaffe ist schon"))
    _note_equip(ok, result)
    if "fehlgeschlagen" in result.lower():
        _EQUIP_FAILS += 1
        _EQUIP_LAST_FAIL = now
    else:
        _EQUIP_FAILS = 0
    if ok:
        result += (" Für den Kampf: engage(mode=\"melee\") - erst nach diesem "
                   "Ergebnis, nie im selben Schritt.")
    return result


@mcp.tool()
def clean_weapon() -> str:
    """Waffe in der Hand reinigen: repariert sie auf neuwertig und löst eine
    Ladehemmung (Klemmer). Verbraucht ein WeaponCleaningKit aus dem Inventar,
    falls vorhanden. Vorher equip_best, damit die Waffe in der Hand ist."""
    result = BRIDGE.run("clean_weapon", timeout=20)
    return _outcome(result, "Waffe gereinigt:")


@mcp.tool()
def unpack_ammo(classname: str = "") -> str:
    """Eine Munitionskiste (AmmoBox_*) im Inventar aufmachen und die Munition
    herausnehmen. Ohne classname die erste gefundene Box. Die Munition landet
    als Stapel im Inventar (oder am Boden, wenn voll), die Box wird vernichtet.
    Du musst die Box NICHT in die Hand nehmen - das läuft direkt."""
    result = BRIDGE.run("unpack_ammo", text=classname, timeout=15)
    return _outcome(result, "Entpackt:")


@mcp.tool()
def reload(weapon: str = "", classname: str = "") -> str:
    """Lose Munition (Ammo_*-Stapel) in passende Magazine, interne Magazine
    (Mosin, SKS) und Kammern (Izh18, Flinten, Einzellader) umladen. Ohne
    Angabe: erst die Waffe in der Hand, dann jede andere Feuerwaffe im
    Inventar, bis eine passende Munition findet. weapon="MP5K" lädt gezielt
    diese Waffe (auch wenn eine andere in der Hand ist). SO wird aus
    gelooteter Munition Feuerkraft: AmmoBox erst mit unpack_ammo öffnen,
    dann reload. Das Magazin-WECHSELN im Gefecht passiert automatisch. Das
    Ergebnis nennt Waffe und Schusszahl - nur DAS zählt als geladen, nie
    eine Annahme."""
    target = (weapon or classname or "").strip()
    result = BRIDGE.run("reload", text=target, timeout=20)
    return _outcome(result, "Nachgeladen:")


@mcp.tool()
def loot_area(max_items: int = 6) -> str:
    """Sichtbare lohnende Bodenitems in der Umgebung einsammeln (bis zu
    max_items, bewertet nach Nutzen: Waffen-Upgrades, Munition, Medizin,
    Nahrung, Werkzeug; Schrott bleibt liegen). Läuft selbstständig hin,
    sammelt ein und rüstet danach die beste Waffe aus. Kann mehrere Minuten
    dauern. Nur in sicherer Lage nutzen."""
    max_items = max(1, min(12, int(max_items)))
    vb = _vehicle_block()   # C8: im Fahrzeug weder laufen noch looten
    if vb:
        return vb
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
    if result.get("ignored"):
        parts.append("Eigene Ablagen liegen gelassen (bewusst weggeworfen): "
                     + ", ".join(result["ignored"]))
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
    # Solo-Melee-Präferenz respektieren: hält der NPC bewusst eine
    # Nahkampfwaffe, sie NICHT ungefragt gegen die Schusswaffe tauschen
    # (das setzte die equip_melee-Wahl bei jedem Looten still zurück).
    snap2 = BRIDGE.read_state() or {}
    hands = snap2.get("npc", {}).get("in_hands", "")
    if hands and tactics.classify_melee(hands) and not tactics.is_tool(hands):
        parts.append(f"Nahkampfwaffe bleibt in der Hand: {hands}")
    else:
        eq = BRIDGE.run("equip_best", timeout=30)
        parts.append(_outcome(eq, "Ausgeruestet:"))
    return "\n".join(parts)


_MELEE_MODES = ("melee", "nahkampf", "leise", "messer")


def _human_name() -> str:
    """DayZ-Profilname des menschlichen Spielers für Tool-Texte."""
    if PLAYER_PROFILE_NAME:
        return PLAYER_PROFILE_NAME
    if players_registry is not None:
        try:
            for entry in players_registry.all_players():
                dayz = (entry.get("dayz") or "").strip()
                if dayz:
                    return dayz
        except Exception:
            pass
    return "der Spieler"


def _split_engage_detail(detail: str) -> tuple[str, dict | None]:
    """Mod-Detail von engage (C3) zerlegen: "<Text>|SCHUSS=a,b|HAND=x|ESK=y".
    Ohne "|" (alter Mod-Build) -> (Text, None)."""
    if "|" not in (detail or ""):
        return (detail or "").strip(), None
    parts = detail.split("|")
    fields: dict = {}
    for p in parts[1:]:
        key, sep, val = p.partition("=")
        if sep:
            fields[key.strip().upper()] = val.strip()
    return parts[0].strip(), fields


def _engage_report(base: str, fields: dict | None) -> str:
    """Ehrlicher Kampfbericht (MVS-2/ENGAGE-1): Die NPCs funkten "leise mit
    dem Messer", obwohl Expansion zur Schusswaffe gegriffen hatte - das
    Ergebnis sagte nur "Ziel eliminiert". Beginnt immer mit "Kampf beendet:"
    und enthält "GESCHOSSEN mit:", "Von dir kein Schuss" oder "Ob du
    geschossen hast, ist unbekannt"."""
    if fields is None:
        head = f"Kampf beendet: {base}".rstrip().rstrip(".")
        return (head + ". Ob du geschossen hast, ist unbekannt - behaupte "
                       "nicht, es sei leise gewesen.")
    shots = [w.strip() for w in fields.get("SCHUSS", "").split(",") if w.strip()]
    hand = fields.get("HAND", "")
    esk = fields.get("ESK", "")
    if shots:
        out = (f"Kampf beendet: Ziel tot (wer es getötet hat, ist nicht "
               f"geprüft; {_human_name()} und Kameraden schießen mit). "
               f"GESCHOSSEN mit: {', '.join(shots)}. Das war LAUT - sag NICHT "
               f"'leise' oder 'mit dem Messer'.")
    else:
        out = (f"Kampf beendet: Ziel tot. Von dir kein Schuss (leise). "
               f"In der Hand: {hand or 'nichts'}.")
    if esk:
        out += (f" Der Nahkampf-Lock wurde aufgehoben ({esk}) - das "
                f"Kampfsystem durfte zur Schusswaffe greifen.")
    return out


@mcp.tool()
def engage(mode: str = "auto") -> str:
    """Den nächsten Gegner (Infizierter/Raubtier, max. 100 m) angreifen:
    hinlaufen, das Kampfsystem übernimmt auf kurze Distanz. Blockiert, bis
    das Ziel tot ist oder 3 Minuten vergangen sind. Nur einsetzen, wenn du
    kämpfen willst.
    mode="auto" (Standard): das Kampfsystem wählt die Waffe selbst und greift
    dabei oft zur Schusswaffe - LAUT, lockt weitere Infizierte an.
    mode="melee": zieht vorher selbst die beste Nahkampfwaffe (kein extra
    equip_melee nötig) und bindet sie, solange der Kampf nicht eskaliert
    (mehrere Gegner, wenig HP) - der leise Solo-Kampf gegen EINEN Infizierten.
    Das Ergebnis sagt, ob du geschossen hast ("GESCHOSSEN mit" oder "Von dir
    kein Schuss") - berichte nur das, nie deinen Plan."""
    # C8: im Fahrzeug weder Waffe ziehen noch kämpfen (Birgit 24.10.).
    vb = _vehicle_block()
    if vb:
        return vb
    melee = (mode or "").strip().lower() in _MELEE_MODES
    warn = ""
    aborted = None
    if melee:
        # Reise ZUERST stoppen: sonst verdrängt das equip ein laufendes Reise-
        # Segment, der Reise-Thread schickt ein neues move_to hinterher und
        # der NPC läuft mit halb gezogenem Messer weiter (Review W2).
        aborted = _abort_travel()
        # MVS-4: das Ziehen der Nahkampfwaffe gehört IN den Kampfaufruf -
        # parallel abgefeuertes equip_melee + engage lief ungeprüft weiter,
        # obwohl das Ziehen gescheitert war. lock=True: Nahkampf-Lock schon
        # beim Ziehen, nicht erst mit dem engage-Befehl 1-2 s später.
        res = tactics.equip_melee(BRIDGE, log=lambda m: None, lock=True)
        ok = (res.startswith("Nahkampfwaffe gezogen")
              or res.startswith("Nahkampfwaffe ist schon"))
        _note_equip(ok, res)
        if not ok:
            return ("ABGEBROCHEN: Nahkampfwaffe ließ sich nicht ziehen (" + res
                    + "). Kein Kampf gestartet. Entscheide: engage(mode=\"auto\")"
                    " = Schusswaffe, LAUT, oder flee." + _travel_abort_hint(aborted))
    else:
        # Schutz gegen Selbstmord: mit leeren Händen gegen Infizierte ist fast
        # sicherer Tod. Erst eine Waffe ziehen (equip_best) oder fliehen (flee).
        snap = BRIDGE.read_state() or {}
        in_hands = snap.get("npc", {}).get("in_hands", "")
        if not in_hands:
            return ("ABGEBROCHEN: nichts in der Hand. Mit blossen Faeusten gegen "
                    "Infizierte ist toedlich - erst equip_best (Waffe ziehen), "
                    "und wenn keine Waffe da ist, flee.")
        # Leere Feuerwaffe in der Hand = Nahkampf mit dem Gewehrkolben = fast
        # sicherer Tod. Die Inventarliste trägt das in_hands-Flag + quantity.
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
                    "engage(mode=\"melee\") - mit leerem Gewehr angreifen ist "
                    "fast sicherer Tod.")
        # MVS-4: equip im selben Schritt gescheitert? Ans ENDE hängen - das
        # Ergebnis beginnt weiter mit "Kampf beendet:" (C3, run_agent prüft).
        if (time.time() - _LAST_EQUIP["t"]) < 10.0 and not _LAST_EQUIP["ok"]:
            warn = (f" ACHTUNG: equip war eben gescheitert "
                    f"({_LAST_EQUIP['detail']}) - du hast mit {in_hands} gekämpft.")
    if not melee:
        aborted = _abort_travel()   # Kampf übernimmt die Beine - Fernreise abbrechen
    hint = _travel_abort_hint(aborted)
    if melee:
        result = BRIDGE.run("engage", text="melee", timeout=180)
    else:
        result = BRIDGE.run("engage", timeout=180)
    base, fields = _split_engage_detail(result.get("detail") or "")
    if result.get("status") != "done" and "kein Gegner" in base:
        return ("Kein Gegner (Infizierter/Raubtier) in Sicht. NICHT blind "
                "engage wiederholen - erst mit observe pruefen, was in der Naehe "
                "ist. Ist nichts da, geh weiter (move_to/explore_step) oder mach "
                "etwas anderes." + hint)
    if result.get("status") != "done":
        shown = dict(result)
        if fields is not None:
            shown["detail"] = base
        out = _outcome(shown, "Kampf beendet:")
        shots = [w.strip() for w in (fields or {}).get("SCHUSS", "").split(",")
                 if w.strip()]
        if shots:
            out += f" Dabei GESCHOSSEN mit: {', '.join(shots)} - das war LAUT."
        return out + warn + hint
    out = _engage_report(base, fields)
    # Folgekette Nachladen: nach dem Gefecht lose Munition in Magazine/Waffe
    # umladen, solange es ruhig ist - im NÄCHSTEN Kampf zählt jede Patrone.
    # MVS-7/MOD-4: erst 3 s warten - Expansion lädt die Handwaffe direkt nach
    # dem Kill oft noch selbst nach, und unser Reload brach diesen Lade-
    # zustand ab (6 von 12 Reloads am 24.10. mit NULL-Pointer im Crashlog).
    time.sleep(3.0)
    rl = BRIDGE.run("reload", timeout=20)
    rdetail = rl.get("detail") or ""
    if rl.get("status") == "done":
        out += (" Nach dem Kampf nachgeladen (leere Kammern/Magazinplätze "
                "aufgefüllt): " + rdetail)
    elif "gerade selbst" in rdetail:
        out += " Nachladen übersprungen: " + rdetail
    return out + warn + hint


@mcp.tool()
def flee() -> str:
    """150 m vom nächsten Infizierten wegsprinten (oder in Blickrichtung,
    wenn keiner in der Nähe ist)."""
    _abort_travel()   # Flucht übernimmt die Beine - Fernreise abbrechen
    result = BRIDGE.run("flee", timeout=90)
    return _outcome(result, "In Sicherheit.")


def _discord_active() -> bool:
    """True, wenn der Discord-Voice-Bot für diesen Agenten verbunden ist.
    Der Bot legt discord_active.flag neben die voice_outbox, sobald er im
    Sprachkanal ist. Dann sprechen die NPCs nur per Voice - der In-Game-Chat
    würde sonst dieselbe Zeile doppelt zeigen."""
    flag = os.path.join(os.path.dirname(VOICE_OUTBOX), "discord_active.flag")
    return os.path.exists(flag)


def _emit_bubble(text: str) -> None:
    """Comic-Sprechblase ans HUD funken (RPC-only, kein Chat). Im Discord-Pfad
    läuft CmdSay nicht, also schickt nur dieser Aufruf die Blase; ohne Discord
    macht CmdSay das selbst mit. Das HUD zeigt sie nur, wenn Comic-Chat an ist."""
    try:
        BRIDGE.run("bubble", text=text, timeout=10)
    except Exception:
        pass


# Schutz gegen Doppel-Äußerungen: das Gehirn ruft dieselbe Zeile manchmal
# zweimal kurz hintereinander (Begrüßung doppelt). Identischer Text innerhalb
# von 15 s wird einmal unterdrückt - gilt für say und say_voice gemeinsam.
_LAST_SAID = {"text": "", "t": 0.0}


def _is_repeat(text: str) -> bool:
    now = time.time()
    t = (text or "").strip()
    if t and t == _LAST_SAID["text"] and (now - _LAST_SAID["t"]) < 15.0:
        return True
    _LAST_SAID["text"] = t
    _LAST_SAID["t"] = now
    return False


# Funkregel: NUR Äußerungen, die den SPIELER direkt ansprechen (seinen Namen
# nennen), werden vertont (ElevenLabs-TTS im Discord). Funk untereinander (NPC
# an NPC) bleibt Text im In-Game-Chat - das spart Stimm-Kontingent und Spam.
PLAYER_NAMES = [n.strip().lower() for n in os.environ.get(
    "ISU_PLAYER_NAMES",
    os.environ.get("ISU_MIC_NAME", "Player")).split(",") if n.strip()]


def _player_name_variants() -> list[str]:
    """Alle Namen, unter denen der Spieler angesprochen werden kann: Env
    (ISU_PLAYER_NAMES / ISU_MIC_NAME) PLUS arena/players.json (funk, dayz,
    aliases). Vorher zählte nur die Env-Liste - Konrad sagte "Isu, ..."
    (so will der Spieler laut seinem personen.md genannt werden) und wurde
    nie vertont, während Viktors "Isualc, ..." durchging (09.09.2026)."""
    names = set(PLAYER_NAMES)
    if players_registry is not None:
        try:
            for entry in players_registry.all_players():
                for c in [entry.get("funk"), entry.get("dayz")] + list(entry.get("aliases") or []):
                    if isinstance(c, str) and c.strip():
                        names.add(c.strip().lower())
        except Exception:
            pass
    return sorted(names, key=len, reverse=True)


def _is_for_player(text: str) -> bool:
    """True, wenn die Äußerung den Spieler direkt anspricht (Name als
    eigenes Wort im Text). Nur dann wird vertont; sonst stiller Text-Funk
    für die Gruppe."""
    t = (text or "").lower()
    if not t:
        return False
    import re as _re
    for name in _player_name_variants():
        if _re.search(r"(?<![a-z0-9äöüß])" + _re.escape(name) + r"(?![a-z0-9äöüß])", t):
            return True
    return False


# Entwickler-Schalter "NPC radio TTS" (Arena-Menü, 29.08.): ON = ALLE
# Äußerungen werden über ElevenLabs vertont, auch reiner NPC-zu-NPC-Funk.
# OFF (Default) = bisheriges Verhalten, nur Spieler-gerichtetes (_is_for_player).
# Kommt als ISU_NPC_TTS über die Prozesskette Supervisor -> run_agent ->
# Claude Code -> dayz_mcp und wirkt damit NUR auf Ketten, die NACH dem Setzen
# gestartet wurden (Arena: der Supervisor setzt os.environ bei jedem
# Rundenstart; Solo: Env in der Konsole setzen, DANN run_agent starten).
# setx erreicht laufende Prozesse nicht - deren Umgebung ist eingefroren.
def _npc_tts() -> bool:
    return os.environ.get("ISU_NPC_TTS", "0") == "1"


@mcp.tool()
def say(text: str = "", message: str = "", content: str = "") -> str:
    """Etwas laut sagen. Erscheint immer im In-Game-Chat (Spieler in 60 m).
    VERTONT (Discord-Stimme) wird es NUR, wenn du den SPIELER direkt ansprichst,
    also seinen Namen nennst (z.B. "Clausi, pass auf!"). Funk untereinander
    (an Viktor/Igor/Birgit/Konrad) bleibt absichtlich Text - das spart Stimme
    und Aufmerksamkeit. Willst du, dass der Spieler dich HÖRT: nenne seinen
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
    if _discord_active() and (_npc_tts() or _is_for_player(text)):
        _outbox({"type": "tts", "text": text, "agent": AGENT_NAME,
                 "voice": AGENT_VOICE})
    return _outcome(result, "Gesagt.")


@mcp.tool()
def voice_lines() -> str:
    """Katalog deiner hörbaren Sprachzeilen (für say_voice). Nutze die id."""
    catalog = _voice_catalog()
    if not catalog:
        return "Keine Sprachzeilen verfügbar (soundsets.json fehlt)."
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
    """Freitext live über ElevenLabs sprechen (wie say). Gemeinsamer Pfad
    für say und den say_voice-Fallback."""
    if _is_repeat(txt):
        return note + " (schon gesagt, nicht wiederholt)"
    screen = transliterate.to_screen(txt, AGENT_LANG)
    if screen != txt:
        _radio_native(screen, txt)
    result = BRIDGE.run("say", text=screen, timeout=15)
    if _discord_active() and (_npc_tts() or _is_for_player(txt)):
        _outbox({"type": "tts", "text": txt, "agent": AGENT_NAME,
                 "voice": AGENT_VOICE})
    return _outcome(result, note)


@mcp.tool()
def say_voice(phrase_id: str = "", text: str = "", line: str = "") -> str:
    """Eine Sprachzeile HÖRBAR sagen. Mit gültiger phrase_id aus dem Katalog
    (nur Viktor) als 3D-Sound, sonst als Live-TTS. Beispiel:
    say_voice(phrase_id="greet_01"). Für Freitext geht auch einfach say.
    Tolerant: Freitext in phrase_id/text/line wird live gesprochen statt zu
    scheitern."""
    catalog = _voice_catalog()
    info = catalog.get(phrase_id)
    free = (text or line or "").strip()
    # phrase_id war Freitext (Agent verwechselt say/say_voice) -> als Text werten
    if not info and not free and phrase_id and phrase_id not in catalog:
        free = phrase_id.strip()

    # Kein Katalog-Treffer: Freitext live sprechen (funktioniert für alle).
    if not info:
        if free:
            return _speak_tts(free, "Gesagt (Freitext live).")
        return ("Nichts zu sagen. Für Freitext nutze say(text=...), für "
                "Katalog-Zeilen say_voice(phrase_id=...) - Liste mit voice_lines().")

    # Katalog-Oggs existieren bisher nur für Viktor - andere sprechen den
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
    einen Befehl auf einen KAMERADEN (z.B. 'Viktor') nicht fälschlich auf den
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


def _roster_canonical(requested: str) -> str:
    """Roster-Schreibweise eines KI-Kameraden ('igor' -> 'Igor'). Die Mod
    vergleicht in follow/regroup per IsuAgentRegistry.FindByName EXAKT;
    regroup(player_name="igor") lief deshalb still zum Spieler (Review W4)."""
    want = (requested or "").strip().lower()
    for fn in ("active_roster.json", "agents.json"):
        try:
            with open(os.path.join(REPO_DIR, "arena", fn), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for a in data.get("agents", []):
            n = (a.get("name") or "").strip()
            if n and n.lower() == want:
                return n
    return (requested or "").strip()


def _resolve_player_name(requested: str, allow_empty: bool = False) -> str:
    """Funk-/Voice-Namen des Spielers auf seinen DayZ-Profilnamen normalisieren.

    Der menschliche Spieler heißt im Funk oft anders (PLAYER_RADIO_NAME,
    'Isualc') als im Spiel ('Clausi', GetIdentity). Die Mod vergleicht beim
    follow EXAKT (pb.GetIdentity().GetName() != cmd.text), darum scheitert
    follow('Isualc') mit "weder Spieler noch Kamerad in 180 m", während
    follow('Clausi') klappt. Auflösungsreihenfolge:
      1. Leer / KI-Kamerad (Roster): unverändert lassen.
      2. Exakter Treffer auf einen sichtbaren Spieler: dessen echte Schreibweise.
      3. Genau EIN Mensch in Sicht: der ist eindeutig gemeint.
      4. Funk- oder konfigurierter Profilname des Spielers gemeint: den
         konfigurierten Profilnamen nehmen, sonst (allow_empty) leer -> die Mod
         folgt dem nächsten Spieler (NPCs haben keine Identity, der Mensch ist
         der einzige echte Spieler).
      5. Sonst unverändert (Mod-Fallback entscheidet)."""
    requested = (requested or "").strip()
    if not requested:
        return requested
    if requested.lower() in _roster_names():
        # KI-Kamerad - nie auf den Menschen umbiegen, aber in Roster-Schreibweise
        return _roster_canonical(requested)
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
                return dayz                 # aufgelöster Profilname
            if allow_empty:
                return ""                   # bekannt, aber kein Profilname -> Mod folgt nächstem
    # 4b. Fallback (Registry fehlt/kennt den Namen nicht): meint der Befehl
    # nachweislich den Menschen (sein Funk- oder konfigurierter Profilname)?
    aliases = {PLAYER_RADIO_NAME.lower()}
    if PLAYER_PROFILE_NAME:
        aliases.add(PLAYER_PROFILE_NAME.lower())
    if requested.lower() in aliases:
        if PLAYER_PROFILE_NAME:
            return PLAYER_PROFILE_NAME      # explizit konfigurierter Profilname
        if allow_empty:
            return ""                       # Mod folgt dem nächsten Spieler
    return requested


@mcp.tool()
def follow(player_name: str = "") -> str:
    """Einem Spieler folgen (Gruppenbeitritt, du läufst automatisch hinterher).
    Ohne player_name: der nächste Spieler in 100 m. Endet automatisch, sobald
    du selbst irgendwohin gehst (move_to/flee/engage) oder unfollow nutzt."""
    aborted = _abort_travel()   # Folgen bricht eine laufende Fernreise ab
    hint = _travel_abort_hint(aborted, ended=True)
    target = _resolve_player_name(player_name, allow_empty=True)
    result = BRIDGE.run("follow", text=target, timeout=15)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "weder Spieler noch Kamerad" in detail:
        if not (player_name or "").strip():
            return ("Fehlgeschlagen: kein Spieler/Kamerad in Reichweite und du "
                    "hast KEINEN Namen genannt. Gib einen Namen an, z.B. "
                    "follow(player_name=\"Clausi\"), oder geh mit move_to naeher "
                    "ran. NICHT mit leerem follow() wiederholen." + hint)
        return (f"Fehlgeschlagen: '{player_name}' ist nicht in 180 m. Pruefe den "
                f"Namen (Schreibweise wie in observe) oder geh mit move_to naeher "
                f"ran, statt follow zu wiederholen." + hint)
    return _outcome(result, "Folge aufgenommen:") + hint


@mcp.tool()
def unfollow() -> str:
    """Aufhören, einem Spieler zu folgen, und stehenbleiben."""
    result = BRIDGE.run("unfollow", timeout=15)
    return _outcome(result, "Folge beendet.")


@mcp.tool()
def stop() -> str:
    """Sofort stehenbleiben und aktuelle Bewegung abbrechen (beendet auch follow)."""
    _abort_travel()   # bricht auch eine laufende Fernreise ab
    result = BRIDGE.run("stop", timeout=15)
    return _outcome(result, "Stehe still.")


# --------------------------------------------------------------- Wissensbasis
# Kuratiertes Survival-Wissen für research(): verhindert, dass das Gehirn
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
        "Material da ist). Folgekette: fish (sucht Wasser, läuft hin, "
        "angelt; ~60% Chance, mit Köder Worm/Bait ~80%) -> process_food/"
        "cook_meal (garen!) -> eat. Am Weiher gibt es Carp, an der Küste "
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
        "durch reload zu Feuerkraft (füllt Magazine und interne Magazine "
        "wie Mosin/Flinte). Folgekette nach dem Looten und nach jedem "
        "Kampf: unpack_ammo (falls Boxen) -> reload. Zeigt observe die "
        "Waffe UNGELADEN und du hast Munition: reload. Magazinwechsel im "
        "Gefecht läuft automatisch, solange ein gefülltes Magazin im "
        "Inventar liegt. Waffe klemmt/beschädigt: clean_weapon."),
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
    """Survival-Wissen nachschlagen, BEVOR du rätst oder etwas Falsches
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
    lines.append("Hinweis: cook_meal braucht zusätzlich 3x WoodenStick Brennholz "
                 "und ein Zündmittel (Matchbox/PetrolLighter).")
    lines.append("Neues Rezept von Spielern lernen: learn_recipe.")
    return "\n".join(lines)


@mcp.tool()
def learn_recipe(name: str, materials: str, result: str,
                 place: bool = False, desc: str = "") -> str:
    """Ein neues Rezept DAUERHAFT lernen (z.B. wenn ein Spieler es dir
    erklärt). Beispiel: learn_recipe(name="holzkiste",
    materials="2x WoodenPlank + 4x Nail", result="WoodenCrate").
    place=True, wenn das Ergebnis vor dir am Boden platziert werden soll
    (Bauwerke), sonst landet es im Inventar. Materials sind Classnames!"""
    return tactics.learn_recipe(name, materials, result, place, desc)


@mcp.tool()
def loot_corpse() -> str:
    """Die nächste Leiche (kind=corpse in observe, max. 50 m) looten:
    hinlaufen und die Sachen übernehmen. Tote Infizierte tragen oft
    Brauchbares - nach einem Kampf immer eine Überlegung wert."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    result = BRIDGE.run("loot_corpse", timeout=60, interruptible=True)
    return _outcome(result, "Geloote:")


# Skip-Cache: kürzlich erfolglos durchsuchte Container merken (Key = classname
# + grob gerundete NPC-Position; der NPC steht beim Looten am Container). So
# fährt das Gehirn denselben leeren Behälter nicht 18x an.
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
    """Nächsten Behälter MIT INHALT ausräumen (max. 50 m): Leichen,
    liegende Rucksäcke, Kleidung mit Sachen drin, Kisten. ZELTE werden NICHT
    geplündert - das Lager-Zelt ist euer gemeinsamer Stauraum, da legst du mit
    store_container hinein (sonst räumst du den anderen ihr Depot leer). In
    observe erkennst du Loot am Marker [enthält N]. Beispiel:
    loot_container(classname="TaloonBag") oder ohne Filter den nächsten."""
    key = _loot_key(classname)
    now = time.time()
    for k, t in list(_LOOT_EMPTY.items()):   # abgelaufene Einträge räumen
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
    Kiste, Fass, Rucksack am Boden; max. 50 m) - das Gegenstück zu
    loot_container. SO verstaust du Überschuss richtig, statt ihn mit drop auf
    den BODEN zu werfen. Getragene Kleidung und die Waffe in der Hand bleiben
    dran (nur lose Sachen aus deinem Cargo werden verstaut). Optionaler Filter:
    store_container(classname="Ammo") verstaut nur Munition; ohne Filter alles
    Lose. Geh vorher mit move_to zum Zelt/Container. Findet sich KEIN Container
    in der Nähe und dein Inventar ist voll, darfst du als Notlösung mit drop
    Platz schaffen."""
    # Bewegungs-Tool (läuft bis 50 m zum Container): laufende Fernreise
    # abbrechen - war als EINZIGES der 16 Bewegungs-Tools ohne diesen Guard.
    _abort_travel()
    result = BRIDGE.run("store_container", text=classname, timeout=60, interruptible=True)
    out = _outcome(result, "Verstaut:")
    if "kein Container" in out:
        out += (" Fallback: kein Container in der Nähe - wenn dein Inventar voll"
                " ist, mit drop Platz schaffen, sonst später am Lager verstauen.")
    return out


@mcp.tool()
def craft(recipe: str) -> str:
    """Etwas herstellen. Beispiel: craft(recipe="fireplace") - Rezeptnamen
    zeigt recipes(). Prüft Material, verbraucht es und erzeugt das Ergebnis
    (Lagerfeuer wird vor dir platziert, alles andere landet im Inventar)."""
    return tactics.craft(BRIDGE, recipe, log=lambda m: None)


@mcp.tool()
def cook_meal() -> str:
    """Rohes Essen im Inventar garen - erledigt die ganze Kette selbstständig:
    brennendes Feuer suchen, notfalls Lagerfeuer bauen (5x WoodenStick + 1x Rag)
    und anzünden (Zündmittel nötig), dann alles Rohe garen. Rohes Fleisch zu
    essen macht krank - immer erst garen!"""
    _abort_travel()   # läuft zum Feuer - laufende Fernreise abbrechen
    return tactics.cook_meal(BRIDGE, log=lambda m: None)


@mcp.tool()
def drink_at_well() -> str:
    """Zum nächsten sichtbaren Brunnen laufen (kind=water in observe), dort
    trinken und einen Flüssigkeitsbehälter auffüllen. Brunnen stehen in
    Dörfern - wenn keiner sichtbar ist, erst in eine Ortschaft gehen."""
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    return tactics.water_run(BRIDGE, log=lambda m: None)


@mcp.tool()
def find_item(pattern: str = "", item_name: str = "", item_type: str = "",
              item: str = "", name: str = "") -> str:
    """Prüfen, ob du etwas Bestimmtes hast oder in Sichtweite liegt.
    Beispiel: find_item(pattern="Canister") - Teilstring im Classname.
    Für die Suche in der Welt: explore_step wiederholen."""
    # Bewusst nur Alias-Faltung, KEINE Classname-Korrektur: find_item ist eine
    # Teilstring-Suche, ein "korrigierter" Volltreffer würde die Suche verengen.
    needle = _first(pattern, item_name, item_type, item, name)
    if not needle:
        return "Bitte pattern angeben, z.B. find_item(pattern=\"Nail\")."
    return tactics.find_item(BRIDGE, needle)


@mcp.tool()
def explore_step() -> str:
    """Ein Such-/Erkundungsschritt: läuft ~100 m in eine neue Richtung und
    sammelt dort Lohnendes ein. Für gezielte Item-Suche mehrfach wiederholen
    und zwischendurch mit find_item prüfen."""
    vb = _vehicle_block()   # C8: im Fahrzeug weder laufen noch looten
    if vb:
        return vb
    _abort_travel()   # Bewegungs-Tool: laufende Fernreise abbrechen (Mailbox-Konflikt)
    return tactics.explore_step(BRIDGE, log=lambda m: None)


@mcp.tool()
def build_fence_frame() -> str:
    """EXPERIMENTELL: Einen Zaun-Rahmen vor dir bauen (braucht 2x WoodenLog
    im Inventar). Erster Baustein für eine Basis."""
    result = BRIDGE.run("build_fence_frame", timeout=30)
    return _outcome(result, "Gebaut:")


def _drop_gone(before: dict, after: dict, needle: str) -> str:
    """Classname des abgelegten Items aus dem Inventar-Delta (needle exakt,
    sonst als Teilstring; ohne needle das Item in der Hand), sonst ""."""
    if not before or not after:
        return ""
    bc = tactics._inventory_counts(before)
    ac = tactics._inventory_counts(after)
    target = (needle or (before.get("npc") or {}).get("in_hands", "") or "").strip()
    if not target:
        return ""
    cands = [cn for cn in bc if cn == target]
    if not cands:
        cands = [cn for cn in bc if target.lower() in cn.lower()]
    for cn in cands:
        if ac.get(cn, 0) < bc[cn]:
            return cn
    return ""


@mcp.tool()
def drop(classname: str = "", item_name: str = "", item: str = "",
         name: str = "") -> str:
    """Einen Gegenstand aus deinem Inventar auf den Boden legen (mit Animation).
    Beispiel: drop(classname="Mosin9130"); ohne Argument das Item in der Hand.
    Um einem MITSPIELER etwas zu geben, nutze besser give_to (direkt, ohne
    Bodenphase). drop ist für Ablegen oder Platzschaffen."""
    needle = _needle(classname, item_name, item, name)
    before = _safe_state()
    result = BRIDGE.run("drop", text=needle, timeout=30)
    if (result.get("status") == "failed" and before
            and "haengt im Inventar fest" in (result.get("detail") or "")):
        # MOD-7 (24.10.): Die Mod prüft im selben Frame wie der (animierte,
        # bei Hand-Items geklonte) Drop und meldet "haengt im Inventar fest",
        # obwohl das Item einen Frame später am Boden liegt. Ohne Ablage-
        # Eintrag hob loot_area es wieder auf (Izh18, Derringer). Darum kurz
        # warten, neu zählen und bei gesunkener Anzahl als abgelegt melden.
        try:
            time.sleep(1.5)
            after = _safe_state()
            gone = _drop_gone(before, after, needle)
            if gone:
                npc = (before.get("npc") or {})
                tactics.remember_drop(gone, npc.get("pos_x", 0.0), npc.get("pos_z", 0.0))
                return (f"Abgelegt: {gone} (verzögert - die Mod meldete erst "
                        f"'hängt im Inventar fest', das Item ist aber aus dem "
                        f"Inventar verschwunden).")
        except (OSError, TypeError, ValueError, AttributeError):
            pass
    if result.get("status") == "done" and before:
        # Ablage merken (Classname aus dem Inventar-Delta, sonst aus dem
        # Mod-Detail "<Classname> abgelegt"), damit Auto-Loot sie liegen lässt.
        try:
            time.sleep(1.2)   # 1-Hz-State: Inventar-Delta erst nach dem Tick sichtbar
            after = _safe_state()
            gone = [cn for cn, n in tactics._inventory_counts(before).items()
                    if tactics._inventory_counts(after).get(cn, 0) < n]
            dropped = gone[0] if len(gone) == 1 else ""
            if not dropped:
                dropped = (result.get("detail") or "").strip().split(" ")[0]
            if dropped == "Item":   # "Item aus der Hand abgelegt" (ohne Classname)
                dropped = (before.get("npc") or {}).get("in_hands", "")
            npc = (before.get("npc") or {})
            tactics.remember_drop(dropped, npc.get("pos_x", 0.0), npc.get("pos_z", 0.0))
        except (OSError, TypeError, ValueError, AttributeError):
            pass
    return _outcome(result, "Abgelegt:", needle=needle)


@mcp.tool()
def give_to(player_name: str = "", classname: str = "", item_name: str = "",
            item: str = "", name: str = "", player: str = "",
            target: str = "") -> str:
    """Einem anderen Survivor (KI-Kollegen) einen Gegenstand DIREKT ins
    Inventar geben - zuverlässiger als drop+pickup, das Item geht nicht am
    Boden verloren. Beide müssen nah beieinander sein (max 12 m).
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
    nächste Spieler. Auch Kameraden per Name (z.B. regroup(player_name="Igor")).
    Nutze das statt move_to mit geratenen Zahlen, wenn du die Gruppe verloren
    hast. Wartet höchstens 50 s und bricht bei Funk oder Gefahr sofort ab.
    Kommt "Laeuft noch", läufst du serverseitig weiter; ein erneuter
    regroup-Aufruf setzt das Ziel auf die AKTUELLE Position (der Gesuchte
    bewegt sich ja weiter)."""
    aborted = _abort_travel()   # zur Gruppe zurück heißt: laufende Fernreise abbrechen
    hint = _travel_abort_hint(aborted, ended=True)
    target = _resolve_player_name(player_name, allow_empty=True)
    # TOOL-1/RESP-8/VI-11 (24.10.): vorher timeout=240 OHNE interruptible -
    # regroup war bis zu 4 Minuten taub für Funk und Gefahr (Viktor verlor
    # dabei 26 HP, Birgit überhörte 207 s lang Konrads Positionsfunk).
    result = BRIDGE.run("regroup", text=target, timeout=50, interruptible=True)
    detail = (result.get("detail") or "").strip()
    if result.get("status") == "done" and detail.startswith("auf dem Weg zu "):
        # Älterer Mod-Build: das Start-Detail blieb nach der Ankunft stehen
        # ("Wieder bei der Gruppe. auf dem Weg zu Player").
        result = dict(result, detail="bei " + detail[len("auf dem Weg zu "):])
    return _outcome(result, "Wieder bei der Gruppe.") + hint


@mcp.tool()
def door(action: str = "open") -> str:
    """Die nächste Tür (max. 5 m) öffnen oder schließen.
    Beispiel: door(action="open") oder door(action="close"). Beim normalen
    Laufen öffnest du Türen automatisch - das hier ist für gezieltes
    Schließen (Sicherheit!) oder Öffnen ohne hindurchzugehen."""
    if action not in ("open", "close"):
        return "action muss 'open' oder 'close' sein."
    result = BRIDGE.run("door", text=action, timeout=20)
    return _outcome(result, "Tuer:")


@mcp.tool()
def vehicle_exit() -> str:
    """Bewusst aus dem Fahrzeug aussteigen. Nur nutzen, wenn das Fahrzeug
    steht und es einen guten Grund gibt. Während der Fahrt: sitzen bleiben."""
    result = BRIDGE.run("vehicle_exit", timeout=20)
    return _outcome(result, "Steige aus:")


@mcp.tool()
def unstick() -> str:
    """Selbstbefreiung, wenn du festhängst: zwingt dich aufzustehen, bricht
    hängende Aktionen ab und löst Geometrie-Verkeilungen. Nutzen, wenn
    move_to wiederholt ohne Fortschritt fehlschlägt oder du am Boden liegst."""
    global _UNSTICK_CALLS, _UNSTICK_LAST
    now = time.time()
    # Circuit-Breaker (Muster _EQUIP_FAILS, hier AUFRUF-gezählt): unstick hilft
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
    der Karte). Anders als move_to läuft die Reise im Hintergrund weiter und
    weicht Hindernissen (See, Zaun, Sumpf) AUTOMATISCH aus - keine eigenen
    move_to/unstick-Ketten nötig. Dieser Aufruf kehrt SOFORT zurück, damit
    dein Zug endet - du sparst Tokens und meldest dich erst wieder, wenn du
    angekommen bist oder festhängst. Deine Gedankenzeile zeigt die Restdistanz.
    Ein neues travel_to ersetzt das alte; stop, follow oder regroup brechen die
    Reise ab."""
    global _travel_thread, _travel_stop, _travel_goal
    gx, gz = float(x), float(z)
    _abort_travel()   # altes Ziel verwerfen
    snap = BRIDGE.read_state() or {}
    npc = snap.get("npc", {})
    px = pz = None
    d = 0.0
    if npc.get("alive") and npc.get("spawned"):
        px = float(npc.get("pos_x", 0.0))
        pz = float(npc.get("pos_z", 0.0))
        d = math.hypot(gx - px, gz - pz)
        if d <= _TRAVEL_ARRIVE:
            return (f"Du bist schon so gut wie dort (Distanz {d:.0f} m) - "
                    f"keine Reise noetig.")
    stop = threading.Event()
    t = threading.Thread(target=_travel_worker, args=(gx, gz, stop), daemon=True)
    with _travel_lock:
        _travel_thread = t
        _travel_stop = stop
        _travel_goal = (gx, gz)
    t.start()
    if px is None:
        return (f"Reise gestartet Richtung x={gx:.0f} z={gz:.0f} - ich weiche "
                f"Hindernissen unterwegs selbst aus und melde mich bei Ankunft oder "
                f"wenn ich wirklich nicht weiterkomme. Beende jetzt deinen Zug.")
    # MISS-1: Distanz, Richtung und eigene Position als Plausibilitätsprüfung -
    # Birgit hielt ein 718 m entferntes Ziel hinter sich für "gut hundert
    # Meter" entfernt und lief 700 m zurück.
    return (f"Reise gestartet: {d:.0f} m Richtung {bridge_mod._compass(gx - px, gz - pz)} "
            f"nach x={gx:.0f} z={gz:.0f} (du stehst bei x={px:.0f} z={pz:.0f}) - "
            f"ich weiche Hindernissen unterwegs selbst aus und melde mich bei "
            f"Ankunft oder wenn ich wirklich nicht weiterkomme. Beende jetzt "
            f"deinen Zug.")


def _sleep_interruptible(seconds: float) -> str:
    """Sekunden warten, aber sofort abbrechen, wenn neuer Funk in der Inbox
    liegt (wie andere Langaktionen) oder ein kritisches Ereignis kommt.

    Rückgabe ist der GRUND (VI-12/RESP-5, 24.10.: vorher nur True, und wait
    meldete auch bei GEFAHR "Der Spieler funkt dich an"):
      ""                 voll gewartet
      "funk"/"funk: <Text>"  neuer Funk in der Inbox (Text, wenn lesbar)
      "kritisch: <Ereignis>" urgent_event.json neu geschrieben
      "tot"              Tod-Sperre (C1)
    Aufrufer, die nur auf den Wahrheitswert prüfen, bleiben korrekt."""
    inbox = getattr(BRIDGE, "voice_inbox", None)
    base = -1
    if inbox and os.path.exists(inbox):
        try:
            base = os.path.getsize(inbox)
        except OSError:
            base = -1
    # Kritische Welt-Ereignisse (Blutung/Gefahr) brechen das Warten ebenfalls ab.
    flag = getattr(BRIDGE, "urgent_flag", None)
    flag_base = bridge_mod._flag_mtime(flag) if flag else 0.0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _brain_dead():
            return "tot"
        if base >= 0:
            said = bridge_mod._inbox_interrupt_line(inbox, base)
            if said:
                return "funk" if said == "?" else "funk: " + said
        if flag:
            ev = bridge_mod._urgent_event(flag, flag_base)
            if ev:
                return "kritisch: " + ev
        time.sleep(1.0)
    return ""


def _sleep_reason_text(reason: str) -> str:
    """Grund aus _sleep_interruptible in den Klartext von interrupt_reason
    übersetzen (gleiche Formulierung wie bei abgebrochenen Langaktionen)."""
    if reason.startswith("kritisch:"):
        return bridge_mod.interrupt_reason({"detail": reason})
    said = reason[len("funk"):].lstrip(":").strip()
    detail = "neuer Funk: " + said if said else "neuer Funk"
    return bridge_mod.interrupt_reason({"detail": detail})


@mcp.tool()
def fish() -> str:
    """Angeln, um an Nahrung zu kommen. Braucht eine FishingRod im Inventar (ein
    Wurm/Köder ist optional, erhöht aber die Fangchance). Sucht selbst das
    nächste Gewässer, läuft hin, wirft aus und wartet. Chance ~60% (mit Köder
    ~80%). Bei Erfolg landet ein Fisch in deinem Inventar (am Weiher ein Carp, an
    der See ein Mackerel) - roh, also vor dem Essen am Feuer garen (cook_meal)."""
    state = BRIDGE.read_state() or {}
    rod_note = ""
    if not _have_item(state, "FishingRod"):
        # Folgekette: Rute selbst nachbauen, wenn das Material da ist
        crafted = tactics.craft(BRIDGE, "fishing_rod", log=lambda m: None)
        if not crafted.startswith("Hergestellt"):
            return ("Du hast keine Angelrute (FishingRod) und das Material für "
                    "eine neue reicht nicht (1x LongWoodenStick + 1x Rope): "
                    + crafted + " Erst Material/Rute besorgen, dann wieder fish.")
        rod_note = "Angelrute unterwegs gebaut. "
        state = BRIDGE.read_state() or state
    has_bait = (_have_item(state, "Worm") or _have_item(state, "Bait")
                or _have_item(state, "Dough"))

    _abort_travel()   # Angeln übernimmt die Beine - laufende Fernreise abbrechen
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

    _write_intent_line(_hud_text("Fishing by the water", "Angelt am Wasser"))
    interrupted = _sleep_interruptible(random.uniform(60, 120))
    if interrupted == "tot":
        return bridge_mod.DEATH_TEXT
    if interrupted:
        # VI-12: kritische Ereignisse nicht mehr als Spieler-Funk ausgeben.
        return "ABGEBROCHEN beim Angeln: " + _sleep_reason_text(interrupted)

    chance = 0.6 + (0.2 if has_bait else 0.0)
    if has_bait:
        # Den tatsächlich vorhandenen Köder verbrauchen (früher stur "Worm",
        # auch wenn der Köder Bait/Dough war -> nichts wurde verbraucht).
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
    klar, wenn dir nichts fehlt oder das nötige Medikament fehlt."""
    state = BRIDGE.read_state() or {}
    if not survival._modern_bridge(state):
        return "Gezielte Medikamenteneinnahme braucht Servermod 0.9.0 oder neuer."
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
            result = BRIDGE.run("take_medicine", text=med, timeout=30)
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
    BloodBagIV (Flüssigkeit/Blut). Das Item wird dabei verbraucht. Beispiel:
    treat_other(player_name="Igor", classname="BandageDressing"). So spielt der
    Sani seine Rolle, ohne dem anderen das Zeug nur in die Hand zu drücken."""
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
    Ersatzkleidung reißen."""
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
    # Selbstbehandlung über den treat_other-Pfad der Mod: die findet den
    # eigenen Survivor per Name (Distanz 0), prüft IsBleeding und schließt
    # die stärkste Blutungsquelle wie Vanilla-ApplyBandage.
    result = BRIDGE.run("treat_other", target=AGENT_NAME, item=needle, timeout=20)
    detail = result.get("detail") or ""
    if result.get("status") == "failed" and "blutet nicht" in detail:
        return "Du blutest (nicht mehr) - kein Verband noetig."
    return _outcome(result, f"Verbunden mit {needle}:")


# Aufenthalts-Anker für wait (RESP-5, 24.10.): Birgit stand 19 Minuten im
# Umkreis von 150 m und pendelte mit explore_step-Bögen hin und zurück - der
# 15-m-Anker des STILLSTAND-Wächters setzte dabei jedes Mal zurück. Hier zählt
# der AUFENTHALT im 150-m-Kreis; ab 240 s ohne Menschen in der Nähe bekommt
# wait einen Hinweis mit der Kameraden-Lage (keine Verweigerung: Wache auf
# ausdrückliche Bitte bleibt erlaubt).
_WAIT_ANCHOR: tuple[float, float] | None = None
_WAIT_SINCE = 0.0
_WAIT_LAST = 0.0
_WAIT_RADIUS = 150.0       # m - Explore-Bögen mit Rückkehr bleiben "am Ort"
_WAIT_HINT_SEC = 240.0     # s Aufenthalt bis zum Hinweis
_WAIT_GAP_RESET = 600.0    # s ohne wait-Aufruf -> Anker verwerfen (neue Lage)


def _wait_anchor_update(state: dict) -> None:
    """Anker auf die aktuelle Position setzen, wenn noch keiner existiert,
    der letzte wait lange her ist oder der NPC den 150-m-Kreis verlassen hat."""
    global _WAIT_ANCHOR, _WAIT_SINCE
    npc = (state or {}).get("npc") or {}
    if not (npc.get("spawned") and npc.get("alive")):
        return
    pos = (float(npc.get("pos_x", 0.0)), float(npc.get("pos_z", 0.0)))
    now = time.monotonic()
    if (_WAIT_ANCHOR is None or now - _WAIT_LAST > _WAIT_GAP_RESET
            or math.hypot(pos[0] - _WAIT_ANCHOR[0],
                          pos[1] - _WAIT_ANCHOR[1]) > _WAIT_RADIUS):
        _WAIT_ANCHOR = pos
        _WAIT_SINCE = now


def _wait_hint(state: dict) -> str:
    """Hinweis ab 240 s Aufenthalt im 150-m-Kreis, wenn kein Mensch in der
    Umgebung ist und der NPC nicht im Fahrzeug sitzt, sonst ""."""
    if _WAIT_ANCHOR is None:
        return ""
    stay = time.monotonic() - _WAIT_SINCE
    if stay < _WAIT_HINT_SEC:
        return ""
    npc = (state or {}).get("npc") or {}
    if not (npc.get("spawned") and npc.get("alive")) or npc.get("in_vehicle"):
        return ""
    if any(e.get("kind") == "player" for e in (state or {}).get("nearby", [])):
        return ""
    ax, az = _WAIT_ANCHOR
    text = (f"Du hältst dich seit {stay / 60:.0f} min im Umkreis von 150 m um "
            f"x{ax:.0f} z{az:.0f} auf.")
    sq = _squad_now(state)
    if sq:
        return (text + f" KAMERADEN (live): {sq}. Kommt der, auf den du wartest, "
                       f"laut dieser Zeile näher? Wenn nicht: hingehen (travel_to) "
                       f"oder per say nachfragen.")
    return (text + " Kommt der, auf den du wartest, wirklich näher? Wenn nicht: "
                   "hingehen (travel_to) oder per say nachfragen.")


@mcp.tool()
def wait(seconds: int = 15) -> str:
    """Abwarten und beobachten (1-60 Sekunden). Danach kommt automatisch eine
    frische Lagebeschreibung. Spieler-Funk und kritische Ereignisse (Gefahr,
    Blutung) brechen das Warten sofort ab."""
    global _WAIT_LAST
    seconds = max(1, min(60, int(seconds)))
    if _brain_dead():
        return bridge_mod.DEATH_TEXT
    _wait_anchor_update(_safe_state())
    # Unterbrechbar warten (wie fish): Funk soll den NPC sofort wecken,
    # nicht erst nach Ablauf der vollen Wartezeit. "" = voll gewartet.
    reason = _sleep_interruptible(seconds)
    _WAIT_LAST = time.monotonic()
    if reason == "tot":
        return bridge_mod.DEATH_TEXT
    if reason:
        return ("Warten ABGEBROCHEN: " + _sleep_reason_text(reason) + "\n\n"
                + _observe_text())
    hint = _wait_hint(_safe_state())
    if hint:
        # Kameraden stehen schon im Hinweis - observe ohne zweite Zeile.
        return f"{seconds}s gewartet. {hint}\n\n" + _observe_text(squad=False)
    return f"{seconds}s gewartet.\n\n" + _observe_text()


if __name__ == "__main__":
    mcp.run()
