#!/usr/bin/env python3
"""IsuSurvivor Orchestrator - Schiedsrichter und Lagezentrum ueber den Squad.

Im Menue (Taste Einfg) per Toggle "Orchestr." ein-/ausschaltbar. AUS = die
vier NPCs laufen voellig unabhaengig (der saubere Modell-Benchmark, jedes
Modell loest dieselbe Lage allein). AN = dieser Prozess legt sich als
BEOBACHTER ueber den Squad:

  * Er liest jede Sekunde den Bridge-State aller aktiven Agenten
    (Position, HP, Kampf, Bedrohungen) und fuehrt ein komprimiertes
    gemeinsames Lagebild - das Wissen, das KEIN einzelner NPC hat.
  * Er protokolliert das Lagebild (arena/squad_state.json + ein laufendes
    Log) - das ist der Benchmark-Mitschnitt, voellig nicht-invasiv.
  * Bei einer WESENTLICHEN Aenderung (jemand faellt, verliert viel HP, eine
    Bedrohung taucht auf ODER kommt naeher, ein Kampf beginnt, jemand
    erreicht den Treffpunkt) funkt er EINEN kompakten Lagebericht
    ("FUNK von Lagezentrum: ..."). Prio-Funk (Tod/kritisch) geht an ALLE;
    Routine-Sitreps laufen durch einen Relevanz-/Empfaengerfilter
    (Selbstbetroffenheit + ~800 m Distanz, Audit 03.07.), damit nicht jeder
    Weckruf einen Lagebestaetigungs-Zug bei allen vier Modellen kostet.

WICHTIG - Schiedsrichter, NICHT Kommandeur: der Orchestrator BEFIEHLT den
NPCs nichts. Er teilt nur das gemeinsame Lagebild; jedes Modell entscheidet
weiter selbst. So bleibt der Vergleich der Modelle gueltig, der Squad bekommt
aber squad-weite Wahrnehmung. Der Funk ist absichtlich ratenbegrenzt (nur bei
echter Aenderung, fruehestens alle --min-broadcast Sekunden) - ein stiller
Tick darf nichts kosten.

Start (macht der arena_supervisor automatisch, wenn der Menue-Toggle AN ist):
  python daemon\\orchestrator.py --agents viktor,birgit,igor,konrad \\
      --camp-x 4233.7 --camp-z 8512.2
"""

import argparse
import json
import math
import os
import sys
import time

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
sys.path.insert(0, DAEMON_DIR)
from bridge import Bridge, DEFAULT_PROFILE  # noqa: E402

SQUAD_STATE_FILE = os.path.join(REPO_DIR, "arena", "squad_state.json")
LOG_FILE = os.path.join(REPO_DIR, "agent_home", "journal", "orchestrator.log")

# Absender im Funk - taucht beim NPC als "FUNK von Lagezentrum" auf.
RADIO_SENDER = "Lagezentrum"

# Was als LEBENDE Bedrohung in der Umgebung zaehlt (Heuristik; nach Live-
# Beobachtung verfeinerbar). WICHTIG: Beute/Nahrung am Boden (Fleisch, Felle)
# traegt "Wolf"/"Bear" im Namen, ist aber ein ITEM und KEINE Bedrohung - der
# Fehlfunk "Bedrohung WolfSteakMeat" kam genau daher. Passive Tiere (Kuh, Reh,
# Ziege) sind ebenfalls KEINE Bedrohung, nur Raubtiere (Baer, Wolf).
ZOMBIE_HINTS = ("zmb", "infected")
PREDATOR_HINTS = ("wolf", "bear", "canislupus", "ursusarctos")
NON_THREAT_KINDS = ("item", "corpse")
FOOD_LOOT_HINTS = ("meat", "steak", "pelt", "fat", "bone", "lard", "guts",
                   "skin", "sinew", "leather")
