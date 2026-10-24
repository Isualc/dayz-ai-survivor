#!/usr/bin/env python3
"""IsuSurvivor Orchestrator - Schiedsrichter und Lagezentrum über den Squad.

Im Menü (Taste Einfg) per Toggle "Orchestr." ein-/ausschaltbar. AUS = die
vier NPCs laufen völlig unabhängig (der saubere Modell-Benchmark, jedes
Modell löst dieselbe Lage allein). AN = dieser Prozess legt sich als
BEOBACHTER über den Squad:

  * Er liest jede Sekunde den Bridge-State aller aktiven Agenten
    (Position, HP, Kampf, Bedrohungen) und führt ein komprimiertes
    gemeinsames Lagebild - das Wissen, das KEIN einzelner NPC hat.
  * Er protokolliert das Lagebild (arena/squad_state.json + ein laufendes
    Log) - das ist der Benchmark-Mitschnitt, völlig nicht-invasiv.
  * Bei einer WESENTLICHEN Änderung (jemand fällt, verliert viel HP, eine
    Bedrohung taucht auf ODER kommt näher, ein Kampf beginnt, jemand
    erreicht den Treffpunkt) funkt er EINEN kompakten Lagebericht
    ("FUNK von Lagezentrum: ..."). Prio-Funk (Tod/kritisch) geht an ALLE;
    Routine-Sitreps laufen durch einen Relevanz-/Empfängerfilter
    (Selbstbetroffenheit + ~800 m Distanz, Audit 03.07.), damit nicht jeder
    Weckruf einen Lagebestätigungs-Zug bei allen vier Modellen kostet.
  * Sessionfix 24.10.: Gefilterte Empfänger bekommen statt nichts eine
    Kurzlage (nicht weckend, landet in run_agent im Info-Digest), wer weit
    weg von allen anderen steht, gilt als relevant, und jeder Lebende bekommt
    spätestens alle MIN_SUPPLY_SEC eine Kurzlage. Tod/kritisch und
    "zurück im Spiel" umgehen die Funk-Sperre, gedrosselte Ereignisse werden
    zwischengespeichert und mit "vor N s:" nachgeliefert.

WICHTIG - Schiedsrichter, NICHT Kommandeur: der Orchestrator BEFIEHLT den
NPCs nichts. Er teilt nur das gemeinsame Lagebild; jedes Modell entscheidet
weiter selbst. So bleibt der Vergleich der Modelle gültig, der Squad bekommt
aber squad-weite Wahrnehmung. Der Funk ist absichtlich ratenbegrenzt (nur bei
echter Änderung, frühestens alle --min-broadcast Sekunden) - ein stiller
Tick darf nichts kosten. Nicht weckende Lagen (prio=False) kosten keinen Zug,
run_agent hängt nur die neueste davon an den nächsten echten Weckruf.

Start (macht der arena_supervisor automatisch, wenn der Menü-Toggle AN ist):
  python daemon\\orchestrator.py --agents viktor,birgit,igor,konrad \\
      --camp-x 4233.7 --camp-z 8512.2
"""

import argparse
import json
import math
import os
import sys
import time
from collections import deque

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
sys.path.insert(0, DAEMON_DIR)
from bridge import Bridge, DEFAULT_PROFILE, _compass  # noqa: E402

SQUAD_STATE_FILE = os.path.join(REPO_DIR, "arena", "squad_state.json")
LOG_FILE = os.path.join(REPO_DIR, "agent_home", "journal", "orchestrator.log")

# Absender im Funk - taucht beim NPC als "FUNK von Lagezentrum" auf.
RADIO_SENDER = "Lagezentrum"

# Was als LEBENDE Bedrohung in der Umgebung zählt (Heuristik; nach Live-
# Beobachtung verfeinerbar). WICHTIG: Beute/Nahrung am Boden (Fleisch, Felle)
# trägt "Wolf"/"Bear" im Namen, ist aber ein ITEM und KEINE Bedrohung - der
# Fehlfunk "Bedrohung WolfSteakMeat" kam genau daher. Passive Tiere (Kuh, Reh,
# Ziege) sind ebenfalls KEINE Bedrohung, nur Raubtiere (Bär, Wolf).
ZOMBIE_HINTS = ("zmb", "infected")
PREDATOR_HINTS = ("wolf", "bear", "canislupus", "ursusarctos")
NON_THREAT_KINDS = ("item", "corpse")
FOOD_LOOT_HINTS = ("meat", "steak", "pelt", "fat", "bone", "lard", "guts",
                   "skin", "sinew", "leather")
HOSTILE_KINDS = ("player", "survivor", "ai", "bandit")
# Eine bestehende Bedrohung löst erneut Funk aus, wenn sie um so viele Meter
# näher kommt als beim LETZTEN gemeldeten Stand (threat_latch, nicht der
# letzte 3s-Tick - sonst feuerte ein sprintender Zombie mit 18 m/Tick jeden
# Tick neu). 15 -> 35 m angehoben (Audit 03.07.: 37% der Züge waren reine
# Lagebestätigungen; "Bedrohung 5 m näher" ist kein Zustandswechsel). Ein
# langsam heranpirschender Bär bleibt trotzdem nicht stumm: der Latch
# akkumuliert, bis 35 m zusammenkommen oder er in die Gefahrenzone rutscht.
THREAT_STEP_CLOSER = 35.0

# Distanz-Relevanzfilter für ROUTINE-Sitreps: nur Agenten, die näher als so
# viele Meter am Auslöser-Agenten stehen (oder selbst verletzt/bedroht/im
# Kampf sind), bekommen den vollen Funk. Wer 2 km entfernt lootet, braucht die
# Zombie-Distanz eines Kameraden nicht (Audit 03.07.: 3,15 USD reine
# Lagebestätigungen). ~800 m = plausible Hör-/Eingreifreichweite; Prio-Funk
# (Tod/kritisch) geht weiter an ALLE. Sessionfix 24.10.: Wer von ALLEN anderen
# Lebenden weiter als diese Distanz entfernt ist, gilt immer als relevant
# (der Isolierte braucht das Squad-Bild am dringendsten), und Gefilterte
# bekommen eine Kurzlage statt gar nichts.
ROUTINE_RELEVANCE_DIST = 800.0

# Mindestversorgung (Sessionfix 24.10., VI-5): jeder Lebende bekommt
# spätestens alle so viele Sekunden eine Kurzlage, auch ohne Auslöser. Läuft
# über einen eigenen Zeitstempel je Empfänger, NICHT über last_broadcast -
# sonst würde der Takt echte Meldungen drosseln. Kostet keinen Zug (prio=False).
MIN_SUPPLY_SEC = 180.0

