"""IsuSurvivor File-Bridge — gemeinsamer Client für Testtreiber und MCP-Server.

Spricht mit der IsuSurvivor-Servermod über zwei JSON-Dateien im Profilordner
(Mailbox-Protokoll, siehe docs/protocol.md).
"""

import json
import math
import os
import re
import threading
import time
import uuid

_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
DEFAULT_PROFILE = os.path.join(_SERVER_DIR, "profiles")

# Projektordner (Elternordner von daemon/) - dort liegt arena/squad_state.json,
# das Lagebild, das der Orchestrator alle 3 s atomar schreibt (C2).
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQUAD_STATE_FILE = os.path.join(REPO_DIR, "arena", "squad_state.json")

# Tod-Sperre (Schnittstelle C1): run_agent schreibt beim Erkennen des Todes
# SOFORT agent_home/death_epoch.txt. Ein MCP-Prozess, der VOR dieser Epoche
# gestartet ist, gehört zum alten Gehirn und darf den neuen Körper nicht mehr
# steuern (Birgit 24.10.: 77 s Fremdsteuerung, Restore sabotiert).
DEATH_TEXT = ("DU BIST GESTORBEN - dieses Gehirn ist beendet. Keine Aktionen "
              "mehr, beende den Zug sofort.")

# Einleitung, die wait_status vor den Funktext einer unterbrechenden Inbox-
# Zeile setzt (interrupt_reason zerlegt sie wieder).
_FUNK_PREFIX = "neuer Funk"
# Zusatz im Verdrängungs-Detail, wenn der neue Befehl vom Befehlsrad des
# Spielers kommt (Mod-ids "player_..."): der Reise-Thread gibt dann auf,
# statt den Spielerbefehl seinerseits zu verdrängen.
PLAYER_ORDER_MARK = "(Befehlsrad des Spielers)"


def _inbox_interrupt_line(path: str, base_size: int) -> str:
    """Text der ersten Inbox-Zeile seit base_size, die einen laufenden
    Marsch/Aktion unterbrechen soll, sonst "". Unterbrechend ist Funk vom
    Spieler oder einem anderen Absender (user != 'Lagezentrum') - Rückgabe ist
    dann der gesprochene Text - ODER ein als prio markierter Lagezentrum-Funk
    (Tod/kritische HP), Rückgabe "Lagezentrum: <Text>". Routine-Squad-Sitreps
    des Orchestrators (user=Lagezentrum, prio=False) unterbrechen NICHT - sie
    werden beim nächsten Aufwachen ohnehin über die normale Inbox gelesen; so
    brechen wir nicht jeden Marsch für eine Positionsmeldung ab. Bei jedem
    Fehler konservativ "?" (= unterbrechen, Inhalt unbekannt), damit der
    Spieler nie überhört wird.

    VOICE-1 (24.10.): Früher lieferte die Unterbrechung keinen Inhalt - die
    NPCs rätselten "kein neuer Funk sichtbar, Fehlalarm" und machten weiter.
    Jetzt reicht wait_status den Text als detail "neuer Funk: <Text>" durch."""
    try:
        cur = os.path.getsize(path)
    except OSError:
        return ""
    if cur <= base_size:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(base_size)
            chunk = f.read()
    except OSError:
        return "?"
    saw_line = False
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        saw_line = True
        try:
            e = json.loads(line)
        except Exception:
            return "?"    # unparsbar -> sicherheitshalber unterbrechen
        if not isinstance(e, dict):
            return "?"
        text = " ".join(str(e.get("text") or "").split())
        if len(text) > 200:
            text = text[:197] + "..."
        if (e.get("user") or "").strip().lower() != "lagezentrum":
            return text or "?"   # Spieler/anderer Absender -> unterbrechen
        if e.get("prio"):
            # kritischer Lagezentrum-Funk -> unterbrechen
            return ("Lagezentrum: " + text) if text else "?"
    if not saw_line:
        return "?"        # gewachsen, aber keine lesbare Zeile -> altes Verhalten
    return ""             # nur Routine-Lagezentrum-Sitreps -> NICHT unterbrechen


def _inbox_should_interrupt(path: str, base_size: int) -> bool:
    """True, wenn seit base_size eine unterbrechende Inbox-Zeile dazukam
    (Regeln siehe _inbox_interrupt_line). Bleibt bewusst ein echtes bool -
    Aufrufer und Tests prüfen auf `is True`/`is False`; den Funktext liefert
    _inbox_interrupt_line."""
    return bool(_inbox_interrupt_line(path, base_size))