HOSTILE_KINDS = ("player", "survivor", "ai", "bandit")
# Eine bestehende Bedrohung loest erneut Funk aus, wenn sie um so viele Meter
# naeher kommt als beim LETZTEN gemeldeten Stand (threat_latch, nicht der
# letzte 3s-Tick - sonst feuerte ein sprintender Zombie mit 18 m/Tick jeden
# Tick neu). 15 -> 35 m angehoben (Audit 03.07.: 37% der Zuege waren reine
# Lagebestaetigungen; "Bedrohung 5 m naeher" ist kein Zustandswechsel). Ein
# langsam heranpirschender Baer bleibt trotzdem nicht stumm: der Latch
# akkumuliert, bis 35 m zusammenkommen oder er in die Gefahrenzone rutscht.
THREAT_STEP_CLOSER = 35.0

# Distanz-Relevanzfilter fuer ROUTINE-Sitreps: nur Agenten, die naeher als so
# viele Meter am Ausloeser-Agenten stehen (oder selbst verletzt/bedroht/im
# Kampf sind), bekommen den Funk. Wer 2 km entfernt lootet, braucht die
# Zombie-Distanz eines Kameraden nicht - jeder Weckruf ist ein LLM-Zug
# (Audit 03.07.: 3,15 USD reine Lagebestaetigungen). ~800 m = plausible
# Hoer-/Eingreifreichweite; Prio-Funk (Tod/kritisch) geht weiter an ALLE.
ROUTINE_RELEVANCE_DIST = 800.0