# Gedrosselte Ereignis-Gründe (Sessionfix 24.10., VI-8/RESP-3): was während
# der Funk-Sperre passiert und NICHT im aktuellen Lagebild steht, wird
# zwischengespeichert und beim nächsten Senden mit "vor N s:" nachgeliefert.
# Zustandsgründe (Kampf, Bedrohung) nicht - die zeigt das Lagebild ohnehin.
# "zurueck im Spiel" bleibt bewusst ASCII: es ist der feste Meldetext aus
# detect_changes, auf den auch andere Pakete per Teilstring prüfen dürfen.
EVENT_WORDS = ("gefallen", "zurueck im Spiel", "verliert HP", "kritisch",
               "Treffpunkt erreicht", "Kontakt zu")
HELD_MAX = 6
# So viele Gründe stehen höchstens hinter "Ausloeser:" (frische zuerst).
TRIGGER_MAX = 6

# Kampf-Flag entprellen (Sessionfix 24.10., VI-3): "im KAMPF" erst, wenn
# Expansion-Fighting UND eine echte Bedrohung bzw. fremde KI in Reichweite
# (combat_near) in so vielen aufeinanderfolgenden Ticks stehen. Das rohe
# FSM-Flag flackert über einen 3-s-Tick und steht auch, wenn Expansion nur
# ein Item ansteuert (81 % der gemeldeten
# Kämpfe waren beim Zustellen schon vorbei).
FIGHT_CONFIRM_TICKS = 2

# Gefechtskontakt für die Entprellung (Review 24.10.): "threat" kennt Spieler/AI
# nur mit --hostile, der Orchestrator läuft aber per Supervisor-Gate nie damit.
# Expansion-Banditen und Patrouillen (Mission birgit: Raiders am Kopa-Gefängnis,
# Ambient-Patrouillen, Free-Modus) melden sich als kind "ai" mit LEEREM Namen
# (isubridge.c CollectNearby, IsuAgentRegistry.AgentName liefert "" für
# Nicht-Agenten) - ohne diesen Kontakt kam in keinem Gefecht gegen KI mehr
# "im KAMPF". Squad-Agenten (ai MIT Namen) und Spieler zählen nicht. Radius =
# Mod-Wahrnehmungsradius (GetObjectsAtPosition3D(center, 100)): Feuergefechte
# laufen oft jenseits der 60-m-threat_dist, die Kampf-FSM ist ohnehin das
# Hauptsignal, der Kontakt nur die Plausibilitätsprüfung dazu.
COMBAT_CONTACT_DIST = 100.0
FOREIGN_FIGHTER_KINDS = ("bandit", "survivor")

# Bewegungsrichtung im Sitrep (Sessionfix 24.10., RESP-10): ", unterwegs
# Richtung W", wenn ein Agent in den letzten ~MOTION_WINDOW_SEC mehr als
# MOTION_MIN_M zurückgelegt hat. prev_snaps taugt dafür nicht (nur 3 s alt,
# ~12 m), deshalb ein eigener kurzer Positionsverlauf je Agent.
MOTION_WINDOW_SEC = 20.0
MOTION_MIN_M = 30.0

# Absichten altern (Sessionfix 24.10., VI-4): bis INTENT_FRESH_SEC als
# "(will: ...)", bis INTENT_MAX_SEC als "(dachte vor N s: ...)", älter gar
# nicht mehr - eine Minuten alte Momentaufnahme ist kein Plan.
INTENT_FRESH_SEC = 60.0
INTENT_MAX_SEC = 180.0
INTENT_NONE_AGE = 9999.0


# Debug-Modus ($env:ISU_ORCH_DEBUG=1): loggt JEDEN Tick den aktuellen
# Entscheidungs-Gedanken (auch "alles ruhig"), nicht nur jede 10. Lage-Zeile.
# Default aus, sonst flutet der 3-s-Takt das Log.
DEBUG = os.environ.get("ISU_ORCH_DEBUG", "") not in ("", "0")


def log(msg: str) -> None:
    # Mit Datum (Sessionfix 24.10., VI-13): das Log läuft über viele Sessions,
    # ohne Datum standen '[22:12:05]' und '[17:44:23]' direkt untereinander.
    # Kein Code parst orchestrator.log (grep), das Format ist frei.
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [orchestrator] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def agent_home(aid: str) -> str:
    """Arbeitsverzeichnis eines Agenten (zentral in agent_paths)."""
    import agent_paths
    return agent_paths.agent_home_dir(aid)


def read_intent(bridge_dir: str, aid: str) -> tuple:
    """Aktuelle Gedankenzeile des Agenten als (text, alter_in_s).

    intent_<id>.txt schreibt run_agent aus der laufenden Aktivität (dazu
    dayz_mcp.intent und die Reise-/Erkundungs-Fortschrittszeilen). Das ist die
    ABSICHT, die dem reinen Positions-Funk fehlte: so sieht der Empfänger
    'Igor: auf dem Rückweg zum Lager' statt nur Koordinaten. Die Datei liegt im
    Bridge-Profil (bridge.dir), nicht im Projektordner. Bei fremdsprachigen
    NPCs liegt neben der latinisierten Bildschirm-Fassung eine NATIVE
    Begleitdatei (intent_native_<id>.txt) mit dem lesbaren Originalvorsatz.

    Sessionfix 24.10. (VI-4/HUD-5/VI-M2): Die NEUERE der beiden Dateien gewinnt
    (mtime). Vorher hatte die native Datei blind Vorrang - sie wird aber nur
    geschrieben, wenn die Bildschirmzeile vom Original abweicht, und überdeckte
    so minutenlang frische kurze Absichten. Beim selben Schreibvorgang legt
    run_agent die native Fassung NACH der Anzeige ab, sie ist dann neuer und
    bleibt bevorzugt. Das Alter geht mit zurück, damit der Sitrep alte
    Momentaufnahmen nicht als aktuelle Absicht funkt. Nichts gefunden ->
    ("", INTENT_NONE_AGE)."""
    npath = os.path.join(bridge_dir, f"intent_native_{aid}.txt")
    path = os.path.join(bridge_dir, f"intent_{aid}.txt")
    candidates = []
    for p in (npath, path):
        try:
            mtime = os.path.getmtime(p)
            with open(p, "r", encoding="utf-8") as f:
                txt = f.read().strip()
        except OSError:
            continue
        if txt:
            candidates.append((mtime, p == npath, txt))
    if not candidates:
        return "", INTENT_NONE_AGE
    # neueste Datei gewinnt; bei exakt gleicher mtime die native Fassung
    candidates.sort(key=lambda c: (c[0], c[1]))
    mtime, _native, txt = candidates[-1]
    txt = " ".join(txt.split())   # Zeilenumbrüche/Mehrfach-Whitespace glätten
    if len(txt) > 60:
        txt = txt[:57].rstrip() + "..."
    return txt, max(0.0, time.time() - mtime)