def _flag_mtime(path) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def _urgent_event(path: str, base_mtime: float) -> str:
    """Text des kritischen Ereignisses, wenn urgent_event.json seit base_mtime
    neu geschrieben wurde - sonst ''. run_agent schreibt die Datei atomar
    (JSON {"event": ..., "t": ...}) bei DU BLUTEST / GEFAHR / UMGEKIPPT /
    GESTORBEN. Ein Zeitstempel VOR dem Aktionsstart zählt nicht: dieses
    Ereignis ist dann schon auf dem Weg zum Gehirn."""
    if not path:
        return ""
    if _flag_mtime(path) <= base_mtime:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return str(data.get("event") or "kritisches Ereignis")
    except (OSError, ValueError, AttributeError):
        return "kritisches Ereignis"


def interrupt_reason(result: dict) -> str:
    """Grund einer 'interrupted'-Antwort in Klartext (ohne ABGEBROCHEN-Prefix).

    Zwei Auslöser: neuer Spieler-Funk (Inbox) oder ein kritisches Welt-
    Ereignis (urgent_event.json: Blutung, Gefahr, Bewusstlosigkeit). Beim
    zweiten soll der NPC ZUERST die Gefahr behandeln, nicht 'zuhören' -
    früher las jede Unterbrechung als Spieler-Funk."""
    detail = str(result.get("detail") or "")
    if detail.startswith("kritisch:"):
        ev = detail[len("kritisch:"):].strip()
        return (f"kritisches Ereignis: {ev} Reagiere ZUERST darauf "
                f"(bandage/flee/engage, bei Bedarf observe), dann weiter.")
    if detail.startswith("verdraengt durch"):
        # BRIDGE-1: Die Mod führt pro NPC nur EINEN Befehl; ein neuerer hat
        # diesen ersetzt. Das ist weder Funk noch Gefahr - der alte Befehl
        # wird nie mehr fertig, also klar sagen, dass er NICHT weiterläuft.
        by = detail[len("verdraengt durch"):].strip() or "neuer Befehl"
        if PLAYER_ORDER_MARK in by:
            # Befehlsrad: eine ausdrückliche Anweisung des Spielers. Der
            # generische "starte ihn bei Bedarf neu"-Text verleitete das
            # Gehirn, den Spielerbefehl sofort wieder zu überschreiben.
            order = by.replace(PLAYER_ORDER_MARK, "").strip() or "neuer Befehl"
            return (f"Der Spieler hat dir per Befehlsrad einen neuen Befehl "
                    f"gegeben ({order}) - der hat Vorrang, dein alter Befehl "
                    f"läuft NICHT weiter. Starte ihn NICHT neu, außer der "
                    f"Spieler bittet darum.")
        return (f"Dein Befehl wurde vom Server durch einen neueren ersetzt "
                f"({by}) und läuft NICHT weiter. Prüfe mit observe und starte "
                f"ihn bei Bedarf neu.")
    if detail.startswith(_FUNK_PREFIX + ":"):
        said = detail[len(_FUNK_PREFIX) + 1:].strip()
        if said and said != "?":
            if said.lower().startswith("lagezentrum:"):
                said = said.split(":", 1)[1].strip()
                return (f"Das Lagezentrum funkt dringend: \"{said}\" - reagiere "
                        f"jetzt darauf (kommt gleich noch als EREIGNIS, dann "
                        f"nicht doppelt antworten).")
            return (f"Der Spieler funkt: \"{said}\" - reagiere jetzt darauf "
                    f"(kommt gleich noch als EREIGNIS, dann nicht doppelt "
                    f"antworten).")
    return ("Der Spieler funkt dich gerade an. Hör SOFORT zu und reagiere "
            "auf seinen Funk, bevor du weitermachst.")


def interrupt_text(result: dict) -> str:
    return "ABGEBROCHEN: " + interrupt_reason(result)