def log(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
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


def read_intent(bridge_dir: str, aid: str) -> str:
    """Aktuelle Gedankenzeile des Agenten (intent_<id>.txt, von run_agent aus
    der laufenden Aktivitaet auto-geschrieben). Das ist die ABSICHT, die dem
    reinen Positions-Funk fehlte: so sieht der Empfaenger 'Igor: auf dem
    Rueckweg zum Lager' statt nur Koordinaten - und rennt ihm nicht hinterher,
    wenn er schon selbst zurueck/gesund ist. Die Datei liegt im Bridge-Profil
    (bridge.dir), nicht im Projektordner. Bei fremdsprachigen NPCs liegt neben
    der latinisierten Bildschirm-Fassung eine NATIVE Begleitdatei
    (intent_native_<id>.txt) - die hat Vorrang, damit der Sitrep den lesbaren
    Originalvorsatz funkt statt Pinyin/Buckwalter."""
    npath = os.path.join(bridge_dir, f"intent_native_{aid}.txt")
    path = os.path.join(bridge_dir, f"intent_{aid}.txt")
    txt = ""
    for p in (npath, path):   # native zuerst, sonst die (ggf. latinisierte) Anzeige
        try:
            with open(p, "r", encoding="utf-8") as f:
                txt = f.read().strip()
        except OSError:
            txt = ""
        if txt:
            break
    if not txt:
        return ""
    txt = " ".join(txt.split())   # Zeilenumbrueche/Mehrfach-Whitespace glaetten
    if len(txt) > 60:
        txt = txt[:57].rstrip() + "..."
    return txt


def inbox_append(aid: str, text: str, prio: bool = False) -> bool:
    """Eine Funk-Zeile in die voice_inbox des Agenten haengen (run_agent liest
    sie und weckt das Gehirn als 'FUNK von Lagezentrum: ...'). prio=True
    markiert kritischen Funk (Tod/kritische HP), der einen laufenden Marsch
    SOFORT unterbricht; Routine-Sitreps (prio=False) werden erst beim naechsten
    Aufwachen gelesen, ohne den Marsch abzubrechen (bridge._inbox_should_interrupt
    wertet das aus - spart die redundanten observe/move_to-Zyklen)."""
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
    # Lebende Raubtiere - passive Tiere (Kuh, Reh, Ziege) zaehlen NICHT
    for hint in PREDATOR_HINTS:
        if hint in cls:
            return True
    # Spieler/AI nur im Hostile-Modus
    if hostile and kind in HOSTILE_KINDS:
        return True
    return False


def nearest_threat(state: dict, hostile: bool, max_dist: float):
    """(distance, beschreibung) der naechsten Bedrohung oder None."""
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


def snapshot(aid: str, state, hostile: bool, threat_dist: float) -> dict:
    """Komprimiertes Lagebild eines Agenten - nur was fuers gemeinsame Bild
    zaehlt, keine vollen Transkripte (sonst waechst der Kontext sinnlos)."""
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
    """Eine Agentenzeile fuer den Funk-Lagebericht (knapp = guenstig)."""
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


def detect_changes(prev: dict, cur: dict, hp_drop: float,
                   camp, rally_dist: float, prev_rally: set,
                   danger_dist: float, threat_latch: dict) -> list:
    """Wesentliche Aenderungen seit dem letzten Tick -> Liste von
    (ausloeser_aid, text)-Tupeln. Die aid des Betroffenen traegt jeder Grund
    explizit mit, damit der Empfaengerfilter im Broadcast NICHT ueber
    Namens-Substrings raten muss (Selbstbetroffenheit + Distanz-Relevanz,
    Audit 03.07.). threat_latch (aid -> Distanz der zuletzt GEMELDETEN
    Bedrohung) persistiert ueber Ticks: der Naeher-kommt-Vergleich laeuft
    gegen den letzten Meldestand, nicht gegen den letzten 3s-Tick - sonst
    ist der 35-m-Schritt nie erreichbar (langsamer Baer) oder feuert jeden
    Tick (sprintender Zombie)."""
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
            # faelschlich "X ist gefallen" (Despawn-Poll-Artefakt, 20.06.: Birgit
            # + Igor zeitgleich "gefallen" obwohl 4 km auseinander bei HP100).
            # Ein echter Tod hat spawned=true + alive=false (Leiche liegt da).
            continue
        if not s.get("alive"):
            if p and p.get("alive"):
                reasons.append((aid, f"{name} ist gefallen"))
            continue
        if p and p.get("online") and not p.get("alive") and s.get("alive"):
            reasons.append((aid, f"{name} ist zurueck im Spiel"))
        if p and p.get("alive"):
            drop = p.get("health", 100) - s.get("health", 100)
            if drop >= hp_drop:
                reasons.append((aid, f"{name} verliert HP (jetzt {s['health']})"))
            elif s.get("health", 100) < 35 <= p.get("health", 100):
                reasons.append((aid, f"{name} kritisch ({s['health']} HP)"))
        # Kampfbeginn (nicht->kaempfend) ist ein Ausloeser
        if s.get("fighting") and not (p and p.get("fighting")):
            reasons.append((aid, f"{name} ist im Kampf"))
        # Bedrohung: nur bei ZUSTANDSWECHSEL (neu aufgetaucht, in die
        # Gefahrenzone gerutscht, seit der letzten Meldung >= 35 m naeher) -
        # nicht pro Bewegungsschritt. So bleibt ein bereits in der Grundlinie
        # vorhandener Gegner nicht fuer immer stumm, spammt aber auch nicht.
        cur_threat = s.get("threat")
        prev_threat = p.get("threat") if p else None
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
                    reasons.append((aid, f"Bedrohung naeher an {name}: "
                                         f"{cur_threat['what']} {cd}m"))
                    threat_latch[aid] = cd
        else:
            # Bedrohung weg -> Latch loesen, damit eine NEUE als "neu
            # aufgetaucht" sauber wieder meldet.
            threat_latch.pop(aid, None)
        in_rally = dist2d(s["x"], s["z"], camp[0], camp[1]) <= rally_dist
        if in_rally and aid not in prev_rally:
            reasons.append((aid, f"{name} hat den Treffpunkt erreicht"))
    return reasons


def routine_relevant(recipient: dict, subject_ids: list, cur: dict) -> bool:
    """Distanz-Relevanzfilter fuer Routine-Sitreps: True, wenn der Empfaenger
    den Funk braucht. Immer relevant, wenn er selbst verletzt/bedroht/im Kampf
    ist (dann zaehlt jede Squad-Info); sonst nur, wenn mindestens ein
    Ausloeser-Agent naeher als ROUTINE_RELEVANCE_DIST steht. Ausloeser ohne
    brauchbare Position (offline/despawnt, z.B. 'Kontakt verloren') gelten
    konservativ als relevant - lieber ein Funk zu viel als Info verlieren."""
    if (recipient.get("fighting") or recipient.get("threat")
            or recipient.get("health", 100) < 60):
        return True
    rx, rz = recipient.get("x", 0), recipient.get("z", 0)
    for sub in subject_ids:
        s = cur.get(sub)
        if not s or not s.get("online") or not s.get("spawned"):
            return True
        if dist2d(rx, rz, s["x"], s["z"]) <= ROUTINE_RELEVANCE_DIST:
            return True
    return False


def build_sitrep(snaps: list, reasons: list) -> str:
    """Kompakter Squad-Lagebericht fuer den Funk. Bewusst kurz und mit dem
    expliziten Hinweis, dass es Lage-Info ist, KEIN Befehl."""
    # Pro Agent die Positionszeile PLUS seine aktuelle Absicht - genau die
    # Ebene, deren Fehlen den Birgit/Igor-Doppellauf ausloeste (sie rannte zu
    # einem Igor, der laut seiner Absicht schon selbst auf dem Rueckweg war).
    # short_line bleibt absichtsfrei (es speist auch das knappe Lage-Log).
    lines = []
    for s in snaps:
        if not s.get("online"):
            continue
        line = short_line(s)
        intent = s.get("intent")
        if intent:
            line += f" (will: {intent})"
        lines.append(line)
    body = "; ".join(lines)
    trigger = " | ".join(reasons[:4]) if reasons else ""
    msg = f"Squad-Lage: {body}."
    if trigger:
        msg += f" Ausloeser: {trigger}."
    msg += " (Lageinfo, kein Befehl - du entscheidest selbst.)"
    return msg


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
                    help="Bis zu dieser Distanz zaehlt eine Bedrohung")
    ap.add_argument("--hp-drop", type=float, default=20.0,
                    help="HP-Verlust, der einen Lagebericht ausloest")
    ap.add_argument("--min-broadcast", type=float, default=40.0,
                    help="Mindestabstand zwischen zwei Funk-Lageberichten (s)")
    ap.add_argument("--danger-dist", type=float, default=20.0,
                    help="Bedrohung unter dieser Distanz gilt als Eskalation "
                         "und loest auch dann Funk aus, wenn sie schon vorher da war")
    ap.add_argument("--heartbeat", type=float, default=0.0,
                    help="Optionaler periodischer Lagebericht alle N s, auch "
                         "ohne Aenderung (sichtbares Lebenszeichen beim Testen). "
                         "0 = aus, rein ereignisgetrieben (Default).")
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
        f"Treffpunkt {camp[0]:.0f}/{camp[1]:.0f}.")

    prev_snaps: dict = {}
    prev_rally: set = set()
    # aid -> Distanz der zuletzt GEMELDETEN Bedrohung (Latch fuer den
    # 35-m-Naeher-Schritt, siehe detect_changes)
    threat_latch: dict = {}
    last_broadcast = 0.0
    ticks = 0
    try:
        while True:
            time.sleep(args.interval)
            ticks += 1
            cur = {}
            for aid in ids:
                state = bridges[aid].read_state()
                snap = snapshot(aid, state, args.hostile, args.threat_dist)
                # Absicht nur fuer aktive Agenten anhaengen - sie macht den Funk
                # erst koordinationsfaehig (sieht, wer was VORHAT, nicht nur wo).
                if snap.get("spawned") and snap.get("alive"):
                    snap["intent"] = read_intent(bridges[aid].dir, aid)
                cur[aid] = snap
            snaps = [cur[aid] for aid in ids]
            summary = squad_summary(snaps, camp, args.rally_dist)

            # Lagebild rausschreiben (Benchmark-Mitschnitt, nicht-invasiv)
            try:
                payload = {"t": time.time(), "camp": camp,
                           "agents": snaps, "summary": summary}
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

            # Erster Tick: nur die Grundlinie setzen, NICHT funken. Sonst loest
            # eine beim Start schon bestehende Lage (Bedrohung in Reichweite,
            # Gruppe steht bereits am Treffpunkt) einen Lagebericht aus, obwohl
            # sich nichts GEAENDERT hat - genau die Verschwendung, die der
            # ratenbegrenzte Funk vermeiden soll.
            if not prev_snaps:
                prev_snaps = cur
                prev_rally = set(summary["at_rally"])
                log("Grundlinie gesetzt (erster Tick, kein Funk).")
                continue

            # Wesentliche Aenderung (oder optionaler Heartbeat) -> Funk
            now = time.monotonic()
            reasons = detect_changes(prev_snaps, cur, args.hp_drop, camp,
                                     args.rally_dist, prev_rally,
                                     args.danger_dist, threat_latch)
            why = [txt for _aid, txt in reasons]
            heartbeat_due = (args.heartbeat > 0
                             and (now - last_broadcast) >= args.heartbeat)
            ready = (now - last_broadcast) >= args.min_broadcast
            if (reasons or heartbeat_due) and not args.no_broadcast and ready:
                # Kritischer Funk (Tod/kritische HP) darf einen laufenden Marsch
                # unterbrechen; Routine (Bedrohung, Kampf, Position) nicht - die
                # NPCs lesen ihn beim naechsten Aufwachen.
                prio = any(("gefallen" in t) or ("kritisch" in t) for t in why)
                if prio or not reasons:
                    # Prio-Broadcast und Heartbeat gehen UNGEFILTERT an alle
                    # Lebenden - Tod/kritisch muss jeder wissen, der Heartbeat
                    # ist das bewusste Lebenszeichen beim Testen.
                    hb_why = why if why else ["Routine-Lagebericht"]
                    sitrep = build_sitrep(snaps, hb_why)
                    sent = [aid for aid in ids
                            if cur[aid].get("alive")
                            and inbox_append(aid, sitrep, prio)]
                    log(f"FUNK an {sent}: {hb_why[:4]}"
                        + (" [PRIO]" if prio else ""))
                else:
                    # ROUTINE-Sitrep: Relevanz- und Empfaengerfilter (Audit
                    # 03.07.: 37% der Zuege reine Lagebestaetigungen).
                    # a) Selbstbetroffenheit: Gruende, die NUR den Empfaenger
                    #    selbst betreffen, hat er laengst als eigenen GEFAHR-/
                    #    REISE-Weckruf - Doppelzustellung vermeiden; bei
                    #    gemischten Gruenden nur die fuer ihn FREMDEN funken.
                    # b) Distanz: nur Empfaenger nahe am Ausloeser (oder selbst
                    #    verletzt/bedroht/im Kampf), siehe routine_relevant.
                    sent = []
                    skipped = []
                    for aid in ids:
                        if not cur[aid].get("alive"):
                            continue
                        foreign = [(sub, t) for sub, t in reasons
                                   if sub != aid]
                        if not foreign:
                            skipped.append(aid)
                            continue
                        subs = [sub for sub, _t in foreign]
                        if not routine_relevant(cur[aid], subs, cur):
                            skipped.append(aid)
                            continue
                        sitrep = build_sitrep(
                            snaps, [t for _sub, t in foreign])
                        if inbox_append(aid, sitrep, False):
                            sent.append(aid)
                    log(f"FUNK an {sent}: {why[:4]}"
                        + (f" (gefiltert: {skipped})" if skipped else ""))
                if sent:
                    last_broadcast = now
            elif reasons and not args.no_broadcast:
                # Aenderung erkannt, aber Funk noch in der Sperrzeit - das ist
                # gewollt (Token-Disziplin), nur fuers Log vermerken.
                log(f"Aenderung gesehen (Funk gedrosselt): {why[:3]}")

            prev_snaps = cur
            prev_rally = set(summary["at_rally"])
    except KeyboardInterrupt:
        log("Orchestrator AUS (Strg+C).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