def inbox_append(aid: str, text: str, prio: bool = False) -> bool:
    """Eine Funk-Zeile in die voice_inbox des Agenten hängen (run_agent liest
    sie und weckt das Gehirn als 'FUNK von Lagezentrum: ...'). prio=True
    markiert weckenden Funk (Tod/kritische HP, beim Zurückgekehrten auch
    "zurück im Spiel"), der einen laufenden Marsch SOFORT unterbricht;
    Routine-Sitreps und Kurzlagen (prio=False) landen in run_agent nur im
    Info-Digest und werden beim nächsten Aufwachen gelesen, ohne den Marsch
    abzubrechen (bridge._inbox_should_interrupt wertet das aus)."""
    path = os.path.join(agent_home(aid), "voice_inbox.jsonl")
    entry = {"user": RADIO_SENDER, "text": text, "t": time.time(), "prio": prio}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def is_threat(entity: dict, hostile: bool) -> bool:
    kind = (entity.get("kind") or "").lower()
    cls = (entity.get("classname") or "").lower()
    # Bodenitems und Kadaver sind nie eine Bedrohung (Fix: Fehlfunk WolfSteakMeat)
    if kind in NON_THREAT_KINDS:
        return False
    for food in FOOD_LOOT_HINTS:
        if food in cls:
            return False
    # Zombies / Infizierte
    if "zombie" in kind or "infected" in kind:
        return True
    for hint in ZOMBIE_HINTS:
        if hint in cls:
            return True
    # Lebende Raubtiere - passive Tiere (Kuh, Reh, Ziege) zählen NICHT
    for hint in PREDATOR_HINTS:
        if hint in cls:
            return True
    # Spieler/AI nur im Hostile-Modus
    if hostile and kind in HOSTILE_KINDS:
        return True
    return False


def nearest_threat(state: dict, hostile: bool, max_dist: float):
    """(distance, beschreibung) der nächsten Bedrohung oder None."""
    best = None
    for e in state.get("nearby", []):
        if not is_threat(e, hostile):
            continue
        dist = e.get("distance", 9999.0)
        if dist > max_dist:
            continue
        if best is None or dist < best[0]:
            label = e.get("name") or e.get("classname") or e.get("kind") or "?"
            best = (dist, label)
    return best


def combat_contact(state: dict, max_dist: float) -> bool:
    """True, wenn eine FREMDE KI in max_dist steht: kind "ai" ohne
    Agentennamen (Expansion-Bandit/Patrouille) oder ein Bandit-/Survivor-Kind.
    Squad-Agenten (ai mit Namen) und Spieler zählen nicht. Nur für die
    Kampf-Entprellung, ändert "threat" und die Bedrohungs-Auslöser nicht."""
    for e in state.get("nearby") or []:
        kind = (e.get("kind") or "").lower()
        if kind == "ai":
            if e.get("name"):
                continue                 # Mit-Agent, kein Gegner
        elif kind not in FOREIGN_FIGHTER_KINDS:
            continue
        if e.get("distance", 9999.0) <= max_dist:
            return True
    return False


def snapshot(aid: str, state, hostile: bool, threat_dist: float) -> dict:
    """Komprimiertes Lagebild eines Agenten - nur was fürs gemeinsame Bild
    zählt, keine vollen Transkripte (sonst wächst der Kontext sinnlos).
    "fighting" ist hier noch das rohe Mod-Flag; Lagezentrum.prepare entprellt
    es (Rohwert bleibt als "fighting_raw" im squad_state). "combat_near"
    (nur gesetzt, wenn wahr) markiert fremde KI in Gefechtsreichweite, damit
    die Entprellung auch Kämpfe gegen Banditen ohne --hostile erkennt."""
    if not state:
        return {"id": aid, "online": False}
    npc = state.get("npc") or {}
    spawned = bool(npc.get("spawned"))
    alive = bool(npc.get("alive"))
    snap = {
        "id": aid,
        "online": True,
        "name": npc.get("name") or aid.capitalize(),
        "spawned": spawned,
        "alive": alive,
        "x": round(npc.get("pos_x", 0.0)),
        "z": round(npc.get("pos_z", 0.0)),
        "health": round(npc.get("health", 0.0)),
        "blood": round(npc.get("blood", 0.0)),
        "fighting": bool(npc.get("fighting")),
        "in_vehicle": bool(npc.get("in_vehicle")),
        "in_hands": npc.get("in_hands") or "leer",
        "seq": state.get("seq", 0),
    }
    if spawned and alive:
        nt = nearest_threat(state, hostile, threat_dist)
        if nt:
            snap["threat"] = {"dist": round(nt[0]), "what": nt[1]}
        if combat_contact(state, max(threat_dist, COMBAT_CONTACT_DIST)):
            snap["combat_near"] = True
    return snap


def dist2d(ax, az, bx, bz) -> float:
    return math.hypot(ax - bx, az - bz)


def squad_summary(snaps: list, camp, rally_dist: float) -> dict:
    """Abgeleitete Squad-Fakten - das, was kein einzelner NPC sieht."""
    alive = [s for s in snaps if s.get("online") and s.get("alive")]
    hurt = [s for s in alive if s.get("health", 100) < 60]
    threatened = [s for s in alive if s.get("threat")]
    at_rally = []
    for s in alive:
        if dist2d(s["x"], s["z"], camp[0], camp[1]) <= rally_dist:
            at_rally.append(s["id"])
    return {
        "alive": [s["id"] for s in alive],
        "hurt": [s["id"] for s in hurt],
        "under_threat": [s["id"] for s in threatened],
        "at_rally": at_rally,
    }


def short_line(s: dict) -> str:
    """Eine Agentenzeile für den Funk-Lagebericht (knapp = günstig)."""
    if not s.get("online"):
        return f"{s['id']}: kein Kontakt"
    if not s.get("spawned"):
        return f"{s['name']}: nicht im Spiel"
    if not s.get("alive"):
        return f"{s['name']}: TOT"
    parts = [f"{s['name']} x{s['x']} z{s['z']} HP{s['health']}"]
    if s.get("fighting"):
        parts.append("im KAMPF")
    if s.get("threat"):
        parts.append(f"Bedrohung {s['threat']['what']} {s['threat']['dist']}m")
    return " ".join(parts)


