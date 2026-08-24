"""IsuSurvivor File-Bridge — gemeinsamer Client fuer Testtreiber und MCP-Server.

Spricht mit der IsuSurvivor-Servermod ueber zwei JSON-Dateien im Profilordner
(Mailbox-Protokoll, siehe docs/protocol.md).
"""

import json
import math
import os
import threading
import time
import uuid

_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
DEFAULT_PROFILE = os.path.join(_SERVER_DIR, "profiles")


def _inbox_should_interrupt(path: str, base_size: int) -> bool:
    """True, wenn seit base_size eine Inbox-Zeile dazukam, die einen laufenden
    Marsch/Aktion unterbrechen soll: Funk vom Spieler oder einem anderen Absender
    (user != 'Lagezentrum') ODER ein als prio markierter Lagezentrum-Funk
    (Tod/kritische HP). Routine-Squad-Sitreps des Orchestrators (user=Lagezentrum,
    prio=False) unterbrechen NICHT - sie werden beim naechsten Aufwachen ohnehin
    ueber die normale Inbox gelesen; so brechen wir nicht jeden Marsch fuer eine
    Positionsmeldung ab. Bei jedem Fehler konservativ True (= altes Verhalten:
    jeder neue Funk unterbricht), damit der Spieler nie ueberhoert wird."""
    try:
        cur = os.path.getsize(path)
    except OSError:
        return False
    if cur <= base_size:
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(base_size)
            chunk = f.read()
    except OSError:
        return True
    saw_line = False
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        saw_line = True
        try:
            e = json.loads(line)
        except Exception:
            return True   # unparsbar -> sicherheitshalber unterbrechen
        if (e.get("user") or "").strip().lower() != "lagezentrum":
            return True   # Spieler/anderer Absender -> unterbrechen
        if e.get("prio"):
            return True   # kritischer Lagezentrum-Funk -> unterbrechen
    if not saw_line:
        return True       # gewachsen, aber keine lesbare Zeile -> altes Verhalten
    return False          # nur Routine-Lagezentrum-Sitreps -> NICHT unterbrechen


class Bridge:
    def __init__(self, profile_dir: str = DEFAULT_PROFILE, npc_id: str = "viktor"):
        self.npc_id = npc_id
        self.dir = os.path.join(profile_dir, "IsuSurvivor")
        self.state_file = os.path.join(self.dir, f"state_{npc_id}.json")
        self.cmd_file = os.path.join(self.dir, f"commands_{npc_id}.json")
        # Pfad zur Funk-Inbox; gesetzt von dayz_mcp. Lange Aktionen brechen ab,
        # sobald hier eine neue Zeile auftaucht, damit der Agent sofort auf den
        # Spieler reagiert statt minutenlang weiterzumarschieren/-kaempfen.
        self.voice_inbox = None
        # Reise-Thread (_travel_worker) und Tool-Thread teilen sich DIESELBE
        # Bridge: ohne Lock koennen beide gleichzeitig "Mailbox frei" sehen und
        # sich das commands.json ueberschreiben - der Verlierer wartet dann auf
        # eine cmd_id, die nie ankommt (volles Timeout, leere Fehlermeldung).
        self._send_lock = threading.Lock()

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
        """Zwei Snapshots vergleichen: laeuft die Bridge (steigt seq)?"""
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
        # Zusatzfelder fuer neuere Kommandos (z.B. find_water max_dist,
        # spawn_infected count, treat_other target/item) unveraendert
        # durchreichen. Bestehende Aufrufe uebergeben kein **extra -> der
        # ausgehende Befehl bleibt fuer sie exakt gleich. Aeltere Mod-Builds,
        # die ein Feld nicht kennen, ignorieren es beim Deserialisieren still.
        for k, v in extra.items():
            if k not in cmd:
                cmd[k] = v
        payload = {"commands": [cmd]}

        # Lock ueber Warten+Schreiben: das "Mailbox ist frei"-Fenster darf nur
        # EIN Thread gleichzeitig beanspruchen (Reise- vs. Tool-Thread).
        with self._send_lock:
            deadline = time.monotonic() + timeout
            while os.path.exists(self.cmd_file):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "commands.json wird nicht konsumiert - laeuft der Server mit "
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
                    stop_event=None) -> dict:
        """Pollen bis state.command.id == cmd_id und status done/failed.

        Bei Timeout wird der LETZTE bekannte command-Block zurueckgegeben
        (status bleibt dann "running") statt eine Exception zu werfen.

        interrupt_inbox: Pfad zur Funk-Inbox. Waechst die Datei waehrend des
        Wartens (neuer Funk vom Spieler), wird mit status "interrupted"
        abgebrochen, damit der Agent sofort zuhoeren statt weitermarschieren kann.

        stop_event: threading.Event - gesetzt = sofort mit "interrupted"
        aussteigen (der Reise-Thread haengt sonst bis zu 75 s in einem
        laufenden Segment, waehrend das naechste Tool schon die Beine will).
        """
        deadline = time.monotonic() + timeout
        base_size = -1
        if interrupt_inbox:
            try:
                base_size = os.path.getsize(interrupt_inbox)
            except OSError:
                base_size = -1
        last_cmd: dict = {}
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return {"id": cmd_id, "status": "interrupted",
                        "detail": "abgebrochen (stop_event)"}
            state = self.read_state()
            cmd = (state or {}).get("command", {})
            if cmd.get("id") == cmd_id:
                last_cmd = cmd
                status = cmd.get("status", "")
                if status == "running" and on_progress:
                    on_progress(cmd)
                if status in ("done", "failed"):
                    return cmd
            if base_size >= 0 and _inbox_should_interrupt(interrupt_inbox, base_size):
                return {"id": cmd_id, "status": "interrupted",
                        "detail": "neuer Funk"}
            time.sleep(1.0)
        return last_cmd

    def run(self, action: str, timeout: float = 700.0,
            interruptible: bool = False, stop_event=None, **kwargs) -> dict:
        """Befehl senden und auf Endstatus warten (oder Timeout -> running).

        interruptible=True: lange Aktion (move_to, engage, loot, ...) bricht ab,
        sobald neuer Funk in der Inbox liegt - der Agent reagiert dann sofort.
        stop_event: bricht das Warten ab, sobald das Event gesetzt ist
        (Reise-Thread-Abbruch via _abort_travel).
        """
        cmd_id = self.send(action, **kwargs)
        inbox = self.voice_inbox if interruptible else None
        return self.wait_status(cmd_id, timeout=timeout, interrupt_inbox=inbox,
                                stop_event=stop_event)


