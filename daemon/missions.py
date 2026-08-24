#!/usr/bin/env python3
"""IsuSurvivor Missions-Engine - laedt Skript-Missionen aus arena/missions/<id>.json
und faehrt ihre Events/Erfolgspruefung als Supervisor-Thread.

Bisher war die einzige Mission ("birgit") hart im arena_supervisor codiert.
Jetzt sind Missionen Daten: das In-Game-Menue schickt weiterhin "mission:<id>"
ueber den bestehenden Kanal, der Supervisor holt sich die Definition hier ab.
WICHTIG (Konvention wie s_LangCodes == LANG_NAMES): die Missions-Liste im Mod-
Menue muss 1:1 der Dateiliste in arena/missions/ entsprechen (v1: birgit, horde).

Schema (tolerant gelesen - unbekannte Felder werden ignoriert):
  {
    "id": "horde", "name": "...", "map": "" | "enoch" | ...,
    "camp": [x, z] | null,          # null = Lagerpunkt aus dem Menue
    "rally": [x, z] | null,
    "briefing": "...",              # --mission-Text ({tx}/{tz} = Zielkoords)
    "role_briefings": {"captive": "...", "freed": "..."},
    "patrols": [ ...AIPatrolSettings-Eintraege... ],   # optional, injiziert
    "objective": {"type": "rescue"|"defend", ...},
    "events": [{"at_sec": 30, "type": "broadcast"|"spawn_infected", ...}],
    "success": {"type": "captive_released"|"all_infected_dead", ...},
    "on_success_broadcast": "...", "on_fail_broadcast": "..."
  }

Die Rettungslogik (Gefangene erscheint erst nach der Saeuberung) bleibt im
Supervisor (Arena.check_birgit_release) - sie braucht dessen Prozessverwaltung.
Dieser Thread hier faehrt die zeitgesteuerten Events (Horde-Wellen) und die
Erfolgspruefung "alle Infizierten tot" - als BEOBACHTER/Spielleiter, er befiehlt
den NPCs nichts (Benchmark-Charakter bleibt, wie beim Orchestrator).
"""

import json
import math
import os
import random
import threading
import time

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
MISSIONS_DIR = os.path.join(REPO_DIR, "arena", "missions")

from bridge import Bridge, DEFAULT_PROFILE  # noqa: E402