def compact_line(s: dict) -> str:
    """Agentenzeile der Kurzlage: nur Name, Position, HP bzw. TOT - ohne
    Bedrohung und Absicht (Sessionfix 24.10., VI-5)."""
    if not s.get("online"):
        return f"{s['id']}: kein Kontakt"
    if not s.get("spawned"):
        return f"{s['name']}: nicht im Spiel"
    if not s.get("alive"):
        return f"{s['name']}: TOT"
    return f"{s['name']} x{s['x']} z{s['z']} HP{s['health']}"


def detect_changes(prev: dict, cur: dict, hp_drop: float,
                   camp, rally_dist: float, prev_rally: set,
                   danger_dist: float, threat_latch: dict,
                   dead_memory: set = None) -> list:
    """Wesentliche Änderungen seit dem letzten Tick -> Liste von
    (auslöser_aid, text)-Tupeln. Die aid des Betroffenen trägt jeder Grund
    explizit mit, damit der Empfängerfilter im Broadcast NICHT über
    Namens-Substrings raten muss (Selbstbetroffenheit + Distanz-Relevanz,
    Audit 03.07.). threat_latch (aid -> Distanz der zuletzt GEMELDETEN
    Bedrohung) persistiert über Ticks: der Näher-kommt-Vergleich läuft
    gegen den letzten Meldestand, nicht gegen den letzten 3s-Tick - sonst
    ist der 35-m-Schritt nie erreichbar (langsamer Bär) oder feuert jeden
    Tick (sprintender Zombie).

    Grundlinie pro Agent (Sessionfix 24.10., VI-7): Ein Agent, der im vorigen
    Tick nicht online/gespawnt war, erzeugt KEINE Meldung - ein Erst-Spawn ist
    keine Rückkehr, kein Kampfbeginn und kein "Treffpunkt erreicht". Eine
    echte Rückkehr (Leiche -> neuer Körper) meldet weiter "zurueck im Spiel".
    dead_memory (optional, persistiert über Ticks) merkt sich gesehene Tode,
    damit die Rückkehr auch dann gemeldet wird, wenn zwischen Leiche und neuem
    Körper ein Tick ohne Körper lag (Despawn beim Respawn, TOD-CAP-Pause)."""
    reasons = []
    for aid, s in cur.items():
        p = prev.get(aid)
        if not s.get("online"):
            if p and p.get("online"):
                reasons.append((aid, f"Kontakt zu {aid} verloren"))
            continue
        name = s.get("name", aid)
        if not s.get("spawned"):
            # Body despawnt = Supervisor-Stop oder Respawn-/Session-Tausch, KEIN
            # Kampftod. Sonst meldet der Orchestrator beim Batch-Stop aller Bots
            # fälschlich "X ist gefallen" (Despawn-Poll-Artefakt, 20.06.: Birgit
            # + Igor zeitgleich "gefallen" obwohl 4 km auseinander bei HP100).
            # Ein echter Tod hat spawned=true + alive=false (Leiche liegt da).
            continue
        if not s.get("alive"):
            if dead_memory is not None:
                dead_memory.add(aid)
            if p and p.get("alive"):
                reasons.append((aid, f"{name} ist gefallen"))
            continue
        returned = bool(p and p.get("online") and p.get("spawned")
                        and not p.get("alive"))
        if dead_memory is not None and aid in dead_memory:
            returned = True
            dead_memory.discard(aid)
        if returned:
            reasons.append((aid, f"{name} ist zurueck im Spiel"))
        if not p or not p.get("online") or not p.get("spawned"):
            # Erst-Spawn bzw. neu angemeldet: das ist die Grundlinie für
            # diesen Agenten, kein Funk (Startfunk 24.10.: "Konrad ist zurück
            # im Spiel, im Kampf, hat den Treffpunkt erreicht" beim Erst-Spawn).
            continue
        if p.get("alive"):
            drop = p.get("health", 100) - s.get("health", 100)
            if drop >= hp_drop:
                reasons.append((aid, f"{name} verliert HP (jetzt {s['health']})"))
            elif s.get("health", 100) < 35 <= p.get("health", 100):
                reasons.append((aid, f"{name} kritisch ({s['health']} HP)"))
        # Kampfbeginn (nicht->kämpfend) ist ein Auslöser. Das Flag ist schon
        # entprellt (Lagezentrum.prepare), ein Flackern löst nichts mehr aus.
        if s.get("fighting") and not p.get("fighting"):
            reasons.append((aid, f"{name} ist im Kampf"))
        # Bedrohung: nur bei ZUSTANDSWECHSEL (neu aufgetaucht, in die
        # Gefahrenzone gerutscht, seit der letzten Meldung >= 35 m näher) -
        # nicht pro Bewegungsschritt. So bleibt ein bereits in der Grundlinie
        # vorhandener Gegner nicht für immer stumm, spammt aber auch nicht.
        cur_threat = s.get("threat")
        prev_threat = p.get("threat")
        if cur_threat:
            cd = cur_threat["dist"]
            if not prev_threat:
                reasons.append((aid, f"Bedrohung nahe {name}: "
                                     f"{cur_threat['what']} {cd}m"))
                threat_latch[aid] = cd
            else:
                # Latch = Distanz beim letzten gemeldeten Stand; Fallback auf
                # den Vor-Tick, falls der Latch fehlt (z.B. Orchestrator-Neustart
                # mit bereits bestehender Bedrohung in der Grundlinie).
                pd = threat_latch.get(aid, prev_threat.get("dist", 9999))
                if (cd <= danger_dist < pd) or (pd - cd) >= THREAT_STEP_CLOSER:
                    reasons.append((aid, f"Bedrohung näher an {name}: "
                                         f"{cur_threat['what']} {cd}m"))
                    threat_latch[aid] = cd
        else:
            # Bedrohung weg -> Latch lösen, damit eine NEUE als "neu
            # aufgetaucht" sauber wieder meldet.
            threat_latch.pop(aid, None)
        # Treffpunkt nur, wenn der Agent im vorigen Tick LEBTE - wer am Lager
        # respawnt, hat den Treffpunkt nicht "erreicht" (Fehlalarm 24.10.).
        in_rally = dist2d(s["x"], s["z"], camp[0], camp[1]) <= rally_dist
        if in_rally and aid not in prev_rally and p.get("alive"):
            reasons.append((aid, f"{name} hat den Treffpunkt erreicht"))
    return reasons