class Bridge:
    def __init__(self, profile_dir: str = DEFAULT_PROFILE, npc_id: str = "viktor"):
        self.npc_id = npc_id
        self.dir = os.path.join(profile_dir, "IsuSurvivor")
        self.state_file = os.path.join(self.dir, f"state_{npc_id}.json")
        self.cmd_file = os.path.join(self.dir, f"commands_{npc_id}.json")
        # Pfad zur Funk-Inbox; gesetzt von dayz_mcp. Lange Aktionen brechen ab,
        # sobald hier eine neue Zeile auftaucht, damit der Agent sofort auf den
        # Spieler reagiert statt minutenlang weiterzumarschieren/-kämpfen.
        self.voice_inbox = None
        # Reise-Thread (_travel_worker) und Tool-Thread teilen sich DIESELBE
        # Bridge: ohne Lock können beide gleichzeitig "Mailbox frei" sehen und
        # sich das commands.json überschreiben - der Verlierer wartet dann auf
        # eine cmd_id, die nie ankommt (volles Timeout, leere Fehlermeldung).
        self._send_lock = threading.Lock()
        # Kritische Welt-Ereignisse (run_agent: Blutung, Gefahr, Bewusstlos)
        # brechen laufende Langaktionen ab wie neuer Spieler-Funk - Pfad zur
        # urgent_event.json im agent_home, gesetzt von dayz_mcp.
        self.urgent_flag = None
        # Tod-Sperre (C1): Pfad zu agent_home/death_epoch.txt und Startzeit
        # dieses Gehirns, beides gesetzt von dayz_mcp. Der Runner-eigene
        # Bridge bleibt ohne Datei (None) und ist damit nie gesperrt.
        self.death_epoch_file = None
        self.session_start = 0.0

    def died_since_start(self) -> bool:
        """True, wenn run_agent NACH dem Start dieses Gehirns einen Tod
        gemeldet hat (death_epoch.txt jünger als session_start). Fehlt die
        Datei oder ist sie unlesbar, gilt das Gehirn als frei."""
        if not self.death_epoch_file:
            return False
        ep = 0.0
        try:
            with open(self.death_epoch_file, "r", encoding="utf-8") as f:
                ep = float(f.read().strip() or 0)
        except (OSError, ValueError):
            ep = 0.0
        return ep > self.session_start

    # ------------------------------------------------------------- state I/O

    def read_state(self, retries: int = 5) -> dict | None:
        for _ in range(retries):
            try:
                with open(self.state_file, "r", encoding="utf-8", errors="replace") as f:
                    return json.load(f)
            except FileNotFoundError:
                return None
            except json.JSONDecodeError:
                time.sleep(0.2)
        return None

    def state_fresh(self) -> dict | None:
        """Zwei Snapshots vergleichen: läuft die Bridge (steigt seq)?"""
        s1 = self.read_state()
        if s1 is None:
            return None
        time.sleep(1.5)
        s2 = self.read_state()
        if s2 is None or s2.get("seq", 0) <= s1.get("seq", 0):
            return None
        return s2

    # ----------------------------------------------------------- command I/O

    def send(self, action: str, x: float = 0.0, y: float = 0.0, z: float = 0.0,
             loadout: str = "", text: str = "", faction: str = "", br: int = 0,
             timeout: float = 10.0, **extra) -> str:
        # Tod-Sperre (C1): das alte Gehirn darf dem respawnten Körper keine
        # Befehle mehr schicken (sonst verdrängt es Restore-give_items).
        if self.died_since_start():
            raise RuntimeError(DEATH_TEXT)
        cmd_id = uuid.uuid4().hex[:12]
        cmd = {
            "id": cmd_id,
            "action": action,
            "x": float(x),
            "y": float(y),
            "z": float(z),
            "loadout": loadout,
            "text": text,
            "faction": faction,
            "br": str(br),
        }
        # Zusatzfelder für neuere Kommandos (z.B. find_water max_dist,
        # spawn_infected count, treat_other target/item) unverändert
        # durchreichen. Bestehende Aufrufe übergeben kein **extra -> der
        # ausgehende Befehl bleibt für sie exakt gleich. Ältere Mod-Builds,
        # die ein Feld nicht kennen, ignorieren es beim Deserialisieren still.
        for k, v in extra.items():
            if k not in cmd:
                cmd[k] = v
        payload = {"commands": [cmd]}

        # Lock über Warten+Schreiben: das "Mailbox ist frei"-Fenster darf nur
        # EIN Thread gleichzeitig beanspruchen (Reise- vs. Tool-Thread).
        with self._send_lock:
            deadline = time.monotonic() + timeout
            while os.path.exists(self.cmd_file):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "commands.json wird nicht konsumiert - läuft der Server mit "
                        "-servermod=@IsuSurvivor?"
                    )
                time.sleep(0.3)

            os.makedirs(self.dir, exist_ok=True)
            tmp = self.cmd_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.cmd_file)
            return cmd_id

    def wait_status(self, cmd_id: str, timeout: float = 700.0,
                    on_progress=None, interrupt_inbox=None,
                    stop_event=None, urgent_flag=None) -> dict:
        """Pollen bis state.command.id == cmd_id und status done/failed.

        Bei Timeout wird der LETZTE bekannte command-Block zurückgegeben
        (status bleibt dann "running") statt eine Exception zu werfen.

        interrupt_inbox: Pfad zur Funk-Inbox. Wächst die Datei während des
        Wartens (neuer Funk vom Spieler), wird mit status "interrupted"
        abgebrochen, damit der Agent sofort zuhören statt weitermarschieren kann.

        stop_event: threading.Event - gesetzt = sofort mit "interrupted"
        aussteigen (der Reise-Thread hängt sonst bis zu 75 s in einem
        laufenden Segment, während das nächste Tool schon die Beine will).

        urgent_flag: Pfad zur urgent_event.json. Wird sie WÄHREND des
        Wartens neu geschrieben (kritisches Ereignis vom run_agent-Watcher),
        endet das Warten mit status "interrupted" und detail "kritisch: ...".
        Viktor 08.09.: DU-BLUTEST um 20:54:09, der laufende explore_step
        blockierte das Gehirn aber bis 20:55:06 - erst dann kam bandage().

        Neben-Befehle (C6): say/intent/ping führt die Mod NICHT über den
        Hauptslot aus, wenn dort gerade ein anderer Befehl läuft, sondern
        meldet sie unter state["side_command"] - die eigene cmd_id wird
        auch dort akzeptiert.

        Verdrängung (BRIDGE-1): Die Mod hat pro NPC nur EINEN Befehlsslot;
        jeder neue Befehl (anderer Thread, Runner, altes Gehirn) ersetzt den
        laufenden, der dann nie mehr done/failed wird. War die eigene cmd_id
        schon im Slot und steht dort jetzt eine andere, endet das Warten
        sofort mit status "interrupted", detail "verdraengt durch <action>",
        statt bis zum vollen Timeout zu blockieren.
        """
        deadline = time.monotonic() + timeout
        urgent_base = _flag_mtime(urgent_flag) if urgent_flag else 0.0
        base_size = -1
        if interrupt_inbox:
            try:
                base_size = os.path.getsize(interrupt_inbox)
            except OSError:
                base_size = -1
        last_cmd: dict = {}
        seen_own = False
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return {"id": cmd_id, "status": "interrupted",
                        "detail": "abgebrochen (stop_event)"}
            if self.died_since_start():
                # Tod-Sperre (C1): ein vor dem Tod gestarteter Befehl darf das
                # alte Gehirn nicht weiter blockieren.
                return {"id": cmd_id, "status": "failed", "detail": DEATH_TEXT}
            if urgent_flag:
                urgent = _urgent_event(urgent_flag, urgent_base)
                if urgent:
                    return {"id": cmd_id, "status": "interrupted",
                            "detail": "kritisch: " + urgent}
            state = self.read_state() or {}
            cmd = state.get("command") or {}
            side = state.get("side_command") or {}
            if not isinstance(cmd, dict):
                cmd = {}
            if not isinstance(side, dict):
                side = {}
            if side.get("id") == cmd_id:
                last_cmd = side
                if side.get("status", "") in ("done", "failed"):
                    return side
            elif cmd.get("id") == cmd_id:
                seen_own = True
                last_cmd = cmd
                status = cmd.get("status", "")
                if status == "running" and on_progress:
                    on_progress(cmd)
                if status in ("done", "failed"):
                    return cmd
            elif seen_own and cmd.get("id"):
                by = str(cmd.get("action") or "neuen Befehl")
                if str(cmd.get("id")).startswith("player_"):
                    # Befehlsrad des Spielers (Mod-ids player_goto/_halt/...)
                    by += " " + PLAYER_ORDER_MARK
                return {"id": cmd_id, "status": "interrupted",
                        "detail": "verdraengt durch " + by}
            if base_size >= 0:
                said = _inbox_interrupt_line(interrupt_inbox, base_size)
                if said:
                    detail = _FUNK_PREFIX
                    if said != "?":
                        detail += ": " + said
                    return {"id": cmd_id, "status": "interrupted",
                            "detail": detail}
            time.sleep(1.0)
        return last_cmd

    def run(self, action: str, timeout: float = 700.0,
            interruptible: bool = False, stop_event=None, **kwargs) -> dict:
        """Befehl senden und auf Endstatus warten (oder Timeout -> running).

        interruptible=True: lange Aktion (move_to, engage, loot, ...) bricht ab,
        sobald neuer Funk in der Inbox liegt ODER der Watcher ein kritisches
        Ereignis meldet (urgent_flag) - der Agent reagiert dann sofort.
        stop_event: bricht das Warten ab, sobald das Event gesetzt ist
        (Reise-Thread-Abbruch via _abort_travel).
        """
        cmd_id = self.send(action, **kwargs)
        inbox = self.voice_inbox if interruptible else None
        urgent = getattr(self, "urgent_flag", None) if interruptible else None
        return self.wait_status(cmd_id, timeout=timeout, interrupt_inbox=inbox,
                                stop_event=stop_event, urgent_flag=urgent)