# Mission-Ordner je Karte (Kopie aus arena_supervisor - muss dazu passen)
MPMISSIONS = os.path.join(os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer"), "mpmissions")
MISSION_DIRS = {
    "chernarus": "dayzOffline.chernarusplus",
    "enoch": "dayzOffline.enoch",
    "livonia": "dayzOffline.enoch",
    "sakhal": "dayzOffline.sakhal",
}

# Absender im Funk (wie orchestrator.RADIO_SENDER) - taucht beim NPC als
# "FUNK von Lagezentrum: ..." auf.
RADIO_SENDER = "Lagezentrum"

# Was in nearby als Infizierter zaehlt (wie orchestrator.ZOMBIE_HINTS)
ZOMBIE_HINTS = ("zmb", "infected", "zombie")


# ------------------------------------------------------------------ Laden

def mission_ids() -> list[str]:
    """Verfuegbare Missions-IDs = Dateiliste arena/missions/*.json."""
    try:
        return sorted(f[:-5] for f in os.listdir(MISSIONS_DIR)
                      if f.endswith(".json"))
    except OSError:
        return []


def load(mission_id: str) -> dict | None:
    """Mission laden; None bei unbekannter ID oder kaputtem JSON."""
    mid = (mission_id or "").strip().lower()
    if not mid:
        return None
    path = os.path.join(MISSIONS_DIR, mid + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(m, dict):
        return None
    m.setdefault("id", mid)
    return m


def format_briefing(text: str, tx: float, tz: float) -> str:
    """{tx}/{tz}-Platzhalter einsetzen; Briefings ohne Platzhalter bleiben
    unveraendert (KeyError/IndexError durch fremde Klammern abfangen)."""
    if not text:
        return ""
    try:
        return text.format(tx=int(tx), tz=int(tz))
    except (KeyError, IndexError, ValueError):
        return text


def needs_thread(mission: dict) -> bool:
    """True, wenn die Mission zeitgesteuerte Events oder eine Infizierten-
    Erfolgspruefung hat (dann startet der Supervisor einen MissionRun)."""
    if mission.get("events"):
        return True
    return ((mission.get("success") or {}).get("type") == "all_infected_dead")


# ------------------------------------------------------------------ Helfer

def agent_home(aid: str) -> str:
    """Arbeitsverzeichnis eines Agenten (zentral in agent_paths)."""
    import agent_paths
    return agent_paths.agent_home_dir(aid)


def broadcast(agent_ids: list[str], text: str, prio: bool = True) -> int:
    """Eine Funk-Zeile in die voice_inbox ALLER Agenten haengen (run_agent
    weckt das Gehirn als 'FUNK von Lagezentrum: ...'). prio=True unterbricht
    laufende Maersche (bridge._inbox_should_interrupt). Gibt die Anzahl der
    erreichten Inboxen zurueck."""
    sent = 0
    entry = {"user": RADIO_SENDER, "text": text, "t": time.time(), "prio": prio}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    for aid in agent_ids:
        path = os.path.join(agent_home(aid), "voice_inbox.jsonl")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            sent += 1
        except OSError:
            pass
    return sent


def ensure_patrols(amap: str, patrols: list, log=print) -> None:
    """Optionale AIPatrolSettings-Eintraege der Mission in die Karten-Settings
    injizieren (idempotent, Match ueber "Name"). Wirkt ab dem naechsten
    Server-Neustart - wie der bestehende set_patrols-Toggle."""
    if not patrols:
        return
    mission_dir = MISSION_DIRS.get((amap or "").strip().lower())
    if not mission_dir:
        log(f"Missions-Patrouillen: Karte '{amap}' ohne Mission-Mapping - "
            f"uebersprungen.")
        return
    path = os.path.join(MPMISSIONS, mission_dir, "expansion", "settings",
                        "AIPatrolSettings.json")
    if not os.path.exists(path):
        log(f"Missions-Patrouillen: AIPatrolSettings fuer '{amap}' nicht da - "
            f"uebersprungen.")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        existing = settings.get("Patrols")
        if not isinstance(existing, list):
            log("Missions-Patrouillen: kein 'Patrols'-Array - uebersprungen.")
            return
        have = {p.get("Name") for p in existing if isinstance(p, dict)}
        added = 0
        for p in patrols:
            if isinstance(p, dict) and p.get("Name") and p["Name"] not in have:
                existing.append(p)
                added += 1
        if added:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
            os.replace(tmp, path)
            log(f"Missions-Patrouillen: {added} Eintrag/Eintraege injiziert "
                f"(wirkt ab naechstem Server-Neustart).")
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log(f"Missions-Patrouillen fehlgeschlagen: {e}")


def _dist2d(x1: float, z1: float, x2: float, z2: float) -> float:
    return math.hypot(x1 - x2, z1 - z2)


# ------------------------------------------------------------- Missions-Lauf

class MissionRun(threading.Thread):
    """Faehrt die zeitgesteuerten Events einer Mission und prueft den Erfolg.

    Laeuft als Daemon-Thread im Supervisor; stop() beendet ihn sauber (kein
    Ergebnis-Broadcast, die Runde wurde ja abgebrochen). Alle Welt-Zugriffe
    gehen ueber die bestehende File-Bridge des jeweils NAECHSTEN Agenten -
    kein neuer Kanal, kein Mod-Repack noetig, solange die Mod spawn_infected
    kennt (x/z/count sind optionale neue Felder, Fallback s.u.)."""

    def __init__(self, mission: dict, camp: tuple[float, float],
                 agent_ids: list[str], log=print, status=None,
                 profile_dir: str = DEFAULT_PROFILE):
        super().__init__(daemon=True, name=f"mission-{mission.get('id', '?')}")
        self.mission = mission
        self.camp = camp
        self.agent_ids = list(agent_ids)
        self.log = log
        self.status = status          # Kurzstatus fuers In-Game-Menue (optional)
        self.profile_dir = profile_dir
        # NICHT self._stop nennen: threading.Thread hat intern eine
        # _stop()-METHODE (join ruft sie auf) - ein Event dort crasht
        # mit "'Event' object is not callable".
        self._stop_evt = threading.Event()
        self._spawned = 0             # ueber alle Wellen angeforderte Infizierte
        self._last_wave = 0.0         # monotonic der letzten Spawn-Welle

    def stop(self) -> None:
        self._stop_evt.set()

    # ------------------------------------------------------------- Weltsicht

    def _states(self) -> dict[str, dict]:
        out = {}
        for aid in self.agent_ids:
            st = Bridge(self.profile_dir, aid).read_state()
            if st:
                out[aid] = st
        return out

    def _nearest_alive(self) -> str | None:
        """Agent, der dem Lagerpunkt am naechsten steht und lebt - sein
        Bridge-Kanal traegt die spawn_infected-Kommandos."""
        best, best_d = None, 1e12
        for aid, st in self._states().items():
            npc = st.get("npc") or {}
            if not (npc.get("spawned") and npc.get("alive")):
                continue
            d = _dist2d(npc.get("pos_x", 0.0), npc.get("pos_z", 0.0),
                        self.camp[0], self.camp[1])
            if d < best_d:
                best, best_d = aid, d
        return best

    def _infected_near_camp(self, radius: float) -> int:
        """Anzahl der von IRGENDEINEM Agenten gesehenen lebenden Infizierten
        im Radius um den Lagerpunkt (Leichen/Items zaehlen nicht)."""
        seen = 0
        for st in self._states().values():
            for e in st.get("nearby", []):
                kind = (e.get("kind") or "").lower()
                cls = (e.get("classname") or "").lower()
                if kind in ("item", "corpse"):
                    continue
                if not any(h in kind or h in cls for h in ZOMBIE_HINTS):
                    continue
                if _dist2d(e.get("x", 0.0), e.get("z", 0.0),
                           self.camp[0], self.camp[1]) <= radius:
                    seen += 1
        return seen

    # --------------------------------------------------------------- Events

    def _do_broadcast(self, ev: dict) -> None:
        text = (ev.get("text") or (ev.get("params") or {}).get("text") or "").strip()
        if not text:
            return
        prio = bool(ev.get("prio", (ev.get("params") or {}).get("prio", True)))
        n = broadcast(self.agent_ids, text, prio=prio)
        self.log(f"MISSION-Funk an {n} Agent(en): {text[:100]}")

    def _do_spawn_infected(self, ev: dict) -> None:
        p = dict(ev.get("params") or {})
        p.update({k: v for k, v in ev.items() if k not in ("type", "at_sec", "params")})
        count = max(1, min(10, int(p.get("count", 1))))
        rmin = float(p.get("ring_min_m", 80.0))
        rmax = float(p.get("ring_max_m", max(120.0, rmin)))
        aid = self._nearest_alive()
        if not aid:
            self.log("MISSION: kein lebender Agent fuer spawn_infected - Welle "
                     "uebersprungen.")
            return
        # Zufaelliger Punkt im Ring um den Lagerpunkt
        ang = random.uniform(0.0, 2.0 * math.pi)
        r = random.uniform(rmin, rmax)
        sx = self.camp[0] + math.sin(ang) * r
        sz = self.camp[1] + math.cos(ang) * r
        br = Bridge(self.profile_dir, aid)
        res = None
        try:
            # Neue Schnittstelle: spawn_infected mit x/z/count (Mod-Seite).
            res = br.run("spawn_infected", timeout=45, x=sx, z=sz, count=count)
        except TypeError:
            # bridge.send kennt 'count' (noch) nicht - alte Bridge-Fassung
            res = None
        except Exception as e:
            self.log(f"MISSION: spawn_infected via {aid} fehlgeschlagen: {e}")
            res = None
        if res and res.get("status") == "done":
            self._spawned += count
            self._last_wave = time.monotonic()
            self.log(f"MISSION: Welle gespawnt - {count} Infizierte bei "
                     f"{sx:.0f}/{sz:.0f} (Ring {rmin:.0f}-{rmax:.0f} m, via {aid}).")
            return
        # Fallback: die Mod kennt x/z/count noch nicht (failed/ignoriert) ->
        # altes Verhalten (1 Stueck, 25 m voraus) ueber den Kanal des
        # naechsten Agenten, count-mal.
        ok = 0
        for _ in range(count):
            if self._stop_evt.is_set():
                break
            try:
                r2 = br.run("spawn_infected", timeout=30)
                if r2.get("status") == "done":
                    ok += 1
            except Exception as e:
                self.log(f"MISSION: spawn_infected-Fallback via {aid}: {e}")
                break
        self._spawned += ok
        self._last_wave = time.monotonic()
        self.log(f"MISSION: Welle per Fallback gespawnt - {ok}/{count} "
                 f"Infizierte 25 m vor {aid} (Mod ohne x/z/count).")

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        m = self.mission
        mid = m.get("id", "?")
        t0 = time.monotonic()
        self._last_wave = t0
        self.log(f"MISSION '{mid}': Engine laeuft (Lager "
                 f"{self.camp[0]:.0f}/{self.camp[1]:.0f}, "
                 f"Agenten: {', '.join(self.agent_ids) or '-'}).")

        events = sorted((m.get("events") or []),
                        key=lambda e: float(e.get("at_sec", 0.0)))
        for ev in events:
            due = t0 + float(ev.get("at_sec", 0.0))
            remaining = due - time.monotonic()
            if remaining > 0 and self._stop_evt.wait(remaining):
                return                       # Runde gestoppt - still beenden
            if self._stop_evt.is_set():
                return
            etype = (ev.get("type") or "").strip().lower()
            if etype == "broadcast":
                self._do_broadcast(ev)
            elif etype == "spawn_infected":
                self._do_spawn_infected(ev)
            else:
                self.log(f"MISSION '{mid}': unbekannter Event-Typ '{etype}' - "
                         f"ignoriert.")

        suc = m.get("success") or {}
        if suc.get("type") != "all_infected_dead":
            return                            # nichts weiter zu pruefen

        timeout_s = float(suc.get("timeout_sec", 900.0))
        radius = float(suc.get("radius_m", 200.0))
        clear_need = float(suc.get("clear_sec", 60.0))
        # Anlaufzeit nach der letzten Welle: die Infizierten muessen erst in
        # Sichtweite (nearby = 100 m um den Agenten) kommen, sonst meldet der
        # erste Poll faelschlich "alles tot".
        grace = 90.0
        clear_since: float | None = None
        seen_any = self._spawned > 0
        while not self._stop_evt.wait(5.0):
            now = time.monotonic()
            if now - t0 >= timeout_s:
                self._finish(success=False, reason="Zeitlimit erreicht")
                return
            n = self._infected_near_camp(radius)
            if n > 0:
                seen_any = True
                clear_since = None
                continue
            if now - self._last_wave < grace:
                continue                      # Welle noch im Anmarsch
            if clear_since is None:
                clear_since = now
                continue
            # Wurde nie ein Infizierter gesehen (Spawn evtl. fehlgeschlagen),
            # laenger warten, bevor wir Erfolg melden.
            need = clear_need if seen_any else max(clear_need, 180.0)
            if now - clear_since >= need:
                self._finish(success=True, reason="keine Infizierten mehr am Lager")
                return

    def _finish(self, success: bool, reason: str) -> None:
        m = self.mission
        mid = m.get("id", "?")
        text = m.get("on_success_broadcast") if success else m.get("on_fail_broadcast")
        if text:
            broadcast(self.agent_ids, text, prio=True)
        verdict = "ERFOLG" if success else "FEHLSCHLAG/ZEITLIMIT"
        self.log(f"MISSION '{mid}' beendet: {verdict} ({reason}, "
                 f"{self._spawned} Infizierte gespawnt).")
        if self.status:
            try:
                self.status(f"MISSION {mid}: {verdict} - {reason}")
            except Exception:
                pass