def routine_relevant(recipient: dict, subject_ids: list, cur: dict) -> bool:
    """Distanz-Relevanzfilter für Routine-Sitreps: True, wenn der Empfänger
    den vollen Funk braucht. Immer relevant, wenn er selbst verletzt/bedroht/
    im Kampf ist (dann zählt jede Squad-Info) oder wenn er weiter als
    ROUTINE_RELEVANCE_DIST von ALLEN anderen Lebenden entfernt ist (der
    Isolierte, z.B. frisch respawnt, braucht das Squad-Bild am dringendsten -
    Sessionfix 24.10., RESP-2); sonst nur, wenn mindestens ein Auslöser-Agent
    näher als ROUTINE_RELEVANCE_DIST steht. Auslöser ohne brauchbare Position
    (offline/despawnt, z.B. 'Kontakt verloren') gelten konservativ als
    relevant - lieber ein Funk zu viel als Info verlieren."""
    if (recipient.get("fighting") or recipient.get("threat")
            or recipient.get("health", 100) < 60):
        return True
    rx, rz = recipient.get("x", 0), recipient.get("z", 0)
    others = [s for s in cur.values()
              if s.get("id") != recipient.get("id") and s.get("online")
              and s.get("spawned") and s.get("alive")]
    if others and min(dist2d(rx, rz, s["x"], s["z"])
                      for s in others) > ROUTINE_RELEVANCE_DIST:
        return True
    for sub in subject_ids:
        s = cur.get(sub)
        if not s or not s.get("online") or not s.get("spawned"):
            return True
        if dist2d(rx, rz, s["x"], s["z"]) <= ROUTINE_RELEVANCE_DIST:
            return True
    return False


def build_sitrep(snaps: list, reasons: list, compact: bool = False,
                 stamp: str = None) -> str:
    """Kompakter Squad-Lagebericht für den Funk. Bewusst kurz und mit dem
    expliziten Hinweis, dass es Lage-Info ist, KEIN Befehl.

    Format (Schnittstelle C9): "Squad-Lage (Stand HH:MM:SS): <Agenten>.
    Ausloeser: <Gründe>. (Lageinfo, ...)". Der Präfix "Squad-Lage" und der
    Trenner "Ausloeser: " bleiben fest, run_agent baut daran ggf. die Lage bei
    Zustellung neu auf. compact=True ist die Kurzlage für gefilterte bzw.
    turnusmäßig versorgte Empfänger: je Agent nur Name, Position, HP, ohne
    Bedrohung und Absicht. Die Bewegungsrichtung (", unterwegs Richtung W",
    aus Lagezentrum.prepare) steht in beiden Fassungen - sie kostet ~5 Token
    und ist genau das, was zum Wiederfinden fehlte."""
    # Pro Agent die Positionszeile PLUS seine aktuelle Absicht - genau die
    # Ebene, deren Fehlen den Birgit/Igor-Doppellauf auslöste (sie rannte zu
    # einem Igor, der laut seiner Absicht schon selbst auf dem Rückweg war).
    # short_line bleibt absichtsfrei (es speist auch das knappe Lage-Log).
    lines = []
    for s in snaps:
        if not s.get("online"):
            continue
        line = compact_line(s) if compact else short_line(s)
        if s.get("spawned") and s.get("alive"):
            if s.get("heading"):
                line += f", unterwegs Richtung {s['heading']}"
            intent = s.get("intent")
            # ohne Altersangabe (Altbestand/Aufrufer ohne read_intent-Alter)
            # wie bisher als aktuelle Absicht behandeln
            age = s.get("intent_age")
            if age is None:
                age = 0.0
            if intent and not compact:
                if age <= INTENT_FRESH_SEC:
                    line += f" (will: {intent})"
                elif age <= INTENT_MAX_SEC:
                    line += f" (dachte vor {int(age)} s: {intent})"
        lines.append(line)
    body = "; ".join(lines)
    trigger = " | ".join(reasons[:TRIGGER_MAX]) if reasons else ""
    if stamp is None:
        stamp = time.strftime("%H:%M:%S")
    msg = f"Squad-Lage (Stand {stamp}): {body}."
    if trigger:
        msg += f" Ausloeser: {trigger}."
    msg += " (Lageinfo, kein Befehl - du entscheidest selbst.)"
    return msg


def _is_event_reason(text: str) -> bool:
    return any(w in text for w in EVENT_WORDS)