# --------------------------------------------------------- Beobachtungstext

def _compass(dx: float, dz: float) -> str:
    """Himmelsrichtung aus Delta (DayZ: x = Ost, z = Nord)."""
    angle = math.degrees(math.atan2(dx, dz)) % 360
    dirs = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
    return dirs[int((angle + 22.5) % 360 // 45)]


def _vitals_label(value: float, low: float, mid: float) -> str:
    if value < low:
        return "KRITISCH"
    if value < mid:
        return "niedrig"
    return "ok"


def _heat_label(heat: float) -> str:
    """Waerme-Comfort -1..+1 in Klartext (Quelle: GetStatHeatComfort)."""
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
    """Kompakte Kennung des Inventars (Classnames + Ladung) - aendert sie sich
    nicht, kann observe das Inventar als Einzeiler ausgeben (Token sparen)."""
    if not state:
        return ""
    parts = []
    for it in state.get("inventory", []):
        # parent + in_hands mit aufnehmen: ein in die Waffe gestecktes Magazin
        # oder ein Handwechsel aendert sich sonst nicht in der Kennung und der
        # "steckt in"-Hinweis (Fix 3) wuerde vom Delta-observe verschluckt
        parent = it.get("parent") or ""
        hand = "1" if it.get("in_hands") else "0"
        parts.append(f"{it.get('classname')}:{it.get('quantity', 0):.0f}:{parent}:{hand}")
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
    vorliegt. Zeigt die Erreger, die die Mod ueber Schwelle eingetragen hat."""
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


def format_observation(state: dict | None, last_chat_id: int = 0,
                       inv_unchanged: bool = False,
                       compact: bool = False) -> tuple[str, int]:
    """Kompakte deutschsprachige Lagebeschreibung fuer das LLM.

    Gibt (text, hoechste_gesehene_chat_id) zurueck. inv_unchanged kuerzt das
    Inventar auf eine Zeile (wenn sich seit dem letzten observe nichts aenderte),
    compact begrenzt die Umgebung auf die 8 wichtigsten Eintraege.
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
    # aeltere Builds schreiben sie nicht -> nichts anzeigen, kein Rauschen).
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
                     f"{clothing_count} Kleidung - observe(full=true) fuer Details)")
    else:
        lines.append(f"INVENTAR ({len(inventory)} Items, davon {clothing_count} Kleidung):")
        # Wichtiges darf beim 15er-Cap nicht hinter Nahrung/Kram wegfallen:
        # Waffen/Munition/Medizin zuerst (stabile Sortierung, Rest wie geliefert).
        _kind_rank = {"firearm": 0, "magazine": 1, "ammo": 1, "medical": 2}
        interesting = sorted(
            interesting,
            key=lambda i: _kind_rank.get(str(i.get("kind", "")).lower(), 5))
        if len(interesting) > 15:
            lines.append(f"  (gekuerzt: {len(interesting) - 15} weitere Items, "
                         f"observe(full=true) zeigt alles)")
        for it in interesting[:15]:
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
            else:
                lines.append(f"  - {it.get('classname')} [{it.get('kind')}]"
                             f" x{qty:.0f}{hand_marker}{stuck}")
        if not interesting:
            lines.append("  - (nur Kleidung)")
        # Kleidung als Einzeiler: getragen vs. lose im Gepaeck. Details holt
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

    # Nach Distanz sortieren, BEVOR auf limit gekuerzt wird: der Mod fuellt
    # nearby in Engine-Abfragereihenfolge (unsortiert) und cappt bei 40. Ohne
    # Sortierung belegen am vollen Lager die Squad-Bots/Zelt/Feuer/Leichen die
    # ersten Plaetze, und ein frisch vor die Fuesse gelegtes Item faellt hinter
    # den Schnitt -> der NPC "sieht" es nicht. Das Naechste zuerst zeigen.
    nearby = sorted(state.get("nearby", []),
                    key=lambda e: e.get("distance", 9999.0))
    limit = 8 if compact else 15
    # GEFAHREN duerfen nie hinter den Schnitt fallen: am vollen Lager belegen
    # Zelt/Feuer/Squad/Items die 8 compact-Plaetze, und der Infizierte auf
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

    chat = state.get("chat", [])
    new_chat = [m for m in chat if m.get("id", 0) > last_chat_id]
    max_id = last_chat_id
    if chat:
        max_id = max(m.get("id", 0) for m in chat)
    if new_chat:
        lines.append("CHAT (NEU):")
        for m in new_chat[-5:]:
            lines.append(f"  [{m.get('sender')}] {m.get('text')}")

    cmd = state.get("command", {})
    if cmd.get("action"):
        detail = cmd.get("detail") or ""
        lines.append(f"LETZTE AKTION: {cmd.get('action')} -> {cmd.get('status')} {detail}".rstrip())

    return ("\n".join(lines), max_id)