# --------------------------------------------------------- Beobachtungstext

def _compass(dx: float, dz: float) -> str:
    """Himmelsrichtung aus Delta (DayZ: x = Ost, z = Nord)."""
    angle = math.degrees(math.atan2(dx, dz)) % 360
    dirs = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
    return dirs[int((angle + 22.5) % 360 // 45)]


def squad_lines(own_id: str, px: float, pz: float, max_age: float = 15.0,
                details: bool = False) -> str:
    """Live-Lage der Kameraden aus arena/squad_state.json (Schnittstelle C2).

    "" wenn die Datei fehlt/unlesbar ist oder älter als max_age Sekunden
    (Orchestrator AUS = Benchmark-Modus, BR/hostile, Reste alter Sessions).
    Sonst je anderer Agent: "<Name> nicht im Spiel", "<Name> TOT" oder
    "<Name> <Distanz> m <Kompass> (x<X> z<Z>, HP<h>[, im KAMPF])"; die
    Lebenden nach Distanz zur eigenen Position (px/pz) sortiert, alles mit
    "; " verbunden. RESP-1 (24.10.): Birgit lief 700 m zu einer Minuten alten
    Sitrep-Koordinate zurück - observe zeigt nur 100 m, Kameraden außerhalb
    tauchten nirgends live auf.
    details=True (Lagezentrum-Neuaufbau im Runner): zusätzlich Bewegungs-
    richtung und Absicht mit Alter, wie sie der Orchestrator in den Sitrep
    schreibt ("will: ..." bis 60 s, "dachte vor N s: ..." bis 180 s). Ohne
    das gingen sie beim Neuaufbau verloren (Review W3, Lücke C2/C9)."""
    try:
        with open(SQUAD_STATE_FILE, "r", encoding="utf-8", errors="replace") as f:
            payload = json.load(f)
        file_age = time.time() - float(payload["t"])
        if file_age > max_age:
            return ""
        agents = payload.get("agents") or []
        px = float(px)
        pz = float(pz)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    own = (own_id or "").strip().lower()
    alive: list[tuple[float, str]] = []
    dead: list[str] = []
    absent: list[str] = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "")
        if aid.strip().lower() == own:
            continue
        name = str(a.get("name") or aid.capitalize() or "?")
        if not a.get("online") or not a.get("spawned"):
            absent.append(f"{name} nicht im Spiel")
            continue
        if not a.get("alive"):
            dead.append(f"{name} TOT")
            continue
        try:
            ax = float(a.get("x", 0.0))
            az = float(a.get("z", 0.0))
            hp = float(a.get("health") or 0.0)
        except (TypeError, ValueError):
            continue
        dx = ax - px
        dz = az - pz
        d = math.hypot(dx, dz)
        fight = ", im KAMPF" if a.get("fighting") else ""
        line = (f"{name} {d:.0f} m {_compass(dx, dz)} "
                f"(x{ax:.0f} z{az:.0f}, HP{hp:.0f}{fight})")
        if details:
            if a.get("heading"):
                line += f", unterwegs Richtung {a['heading']}"
            intent = str(a.get("intent") or "").strip()
            if intent:
                try:
                    iage = float(a.get("intent_age") or 0.0) + max(0.0, file_age)
                except (TypeError, ValueError):
                    iage = 0.0
                if iage <= 60:
                    line += f" (will: {intent})"
                elif iage <= 180:
                    line += f" (dachte vor {iage:.0f} s: {intent})"
        alive.append((d, line))
    alive.sort(key=lambda p: p[0])
    return "; ".join([s for _, s in alive] + dead + absent)


def _vitals_label(value: float, low: float, mid: float) -> str:
    if value < low:
        return "KRITISCH"
    if value < mid:
        return "niedrig"
    return "ok"


def _heat_label(heat: float) -> str:
    """Wärme-Comfort -1..+1 in Klartext (Quelle: GetStatHeatComfort)."""
    if heat < -0.5:
        return "FRIERT STARK - Kleidung anziehen (wear) oder Feuer machen"
    if heat < -0.3:
        return "friert"
    if heat < -0.15:
        return "etwas kuehl (harmlos)"
    if heat > 0.5:
        return "warm"
    return "ok"


def inventory_signature(state: dict | None) -> str:
    """Kompakte Kennung des Inventars (Classnames + Ladung) - ändert sie sich
    nicht, kann observe das Inventar als Einzeiler ausgeben (Token sparen)."""
    if not state:
        return ""
    parts = []
    for it in state.get("inventory", []):
        # parent + in_hands mit aufnehmen: ein in die Waffe gestecktes Magazin
        # oder ein Handwechsel ändert sich sonst nicht in der Kennung und der
        # "steckt in"-Hinweis (Fix 3) würde vom Delta-observe verschluckt
        parent = it.get("parent") or ""
        hand = "1" if it.get("in_hands") else "0"
        safety = (it.get("food_stage"), it.get("liquid_safe"), it.get("food_safe"), it.get("frozen"),
                  it.get("health"), it.get("worn"))
        parts.append(f"{it.get('classname')}:{it.get('quantity', 0):.0f}:{parent}:{hand}:{safety}")
    return "|".join(sorted(parts))


_SUN_LABEL = {"day": "Tag", "dawn": "Morgendaemmerung",
              "dusk": "Abenddaemmerung", "night": "Nacht"}


def _weather_line(world, wet) -> str:
    """Eine Wetterzeile aus state.world (+ npc.wet), oder "" wenn keine Felder
    da sind. Tolerant: fehlende Einzelwerte werden einfach weggelassen."""
    if not isinstance(world, dict):
        return ""
    parts: list[str] = []
    t = world.get("time")
    if t:
        parts.append(str(t))
    sun = world.get("sun")
    if sun:
        parts.append(_SUN_LABEL.get(str(sun), str(sun)))
    rain = world.get("rain")
    if isinstance(rain, (int, float)) and rain > 0.05:
        parts.append(f"Regen {rain * 100:.0f}%")
    fog = world.get("fog")
    if isinstance(fog, (int, float)) and fog > 0.15:
        parts.append(f"Nebel {fog * 100:.0f}%")
    if isinstance(wet, (int, float)) and wet > 0.3:
        parts.append("du bist durchnaesst")
    if not parts:
        return ""
    return "Wetter: " + ", ".join(parts)


def _symptom_line(disease) -> str:
    """Eine Symptomzeile aus npc.disease (Schnittstelle 3), oder "" wenn nichts
    vorliegt. Zeigt die Erreger, die die Mod über Schwelle eingetragen hat."""
    if not isinstance(disease, dict):
        return ""
    agents = disease.get("agents")
    present = []
    if isinstance(agents, dict):
        present = [k for k, v in agents.items()
                   if isinstance(v, (int, float)) and v > 0]
    if not present:
        if disease.get("sick"):
            return "Du fuehlst dich krank."
        return ""
    return "Du fuehlst dich krank: " + ", ".join(sorted(present))


def _chat_addressee(text: str, own_name: str, known_names) -> str:
    """Anrede-Zusatz für eine Chatzeile: " an dich", " an <Name> (nicht an
    dich)" oder "". Nur wenn das Wort vor dem ersten Komma ein BEKANNTER Name
    ist (Kameraden + Spieler-Aliase) - sonst würde "Gut, ich komme" zu "an
    Gut". RESP-1/VI-10 (24.10.): Birgit las Viktors "Player, ... komme zu
    dir, fünfunddreißig Meter" als Nachricht an sich und wartete auf ihn."""
    if not own_name or not known_names or "," not in (text or ""):
        return ""
    lead = text.split(",", 1)[0].strip()
    low = lead.lower()
    if not lead or low not in known_names:
        return ""
    own = own_name.strip().lower()
    if low == own:
        return " an dich"
    # Nennt die Zeile dich weiter hinten ("Player, Birgit, kommt her")?
    # Dann nicht behaupten, sie gelte dir nicht.
    words = {w.lower() for w in re.findall(r"[\wäöüÄÖÜß]+", text)}
    if own in words:
        return f" an {lead}"
    return f" an {lead} (nicht an dich)"


def format_observation(state: dict | None, last_chat_id: int = 0,
                       inv_unchanged: bool = False,
                       compact: bool = False, own_name: str = "",
                       known_names=None) -> tuple[str, int]:
    """Kompakte deutschsprachige Lagebeschreibung für das LLM.

    Gibt (text, höchste_gesehene_chat_id) zurück. inv_unchanged kürzt das
    Inventar auf eine Zeile (wenn sich seit dem letzten observe nichts änderte),
    compact begrenzt die Umgebung auf die 8 wichtigsten Einträge.
    own_name/known_names (optional, Kleinbuchstaben-Menge der Kameraden- und
    Spielernamen) machen in CHAT (NEU) sichtbar, an wen eine Zeile gerichtet ist.
    """
    if state is None:
        return ("FEHLER: Keine Verbindung zur Welt (state.json fehlt). "
                "Der Server ist vermutlich offline.", last_chat_id)

    npc = state.get("npc", {})
    lines: list[str] = []

    if not npc.get("spawned"):
        return ("Du hast keinen Koerper in der Welt (nicht gespawnt).", last_chat_id)

    if not npc.get("alive"):
        return ("DU BIST TOT. Warte auf Wiedergeburt.", last_chat_id)

    x = npc.get("pos_x", 0.0)
    z = npc.get("pos_z", 0.0)
    lines.append(f"DU: {npc.get('name') or 'Survivor'}")
    lines.append(f"POSITION: x={x:.0f} z={z:.0f} (Blick {npc.get('heading', 0):.0f} Grad)")
    if npc.get("following"):
        lines.append("STATUS: Du folgst gerade einem Spieler.")
    if npc.get("unconscious"):
        lines.append("!!! DU BIST BEWUSSTLOS - du liegst am Boden. Das vergeht "
                     "meist von selbst; danach mit unstick aufstehen. !!!")
    if npc.get("in_vehicle"):
        lines.append("STATUS: Du sitzt in einem FAHRZEUG. Bleib sitzen - "
                     "Bewegungs-/Kampfbefehle sind gesperrt. Aussteigen nur "
                     "bewusst mit vehicle_exit.")

    water = npc.get("water", 0.0)
    energy = npc.get("energy", 0.0)
    lines.append(
        f"VITALS: HP {npc.get('health', 0):.0f}/100 | Blut {npc.get('blood', 0):.0f}/5000"
        f" | Wasser {water:.0f} ({_vitals_label(water, 800, 2000)})"
        f" | Energie {energy:.0f} ({_vitals_label(energy, 800, 2500)})"
        f" | Magen {npc.get('stomach_volume', 0):.0f}"
        f" | Waerme: {_heat_label(npc.get('heat_comfort', 0.0))}"
    )

    # Wetter- und Symptomzeile NUR, wenn die Mod die Felder liefert (tolerant:
    # ältere Builds schreiben sie nicht -> nichts anzeigen, kein Rauschen).
    weather_line = _weather_line(state.get("world"), npc.get("wet"))
    if weather_line:
        lines.append(weather_line)
    symptom_line = _symptom_line(npc.get("disease"))
    if symptom_line:
        lines.append(symptom_line)

    if npc.get("fighting"):
        lines.append("!!! IM KAMPF !!!")

    hands = npc.get("in_hands") or "leer"
    lines.append(f"HAND: {hands}")

    inventory = state.get("inventory", [])
    interesting = [i for i in inventory if i.get("kind") != "clothing"]
    clothing_count = len(inventory) - len(interesting)
    if inv_unchanged:
        lines.append(f"INVENTAR: unveraendert ({len(inventory)} Items, davon "
                     f"{clothing_count} Kleidung - observe(full=true) für Details)")
    else:
        lines.append(f"INVENTAR ({len(inventory)} Items, davon {clothing_count} Kleidung):")
        # Wichtiges darf beim 15er-Cap nicht hinter Nahrung/Kram wegfallen:
        # Waffen/Munition/Medizin zuerst (stabile Sortierung, Rest wie geliefert).
        _kind_rank = {"firearm": 0, "magazine": 1, "ammo": 1, "medical": 2}
        interesting = sorted(
            interesting,
            key=lambda i: _kind_rank.get(str(i.get("kind", "")).lower(), 5))
        inventory_limit = 15 if compact else len(interesting)
        # MVS-M1: Ältere Mod-Builds melden Magazine/Munitionsstapel immer mit
        # quantity 1.0 (generische ItemBase-Quantity) - das Modell las "x1"
        # als "eine Patrone". Der neue Build liefert die echte Patronenzahl
        # (GetAmmoCount) und im selben Zug den Schusszähler npc.shots (C4);
        # nur dann "<n> Schuss" anzeigen, sonst bleibt es beim neutralen "xN".
        real_ammo = "shots" in npc
        if len(interesting) > inventory_limit:
            lines.append(f"  (gekuerzt: {len(interesting) - inventory_limit} weitere Items, "
                         f"observe(full=true) zeigt alles)")
        for it in interesting[:inventory_limit]:
            hand_marker = " [IN HAND]" if it.get("in_hands") else ""
            # Steckt das Item in einer Waffe? Dann kann man es nicht droppen
            parent = it.get("parent") or ""
            stuck = f" (steckt in {parent})" if parent else ""
            qty = it.get("quantity", 0)
            if it.get("kind") == "firearm":
                # quantity ist bei Waffen die echte Ladung (Magazin + Kammer)
                if qty > 0:
                    amount = f"geladen: {qty:.0f} Schuss"
                else:
                    amount = "UNGELADEN"
                lines.append(f"  - {it.get('classname')} [{it.get('kind')}]"
                             f" ({amount}){hand_marker}{stuck}")
            elif it.get("kind") == "ammo" and real_ammo:
                cn = str(it.get("classname") or "")
                where = " im Magazin" if cn.startswith("Mag_") else ""
                lines.append(f"  - {cn} [ammo] ({qty:.0f} Schuss{where})"
                             f"{hand_marker}{stuck}")
            else:
                details = []
                stage_names = {1: "roh", 2: "gebraten", 3: "gekocht", 4: "getrocknet",
                               5: "VERBRANNT", 6: "VERDORBEN"}
                if it.get("food_stage") in stage_names:
                    details.append(stage_names[it["food_stage"]])
                if it.get("frozen"):
                    details.append("GEFROREN")
                if it.get("kind") == "food" and it.get("food_safe") is False:
                    details.append("NICHT SICHER ESSBAR")
                if it.get("kind") == "drink" and "liquid_safe" in it:
                    details.append("trinkbar" if it["liquid_safe"] else "NICHT SICHER TRINKBAR")
                safety_text = " (" + ", ".join(details) + ")" if details else ""
                lines.append(f"  - {it.get('classname')} [{it.get('kind')}]"
                             f" x{qty:.0f}{safety_text}{hand_marker}{stuck}")
        if not interesting:
            lines.append("  - (nur Kleidung)")
        # Kleidung als Einzeiler: getragen vs. lose im Gepäck. Details holt
        # dress_best selbst aus dem State (warmth/cargo_size/slot der Mod).
        worn = [i for i in inventory
                if i.get("kind") == "clothing" and i.get("worn")]
        if worn:
            loose_cloth = clothing_count - len(worn)
            cloth_line = "  Getragen: " + ", ".join(
                i.get("classname", "?") for i in worn[:8])
            if loose_cloth > 0:
                cloth_line += (f" | {loose_cloth} Kleidungsstueck(e) lose im "
                               f"Gepaeck (dress_best optimiert)")
            lines.append(cloth_line)

    # Nach Distanz sortieren, BEVOR auf limit gekürzt wird: der Mod füllt
    # nearby in Engine-Abfragereihenfolge (unsortiert) und cappt bei 40. Ohne
    # Sortierung belegen am vollen Lager die Squad-Bots/Zelt/Feuer/Leichen die
    # ersten Plätze, und ein frisch vor die Füße gelegtes Item fällt hinter
    # den Schnitt -> der NPC "sieht" es nicht. Das Nächste zuerst zeigen.
    nearby = sorted(state.get("nearby", []),
                    key=lambda e: e.get("distance", 9999.0))
    limit = 8 if compact else len(nearby)
    # GEFAHREN dürfen nie hinter den Schnitt fallen: am vollen Lager belegen
    # Zelt/Feuer/Squad/Items die 8 compact-Plätze, und der Infizierte auf
    # 50 m war unsichtbar. Bedrohungen zuerst, dann der Rest nach Distanz.
    threats = [e for e in nearby
               if e.get("kind") in ("infected", "animal", "player")]
    rest = [e for e in nearby
            if e.get("kind") not in ("infected", "animal", "player")]
    nearby = threats + rest
    if nearby:
        lines.append("UMGEBUNG (100 m):")
        for e in nearby[:limit]:
            dx = e.get("x", 0.0) - x
            dz = e.get("z", 0.0) - z
            name = ""
            if e.get("name"):
                name = f" '{e.get('name')}'"
            cargo = ""
            if e.get("cargo", 0) > 0:
                cargo = f" [enthaelt {e.get('cargo')}]"
            extra = ""
            if e.get("kind") == "garden":
                extra += " [ERNTEREIF]" if e.get("harvestable") else " [waechst / abgeerntet]"
            if e.get("kind") == "item":
                ik = e.get("item_kind") or ""
                if ik:
                    extra += f" {{{ik}}}"
                if e.get("near"):
                    extra += f" (liegt bei {e.get('near')} - wohl dessen Ablage)"
            lines.append(f"  - {e.get('kind')}: {e.get('classname')}{name}"
                         f" {e.get('distance', 0):.0f}m {_compass(dx, dz)}{cargo}{extra}")
        if len(nearby) > limit:
            lines.append(f"  - ... und {len(nearby) - limit} weitere")
    else:
        lines.append("UMGEBUNG: nichts Auffaelliges in 100 m")

    chat = [m for m in (state.get("chat") or []) if isinstance(m, dict)]
    if chat and max(m.get("id", 0) for m in chat) < last_chat_id:
        # Server-Neustart: die Chat-ids der Mod beginnen wieder bei 1 - sonst
        # blieben die ersten neuen Zeilen unsichtbar.
        last_chat_id = 0
    new_chat = [m for m in chat if m.get("id", 0) > last_chat_id]
    max_id = last_chat_id
    if chat:
        max_id = max(m.get("id", 0) for m in chat)
    if new_chat:
        lines.append("CHAT (NEU):")
        # VI-10: Alter je Zeile aus der Server-uptime (Mod: IsuChatMsg.uptime
        # und state.uptime, beide GetTickTime) - eine 3 Minuten alte Ansage
        # sah sonst aus wie eine frische. Nie negativ (Server-Neustart).
        now_up = state.get("uptime")
        for m in new_chat[-5:]:
            text = str(m.get("text") or "")
            tag = str(m.get("sender") or "?")
            if not own_name or tag.strip().lower() != own_name.strip().lower():
                # eigene Zeilen nicht markieren - der NPC weiß, an wen er sprach
                tag += _chat_addressee(text, own_name, known_names)
            if now_up is not None and m.get("uptime") is not None:
                try:
                    age = max(0.0, float(now_up) - float(m.get("uptime")))
                    tag += f", vor {age:.0f} s"
                except (TypeError, ValueError):
                    pass
            lines.append(f"  [{tag}] {text}")

    cmd = state.get("command", {})
    if cmd.get("action"):
        detail = cmd.get("detail") or ""
        lines.append(f"LETZTE AKTION: {cmd.get('action')} -> {cmd.get('status')} {detail}".rstrip())

    return ("\n".join(lines), max_id)