class Lagezentrum:
    """Entscheidungslogik des Orchestrators über die Ticks hinweg: Grundlinie,
    Änderungserkennung, Drossel samt Zwischenspeicher, Empfängerfilter,
    Kurzlagen, Heartbeat und Mindestversorgung. main() liest nur die States
    ein und ruft step(); so ist die Logik ohne Server testbar (send/clock
    injizierbar, Tests: test_sessionfix_1024_w3.py)."""

    def __init__(self, ids: list, camp, *, rally_dist: float = 60.0,
                 hp_drop: float = 20.0, min_broadcast: float = 40.0,
                 danger_dist: float = 20.0, heartbeat: float = 0.0,
                 no_broadcast: bool = False, send=None, clock=None,
                 logger=None):
        self.ids = list(ids)
        self.camp = camp
        self.rally_dist = rally_dist
        self.hp_drop = hp_drop
        self.min_broadcast = min_broadcast
        self.danger_dist = danger_dist
        self.heartbeat = heartbeat
        self.no_broadcast = no_broadcast
        self.send = send or inbox_append
        self.clock = clock or time.monotonic
        self.log = logger or log
        self.prev_snaps: dict = {}
        self.prev_rally: set = set()
        # aid -> Distanz der zuletzt GEMELDETEN Bedrohung (Latch für den
        # 35-m-Näher-Schritt, siehe detect_changes)
        self.threat_latch: dict = {}
        # gesehene Tode bis zur gemeldeten Rückkehr (detect_changes)
        self.dead_memory: set = set()
        self.last_broadcast = 0.0
        # Bezugspunkt des Heartbeats: die Grundlinie, damit der erste
        # Heartbeat nicht sofort nach dem Start feuert (Startfunk vermeiden).
        self.hb_ref = 0.0
        # Mindestversorgung: aid -> letzter Zeitpunkt, zu dem er irgendeine
        # Lage (voll oder kurz) bekam. Eigener Takt, NICHT last_broadcast.
        self.last_sent_to: dict = {}
        # gedrosselte Ereignis-Gründe: [(zeitpunkt, aid, text)], max HELD_MAX
        self.held: list = []
        # Kampf-Entprellung: aid -> aufeinanderfolgende Ticks mit Kampf+Bedrohung
        self.fight_ticks: dict = {}
        # Bewegungsrichtung: aid -> deque[(zeitpunkt, x, z)] der letzten ~20 s
        self.tracks: dict = {}
        # zuletzt gesehener Anzeigename je Agent (für Snapshots ohne State)
        self.names: dict = {}
        # Aktueller Entscheidungs-Gedanke (Debug-Wunsch 29.08.): WARUM wurde
        # gefunkt / geschwiegen / gefiltert. Wandert pro Tick in
        # squad_state.json ("thought", zeigt das Spectator-Dashboard).
        self.thought = "gestartet, warte auf die Grundlinie"

    # -- Vorverarbeitung -------------------------------------------------
    def prepare(self, cur: dict, now: float) -> None:
        """Entprellt das Kampf-Flag und setzt die Bewegungsrichtung - direkt
        in den Snapshots, damit detect_changes, routine_relevant, der Sitrep
        und squad_state.json denselben Wert sehen. Zusatzfelder im squad_state
        (fighting_raw, heading) sind rein additiv, kein Feld fällt weg."""
        for aid in self.ids:
            s = cur.get(aid)
            if not s or not s.get("online"):
                if s is not None:
                    # Name für squad_state-Leser (bridge.squad_lines: "<Name>
                    # nicht im Spiel"); short_line nutzt weiter die id.
                    s.setdefault("name", self.names.get(aid, aid.capitalize()))
                self.fight_ticks[aid] = 0
                self.tracks.pop(aid, None)
                continue
            if s.get("name"):
                self.names[aid] = s["name"]
            raw = bool(s.get("fighting"))
            s["fighting_raw"] = raw
            # Bedrohung (Zombie/Raubtier) ODER fremde KI in Gefechtsreichweite
            # (combat_near, siehe COMBAT_CONTACT_DIST) bestätigt das rohe Flag
            if raw and (s.get("threat") or s.get("combat_near")):
                self.fight_ticks[aid] = self.fight_ticks.get(aid, 0) + 1
            else:
                self.fight_ticks[aid] = 0
            s["fighting"] = self.fight_ticks[aid] >= FIGHT_CONFIRM_TICKS
            if not (s.get("spawned") and s.get("alive")):
                self.tracks.pop(aid, None)   # Tod/Despawn: Verlauf verwerfen
                continue
            track = self.tracks.setdefault(aid, deque())
            track.append((now, s["x"], s["z"]))
            # vorn bleibt der jüngste Punkt, der mindestens ein Fenster alt ist
            while len(track) > 1 and track[1][0] <= now - MOTION_WINDOW_SEC:
                track.popleft()
            t0, x0, z0 = track[0]
            dx, dz = s["x"] - x0, s["z"] - z0
            if (now - t0 >= MOTION_WINDOW_SEC * 0.75
                    and math.hypot(dx, dz) > MOTION_MIN_M):
                s["heading"] = _compass(dx, dz)

    # -- Hilfen ------------------------------------------------------------
    def _drain_held(self, now: float) -> list:
        """Zwischengespeicherte Ereignisse als 'vor N s: ...' und leeren."""
        out = [(a, f"vor {now - t0:.0f} s: {t}") for t0, a, t in self.held]
        self.held = []
        return out

    def _hold(self, reasons: list, now: float) -> None:
        known = {(a, t) for _t0, a, t in self.held}
        for a, t in reasons:
            if _is_event_reason(t) and (a, t) not in known:
                self.held.append((now, a, t))
                known.add((a, t))
        self.held = self.held[-HELD_MAX:]

    def _alive(self, cur: dict, aid: str) -> bool:
        s = cur.get(aid) or {}
        return bool(s.get("spawned") and s.get("alive"))

    # -- ein Tick ------------------------------------------------------------
    def step(self, cur: dict) -> dict:
        """Einen Beobachtungs-Tick verarbeiten. cur: aid -> Snapshot (wird in
        prepare ergänzt). Rückgabe: squad_summary für squad_state.json."""
        now = self.clock()
        self.prepare(cur, now)
        snaps = [cur[aid] for aid in self.ids]
        summary = squad_summary(snaps, self.camp, self.rally_dist)
        for aid in self.ids:
            if self._alive(cur, aid):
                # Mindestversorgung zählt ab dem ersten lebenden Tick
                self.last_sent_to.setdefault(aid, now)

        # Erster Tick: nur die Grundlinie setzen, NICHT funken. Sonst löst
        # eine beim Start schon bestehende Lage (Bedrohung in Reichweite,
        # Gruppe steht bereits am Treffpunkt) einen Lagebericht aus, obwohl
        # sich nichts GEÄNDERT hat - genau die Verschwendung, die der
        # ratenbegrenzte Funk vermeiden soll. Später dazukommende Agenten
        # bekommen ihre Grundlinie in detect_changes (pro Agent).
        if not self.prev_snaps:
            self.prev_snaps = cur
            self.prev_rally = set(summary["at_rally"])
            self.hb_ref = now
            self.thought = ("Grundlinie gesetzt - funke nur bei ECHTER "
                            "Änderung, nicht für die Startlage")
            self.log("Grundlinie gesetzt (erster Tick, kein Funk).")
            return summary

        reasons = detect_changes(self.prev_snaps, cur, self.hp_drop,
                                 self.camp, self.rally_dist, self.prev_rally,
                                 self.danger_dist, self.threat_latch,
                                 self.dead_memory)
        fresh_why = [txt for _aid, txt in reasons]
        # Tod/kritisch weckt ALLE und umgeht die Sperre (das war laut
        # Supervisor-Kommentar immer die Absicht, der Code prüfte aber die
        # Sperre zuerst - 24.10.: "Birgit ist gefallen" gedrosselt, nie
        # gefunkt). "zurueck im Spiel" umgeht die Sperre ebenfalls, weckt
        # aber NUR den Zurückgekehrten (er braucht sofort das Squad-Bild);
        # die anderen erfahren es nicht weckend über den Digest.
        prio = any(("gefallen" in t) or ("kritisch" in t) for t in fresh_why)
        returned = [sub for sub, t in reasons if "zurueck im Spiel" in t]
        urgent = prio or bool(returned)
        # Heartbeat nur mit mindestens einem lebenden Empfänger (Review 24.10.):
        # sonst blieb sent leer, last_broadcast stand still und hb_due war nach
        # einem Wipe, in einer TOD-CAP-Pause aller oder während der close_game-
        # Wartezeit in JEDEM Tick wahr ("FUNK an []" alle 3 s). Solange niemand
        # lebt, steht die Heartbeat-Uhr: der erste Heartbeat kommt erst
        # heartbeat Sekunden nach dem letzten Tick ohne Lebende, also nicht
        # direkt beim (Wieder-)Spawn - kein Startfunk.
        anyone_alive = any(self._alive(cur, aid) for aid in self.ids)
        if not anyone_alive:
            self.hb_ref = now
        hb_due = (self.heartbeat > 0 and anyone_alive
                  and (now - max(self.last_broadcast, self.hb_ref))
                  >= self.heartbeat)
        # Der Heartbeat ist selbst schon über last_broadcast getaktet und
        # weckt niemanden - er braucht die Sperre nicht zusätzlich.
        ready = (urgent or hb_due
                 or (now - self.last_broadcast) >= self.min_broadcast)

        if self.no_broadcast:
            if reasons:
                self.thought = ("Beobachter-Modus (kein Funk), Änderung notiert: "
                                + "; ".join(fresh_why[:3]))
            else:
                self.thought = self._calm_thought(summary)
        elif (reasons or self.held or hb_due) and ready:
            self._broadcast(cur, snaps, reasons, prio, returned, hb_due, now)
        elif reasons:
            # Änderung erkannt, aber Funk noch in der Sperrzeit - das ist
            # gewollt (Token-Disziplin). Ereignisse, die das Lagebild nicht
            # zeigt, für das nächste Senden vormerken.
            self._hold(reasons, now)
            wait_s = self.min_broadcast - (now - self.last_broadcast)
            self.thought = ("Änderung gesehen, Funk gedrosselt (Sperrzeit noch "
                            f"{max(0, wait_s):.0f}s): " + "; ".join(fresh_why[:3])
                            + (f" | vorgemerkt: {len(self.held)}"
                               if self.held else ""))
            self.log(f"Änderung gesehen (Funk gedrosselt): {fresh_why[:3]}"
                     + (f" - vorgemerkt: {len(self.held)}" if self.held else ""))
        else:
            self.thought = self._calm_thought(summary)

        if not self.no_broadcast:
            self._min_supply(cur, snaps, now)

        self.prev_snaps = cur
        self.prev_rally = set(summary["at_rally"])
        return summary

    def _calm_thought(self, summary: dict) -> str:
        return (f"alles ruhig - {len(summary['alive'])} am Leben"
                + (f", verletzt: {summary['hurt']}"
                   if summary['hurt'] else "")
                + (f", bedroht: {summary['under_threat']}"
                   if summary['under_threat'] else "")
                + (f", am Treffpunkt: {summary['at_rally']}"
                   if summary['at_rally'] else ""))

    def _broadcast(self, cur: dict, snaps: list, reasons: list, prio: bool,
                   returned: list, hb_due: bool, now: float) -> None:
        # frische Gründe zuerst (sie entscheiden über prio und stehen sicher
        # hinter "Ausloeser:"), danach die nachgelieferten
        all_reasons = list(reasons) + self._drain_held(now)
        why = [t for _sub, t in all_reasons]
        sent = []
        brief = []
        skipped = []
        if prio or returned or hb_due or not all_reasons:
            # Prio, Rückkehr und Heartbeat gehen UNGEFILTERT an alle Lebenden.
            # Wecken (prio=True) nur bei Tod/kritisch bzw. beim Zurückgekehrten
            # selbst - der Heartbeat und die Rückkehr der ANDEREN fahren im
            # Info-Digest mit und brechen keinen Marsch ab.
            hb_why = why if why else ["Routine-Lagebericht"]
            sitrep = build_sitrep(snaps, hb_why)
            woken = []
            for aid in self.ids:
                if not self._alive(cur, aid):
                    continue
                wake = prio or aid in returned
                if self.send(aid, sitrep, wake):
                    sent.append(aid)
                    if wake:
                        woken.append(aid)
            tag = " [PRIO]" if prio else ""
            if returned and not prio:
                tag = f" [weckt {woken}]"
            if hb_due and not all_reasons:
                tag += " [Heartbeat]"
            self.log(f"FUNK an {sent}: {hb_why[:4]}{tag}")
            self.thought = ((("[PRIO] " if prio else "")
                             + f"Funk an {sent or 'niemanden'}: "
                             + "; ".join(why[:3] if why else ["Heartbeat"])))
        else:
            # ROUTINE-Sitrep: Relevanz- und Empfängerfilter (Audit 03.07.).
            # a) Selbstbetroffenheit: Gründe, die NUR den Empfänger selbst
            #    betreffen, hat er längst als eigenen GEFAHR-/REISE-Weckruf -
            #    Doppelzustellung vermeiden; bei gemischten Gründen nur die
            #    für ihn FREMDEN funken.
            # b) Distanz: nur Empfänger nahe am Auslöser (oder selbst
            #    verletzt/bedroht/im Kampf/isoliert), siehe routine_relevant.
            # Gefilterte bekommen eine Kurzlage statt gar nichts (24.10.:
            # Birgit 27 min ohne Lage, 25 von 26 Funk-Entscheidungen an
            # niemanden) - nicht weckend, kostet keinen Zug.
            for aid in self.ids:
                if not self._alive(cur, aid):
                    continue
                foreign = [(sub, t) for sub, t in all_reasons if sub != aid]
                if not foreign:
                    sitrep = build_sitrep(snaps, [], compact=True)
                    if self.send(aid, sitrep, False):
                        brief.append(aid)
                    skipped.append(f"{aid} (nur Eigenbetroffenheit -> Kurzlage)")
                    continue
                subs = [sub for sub, _t in foreign]
                if not routine_relevant(cur[aid], subs, cur):
                    sitrep = build_sitrep(snaps, [t for _sub, t in foreign],
                                          compact=True)
                    if self.send(aid, sitrep, False):
                        brief.append(aid)
                    skipped.append(f"{aid} (>{ROUTINE_RELEVANCE_DIST:.0f}m "
                                   "vom Auslöser, selbst ok -> Kurzlage)")
                    continue
                sitrep = build_sitrep(snaps, [t for _sub, t in foreign])
                if self.send(aid, sitrep, False):
                    sent.append(aid)
            self.log(f"FUNK an {sent}: {why[:4]}"
                     + (f" + Kurzlage an {brief}" if brief else "")
                     + (f" (gefiltert: {skipped})" if skipped else ""))
            self.thought = (f"Funk an {sent or 'niemanden'}: "
                            + "; ".join(why[:3])
                            + (f" | Kurzlage an {', '.join(brief)}"
                               if brief else "")
                            + (f" | gefiltert: {', '.join(skipped)}"
                               if skipped else ""))
        # Jeder Heartbeat-VERSUCH startet die Heartbeat-Uhr neu, auch wenn
        # send() scheiterte (voice_inbox nicht schreibbar) - sonst feuerte er
        # bis zum ersten Erfolg in jedem Tick erneut.
        if hb_due:
            self.hb_ref = now
        # Eine Lage ging raus (auch eine Kurzlage) -> Sperre neu starten.
        if sent or brief:
            self.last_broadcast = now
        for aid in sent + brief:
            self.last_sent_to[aid] = now

    def _min_supply(self, cur: dict, snaps: list, now: float) -> None:
        """Mindestversorgung: wer seit MIN_SUPPLY_SEC keine Lage bekam, kriegt
        eine Kurzlage - unabhängig von Sperre und Auslösern (prio=False)."""
        supplied = []
        sitrep = None
        for aid in self.ids:
            if not self._alive(cur, aid):
                continue
            if now - self.last_sent_to.get(aid, now) < MIN_SUPPLY_SEC:
                continue
            if sitrep is None:
                sitrep = build_sitrep(snaps, ["Routine-Lagebericht"],
                                      compact=True)
            if self.send(aid, sitrep, False):
                self.last_sent_to[aid] = now
                supplied.append(aid)
        if supplied:
            self.log(f"Kurzlage an {supplied} (Mindestversorgung "
                     f"{MIN_SUPPLY_SEC:.0f} s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", required=True,
                    help="Kommagetrennte aktive Agent-IDs (viktor,birgit,...)")
    ap.add_argument("--camp-x", type=float, default=4233.7)
    ap.add_argument("--camp-z", type=float, default=8512.2)
    ap.add_argument("--interval", type=float, default=3.0,
                    help="Beobachtungstakt in Sekunden")
    ap.add_argument("--rally-dist", type=float, default=60.0,
                    help="Radius um das Lager, ab dem 'am Treffpunkt' gilt")
    ap.add_argument("--threat-dist", type=float, default=60.0,
                    help="Bis zu dieser Distanz zählt eine Bedrohung")
    ap.add_argument("--hp-drop", type=float, default=20.0,
                    help="HP-Verlust, der einen Lagebericht auslöst")
    ap.add_argument("--min-broadcast", type=float, default=40.0,
                    help="Mindestabstand zwischen zwei Funk-Lageberichten (s); "
                         "Tod/kritisch und Rückkehr umgehen ihn")
    ap.add_argument("--danger-dist", type=float, default=20.0,
                    help="Bedrohung unter dieser Distanz gilt als Eskalation "
                         "und löst auch dann Funk aus, wenn sie schon vorher da war")
    ap.add_argument("--heartbeat", type=float, default=0.0,
                    help="Periodischer Lagebericht, wenn N s lang keine Lage "
                         "rausging (ungefiltert, nicht weckend). 0 = aus "
                         "(Default hier; der Arena-Supervisor gibt 150 mit).")
    ap.add_argument("--hostile", action="store_true",
                    help="Hostile-Modus: auch Spieler/AI gelten als Bedrohung")
    ap.add_argument("--no-broadcast", action="store_true",
                    help="Reiner Beobachter: nur protokollieren, nie funken "
                         "(maximal sauberer Benchmark)")
    args = ap.parse_args()

    ids = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not ids:
        log("Keine Agenten angegeben - Orchestrator beendet sich.")
        return 1
    camp = (args.camp_x, args.camp_z)
    bridges = {aid: Bridge(DEFAULT_PROFILE, aid) for aid in ids}
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    mode = "Beobachter (kein Funk)" if args.no_broadcast else "Lagezentrum + Funk"
    log(f"Orchestrator AN: {len(ids)} Agenten {ids}, Modus: {mode}, "
        f"Treffpunkt {camp[0]:.0f}/{camp[1]:.0f}, Sperre {args.min_broadcast:.0f} s, "
        f"Heartbeat {args.heartbeat:.0f} s (0 = aus).")

    lz = Lagezentrum(ids, camp, rally_dist=args.rally_dist,
                     hp_drop=args.hp_drop, min_broadcast=args.min_broadcast,
                     danger_dist=args.danger_dist, heartbeat=args.heartbeat,
                     no_broadcast=args.no_broadcast)
    ticks = 0
    try:
        while True:
            time.sleep(args.interval)
            ticks += 1
            cur = {}
            for aid in ids:
                state = bridges[aid].read_state()
                snap = snapshot(aid, state, args.hostile, args.threat_dist)
                # Absicht nur für aktive Agenten anhängen - sie macht den Funk
                # erst koordinationsfähig (sieht, wer was VORHAT, nicht nur wo).
                # intent_age (s) ist additiv im squad_state, build_sitrep
                # entscheidet damit über "will" / "dachte vor N s" / weglassen.
                if snap.get("spawned") and snap.get("alive"):
                    intent, age = read_intent(bridges[aid].dir, aid)
                    snap["intent"] = intent
                    if intent:
                        snap["intent_age"] = round(age)
                cur[aid] = snap
            summary = lz.step(cur)
            snaps = [cur[aid] for aid in ids]

            # Lagebild rausschreiben (Benchmark-Mitschnitt, nicht-invasiv).
            # Nach step(), damit Kampf-Flag (entprellt), Richtung und der
            # Gedanke dieses Ticks drinstehen. Felder nur ergänzen, nie
            # entfernen/umbenennen (Leser: spectator_server, bridge.squad_lines).
            try:
                payload = {"t": time.time(), "camp": camp,
                           "agents": snaps, "summary": summary,
                           "thought": lz.thought}
                tmp = SQUAD_STATE_FILE + ".tmp"
                os.makedirs(os.path.dirname(SQUAD_STATE_FILE), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, SQUAD_STATE_FILE)
            except OSError as e:
                log(f"squad_state.json nicht schreibbar: {e}")

            # Nur jeden 10. Tick eine Log-Zeile (sonst flutet das Log)
            if ticks % 10 == 1:
                log("Lage: " + " | ".join(short_line(s) for s in snaps))
            if DEBUG:
                log("denkt: " + lz.thought)
    except KeyboardInterrupt:
        log("Orchestrator AUS (Strg+C).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
