#!/usr/bin/env python3
"""IsuSurvivor Arena-Supervisor - startet/stoppt die Agenten auf Befehl aus
dem Spiel.

Das In-Game-Menue (IsuVoice, Taste Einfg) schickt seine Befehle per RPC
an den Server; die IsuSurvivor-Mod schreibt sie in arena_request.txt im
Server-Profil. Dieser Supervisor pollt die Datei, verwaltet die
run_agent-Prozesse und meldet den Zustand ueber arena_status.txt zurueck
(die Mod funkt ihn ins Menue).

Befehlsformat (eine Zeile, Pipe-getrennt). Pro Agent:
  <slot>:<an>:<modell>[:<persona>[:<name>]]
  start|viktor:1:sonnet:jaeger:Viktor|birgit:1:haiku:sanitaeter:Birgit|igor:0:opus:bauer:Igor|konrad:1:claude-fable-5:exmilitaer:Konrad|hostile:0|camp:4233.7,8512.2|idle:120|turns:10|mic:1|orch:0
  stop
orch:1 schaltet den Orchestrator (Schiedsrichter/Lagezentrum, daemon/orchestrator.py)
zu - AUS laufen die Agenten voellig unabhaengig (sauberer Modell-Benchmark).
Persona-Schluessel = Dateiname in daemon/characters/<key>.md
(jaeger, bauer, sanitaeter, exmilitaer, kampfmaschine). Fehlt persona/name,
gelten die Defaults aus arena/agents.json.

Start:  python daemon\\arena_supervisor.py   (oder tools\\start_supervisor.ps1)
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
PROFILE_DIR = os.path.join(_SERVER_DIR, "profiles", "IsuSurvivor")
REQUEST_FILE = os.path.join(PROFILE_DIR, "arena_request.txt")
STATUS_FILE = os.path.join(PROFILE_DIR, "arena_status.txt")
ROSTER_FILE = os.path.join(REPO_DIR, "arena", "agents.json")
ACTIVE_ROSTER_FILE = os.path.join(REPO_DIR, "arena", "active_roster.json")
CHARACTERS_DIR = os.path.join(REPO_DIR, "daemon", "characters")
DISCORD_FILE = os.path.join(REPO_DIR, "arena", "discord.json")
ACTIVE_MAP_FILE = os.path.join(REPO_DIR, "arena", "active_map.txt")
SERVER_LOADOUTS = os.path.join(_SERVER_DIR, "profiles", "ExpansionMod", "Loadouts")
# Battle-Royale: einheitliches, faires Leicht-Loadout fuer ALLE vier (Pistole +
# 1 Magazin, Rest wird unterwegs gelootet). Liegt in SERVER_LOADOUTS.
BR_LOADOUT = "IsuBrLoadout.json"
# Ambient-AI-Patrouillen (Expansion) je Karte: Menue-Toggle schreibt das
# Top-Level-"Enabled" der Mission-AIPatrolSettings. active_map() -> Mission-Ordner.
MPMISSIONS = os.path.join(_SERVER_DIR, "mpmissions")
MISSION_DIRS = {
    "chernarus": "dayzOffline.chernarusplus",
    "enoch": "dayzOffline.enoch",
    "livonia": "dayzOffline.enoch",
    "sakhal": "dayzOffline.sakhal",
}

# Standard-Lagerpunkt je Karte (Land, nicht im Wasser). Wird genommen, wenn
# der Spieler im Menue keinen eigenen Punkt gesetzt hat (= Chernarus-Default).
MAP_CAMPS = {
    "chernarus": (4233.7, 8512.2),
    "enoch": (7900.0, 6700.0),    # zentral Livonia, Landesinneres zwischen Radunin/Lembork (KEIN Flugplatz - frueher faelschlich "Lukow Airfield")
    "sakhal": (7680.0, 7800.0),   # Petropavlovsk-Sakhalinsk (Hauptstadt, West-Mitte) - offener Platz mitten in der Stadt, ~13m frei von Gebaeuden, Land ~5m. (Alter Punkt 7400/5500 lag im Wasser.) Verifiziert aus mapgrouppos.xml der Sakhal-Mission.
}
CHERNARUS_DEFAULT = (4233.7, 8512.2)

# Skript-Missionen: vordefinierte Szenarien (Menue-Knopf "Mission"). "birgit" =
# Rettungsmission auf Livonia: der Trupp startet am Flugfeld Lukow und muss
# Birgit aus dem Kopa-Gefaengnis befreien, wo sich Banditen eingenistet haben.
# Koordinaten: Kopa-Gefaengnis x5968 z9102 (NW-Livonia; aus BI cfgeventspawns.xml
# "Traffic_Kopa" = Polizeisperre an der NO-Zufahrt zum Gefaengniskomplex - belegt).
# KORREKTUR 06-18: alter Wert 6021/8018 (Supply_Kopa-Konvoi) lag zu weit sued =
# Nidek, NICHT das Gefaengnis. Lukow ~7900/6700. Banditen = feste Raiders-
# Patrouille in der Livonia-AIPatrolSettings (laedt beim Serverstart).
MISSIONS = {
    "birgit": {
        "map": "enoch",
        "map_label": "Livonia",
        "start": (7900.0, 6700.0),    # Lukow Airfield - Rettungstrupp startet hier
        "target": (5968.0, 9102.0),   # Kopa-Gefaengnis (NW) - Birgit gefangen + Banditen
        "combat_agents": ["viktor", "igor", "konrad"],
        "captive": "birgit",
    },
}
# Trupp spawnt eng beisammen am Start (koordinierte Rettung, kein Scatter)
MISSION_OFFSETS = {"viktor": (0.0, 0.0), "igor": (9.0, 5.0), "konrad": (-9.0, 5.0)}
# Elite-Loadouts der Mission (suppressierte Waffen + Muni + Ausruestung, erzeugt
# von tools/gen_mission_loadouts.py). Zusammen mit --no-restore: kein langsames
# Inventar-Wiederherstellen beim Start (sparte ~25s -> Konrad-Startproblem),
# dafuer sofort schwer bewaffnet.
MISSION_LOADOUTS = {"viktor": "IsuMissionViktor", "igor": "IsuMissionIgor",
                    "konrad": "IsuMissionKonrad"}

# {tx}/{tz} = Zielkoordinaten (Kopa). Wird pro Agent eingesetzt.
MISSION_BRIEF_SQUAD = (
    "MISSION: BIRGIT BEFREIEN. Ihr seid an eurem Treffpunkt im zentralen Livonia "
    "aufgewacht (x7900 z6700, Landesinneres - KEIN Flugplatz) - du, deine "
    "Kameraden und der Spieler. Birgit ist NICHT hier. Ihr letzter Funk kam vom "
    "alten Gefaengnis auf dem Huegel bei Kopa (x{tx} z{tz}, Nordwesten): sie "
    "wollte dort nach Vorraeten und Spuren suchen. Seitdem Funkstille, seit "
    "Stunden. Schlechte Nachricht: In dem Gefaengnis haben sich BANDITEN "
    "eingenistet, schwer bewaffnet - Birgit ist dort vermutlich ihre Gefangene.\n\n"
    "EUER AUFTRAG: Sie da rausholen. Aber NICHT blind reinrennen - die Banditen "
    "sind in der Ueberzahl. Geht LEISE vor. Ihr seid bereits ELITE ausgeruestet: "
    "jeder traegt eine SUPPRESSIERTE Waffe, Munition, Medizin und Verpflegung - "
    "ihr muesst NICHT erst lange looten und koennt die Wachen lautlos ausschalten. "
    "Geht moeglichst DIREKT Richtung Kopa; nur wenn euch unterwegs etwas klar "
    "Besseres in die Haende faellt, nehmt es kurz mit, aber macht keine grossen "
    "Umwege.\n\n"
    "VORGEHEN: 1) observe und Waffe ziehen (equip_best; Igor nimmt seine "
    "Nahkampfwaffe). Birgit ist NICHT bei euch - sie ist die Gefangene, wartet "
    "NICHT auf sie. Sammelt euch nur kurz mit den ZWEI anderen Kameraden (und dem "
    "Spieler, wenn er mitkommt), dann brecht SOFORT zur Mission auf. "
    "2) Legt eine MARSCHORDNUNG fest und HALTET sie: einer geht vorne als "
    "Spaeher (Viktor), die anderen FOLGEN ihm in Formation (follow auf den Namen "
    "des Vordermanns) statt jeder fuer sich zu navigieren - so bleibt ihr "
    "zusammen. Nur der Vordermann ruft move_to Richtung Kopa (x{tx} z{tz}). "
    "3) Looted unterwegs Militaer-Stellungen (Waffen, Muni, Schalldaempfer). "
    "4) Naehert euch dem Gefaengnis VORSICHTIG - erst aufklaeren, nicht stuermen. "
    "5) Schaltet die Banditen aus, mit Schalldaempfer leise. 6) ERST wenn die "
    "Banditen erledigt sind und ihr am Gefaengnis steht, ist Birgit sicher - dann "
    "holt ihr sie raus und zieht euch GEMEINSAM zurueck. Haltet per Funk (say) "
    "zusammen. Der Weg ist weit - marschiert beharrlich, brecht NICHT wegen der "
    "Distanz ab."
)
MISSION_BRIEF_FREED = (
    "BEFREIT! Dein Team hat die Banditen am Kopa-Gefaengnis ausgeschaltet und "
    "dich rausgeholt. Du bist wieder frei. Mach SOFORT observe, ruest dich aus "
    "(equip_best, deine Bandagen bereit), schliess dich deinem Team an (follow "
    "auf den naechsten Kameraden) und zieht euch GEMEINSAM vom Gefaengnis "
    "zurueck. Nach so einem Gefecht hat sicher jemand etwas abbekommen - versorge "
    "Verwundete unterwegs. Bedank dich kurz, aber haltet euch nicht lange an dem "
    "gefaehrlichen Ort auf."
)
MISSION_BRIEF_CAPTIVE = (
    "DU BIST IN GEFANGENSCHAFT. Du bist zum alten Gefaengnis bei Kopa gegangen, "
    "um nach Vorraeten und Spuren zu suchen - und bist BANDITEN in die Haende "
    "gefallen, die sich dort eingenistet haben. Sie halten dich fest. Dein Team "
    "(Viktor, Igor, Konrad und der Spieler) weiss, dass dein letzter Funk von "
    "hier kam, und ist unterwegs, um dich zu befreien.\n\n"
    "DEINE LAGE: bewacht und in der Unterzahl. Lauf NICHT eigenmaechtig los - an "
    "den Wachen kaemst du nicht vorbei. Halte dich bedeckt, vermeide "
    "Aufmerksamkeit, bleib in Deckung nahe dem Gefaengnis. Funk ab und zu LEISE "
    "per say, dass du noch lebst und wo du steckst ('Bin im Gefaengnis bei Kopa, "
    "bewacht'), damit dein Team dich findet. Wehr dich nur, wenn dich ein Bandit "
    "DIREKT angreift. Sobald dein Team die Banditen ausgeschaltet hat und bei dir "
    "ist, schliesst du dich ihm an (follow) und ihr zieht euch gemeinsam zurueck. "
    "Durchhalten - sie kommen."
)


def active_map() -> str:
    try:
        with open(ACTIVE_MAP_FILE, "r", encoding="utf-8") as f:
            return (f.readline() or "").strip().lower()
    except OSError:
        return "chernarus"


def start_voice_bots(cfg: dict, roster: dict) -> list:
    """Discord-Bots starten: pro Agent ein eigener Bot in seinem eigenen
    Sprachkanal (arena/discord.json + Token in der jeweiligen Env-Variable).
    Agenten ohne eigenen Token teilen sich den Sammel-Bot
    (DISCORD_BOT_TOKEN, default_channel) - das alte Verhalten."""
    try:
        with open(DISCORD_FILE, "r", encoding="utf-8") as f:
            dcfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        dcfg = {}
    bots = dcfg.get("bots") or {}
    default_channel = dcfg.get("default_channel", "DayZ")

    # Laeuft der lokale Mikro-Router (gleiche Bedingung wie sein Start in
    # Arena.start), duerfen die Bots NICHT zusaetzlich hoeren - sonst kommt
    # jeder Funkspruch doppelt an (Router + Bot-Ohr, je eigenes Transkript)
    no_listen = bool(os.environ.get("ELEVENLABS_API_KEY"))

    procs = []
    shared_outboxes = []
    shared_inboxes = []
    for aid, acfg in cfg["agents"].items():
        if not acfg["enabled"] or aid not in roster:
            continue
        home = agent_home_dir(aid)
        outbox = os.path.join(home, "voice_outbox.jsonl")
        inbox = os.path.join(home, "voice_inbox.jsonl")
        try:
            for stale in (outbox, inbox):
                if os.path.exists(stale):
                    os.remove(stale)  # Altlasten nicht nachplappern
        except OSError:
            pass
        bot = bots.get(aid) or {}
        token_env = bot.get("token_env", "")
        channel = bot.get("channel", default_channel)
        if token_env and os.environ.get(token_env):
            # Log in Datei statt fluechtigem Konsolenfenster - nur so laesst
            # sich ein stummer Bot hinterher diagnostizieren
            logdir = os.path.join(home, "journal")
            os.makedirs(logdir, exist_ok=True)
            logfile = open(os.path.join(logdir, "discord_voice.log"), "a",
                           encoding="utf-8")
            bot_args = [sys.executable,
                        os.path.join(DAEMON_DIR, "discord_voice.py"),
                        "--label", aid,
                        "--token-env", token_env,
                        "--channel", channel,
                        "--voice", roster[aid].get("voice", ""),
                        "--outbox", outbox,
                        "--inbox", inbox]
            if no_listen:
                bot_args.append("--no-listen")
            procs.append(subprocess.Popen(
                bot_args, stdout=logfile, stderr=subprocess.STDOUT))
            log(f"Discord-Bot {aid}: eigener Kanal '{channel}' "
                f"(Log: {aid}/journal/discord_voice.log).")
            # Gestaffelt starten: 4 gleichzeitige Logins provozieren
            # Discord-Connect-Races und ElevenLabs-429
            time.sleep(2.5)
        else:
            shared_outboxes.append(outbox)
            shared_inboxes.append(inbox)

    if shared_outboxes and os.environ.get("DISCORD_BOT_TOKEN"):
        args = [sys.executable, os.path.join(DAEMON_DIR, "discord_voice.py"),
                "--label", "sammel", "--channel", default_channel]
        for box in shared_outboxes:
            args += ["--outbox", box]
        # Funk aus dem Sammel-Kanal geht an ALLE Sammel-Agenten (Broadcast) -
        # ohne diese Inboxen landete er nur in Viktors Default-Inbox
        for box in shared_inboxes:
            args += ["--inbox", box]
        if no_listen:
            args.append("--no-listen")
        sammel_logdir = os.path.join(REPO_DIR, "agent_home", "journal")
        os.makedirs(sammel_logdir, exist_ok=True)
        sammel_log = open(os.path.join(sammel_logdir, "discord_sammel.log"),
                          "a", encoding="utf-8")
        procs.append(subprocess.Popen(args, stdout=sammel_log,
                                      stderr=subprocess.STDOUT))
        log(f"Sammel-Bot: {len(shared_outboxes)} Agenten im Kanal "
            f"'{default_channel}' (Log: agent_home/journal/discord_sammel.log).")
    elif shared_outboxes:
        log("Kein DISCORD_BOT_TOKEN gesetzt - Agenten ohne eigenen Bot "
            "bleiben im Funk stumm (Text-Chat geht immer).")
    return procs

# Spawn-Versatz der vier Agenten um den Lagerpunkt (NW, NO, SW, SO)
# "Getrennt": die Agenten spawnen WEIT auseinander (~90 m in die vier
# Diagonalen, also 180-250 m Abstand) und marschieren dann selbststaendig zum
# Treffpunkt (= Lager), wo sie sich im Coop vereinen - ohne den Spieler (der
# Konvergenz-Auftrag kommt per --rally an run_agent). Distanz bewusst moderat,
# damit alle vier Punkte am Orts-/Stadtlager auf Land bleiben (ResolvePos in der
# Mod prueft kein Wasser; am Sakhal-Stadtlager 7680/7800 verifiziert).
SPAWN_OFFSETS = {
    "viktor": (-90.0, -90.0),
    "birgit": (90.0, -90.0),
    "igor": (-90.0, 90.0),
    "konrad": (90.0, 90.0),
}
# "Als Gruppe": enger Cluster am Lager statt der ~14-m-Streuung, damit die
# Agenten direkt beisammen starten (Menue-Schalter "Spawn: als Gruppe").
GROUP_OFFSETS = {
    "viktor": (-2.0, -2.0),
    "birgit": (2.0, -2.0),
    "igor": (-2.0, 2.0),
    "konrad": (2.0, 2.0),
}
# Maximal-Streuung: jeder Agent spawnt in einer ANDEREN Stadt (km weit) und
# marschiert dann selbst zum Treffpunkt (= Lager), wo sich die Gruppe vereint.
# ABSOLUTE Koordinaten pro Karte (NICHT lagerrelativ). Aus den Server-Mission-
# Dateien (mapgrouppos.xml) als dichte Gebaeude-Cluster verifiziert = Land.
# Fehlt eine Karte/ein Slot hier, faellt der getrennte Spawn auf SPAWN_OFFSETS
# (~90 m) zurueck. Distanz bewusst <= ~5 km, damit der Marsch (auf Sakhal in
# toedlicher Kaelte) machbar bleibt.
SCATTER_TOWNS = {
    # Punkte sind gebaeudefreie Stellen (>=9 m Abstand) im jeweiligen Stadtkern,
    # verifiziert, damit kein NPC in einem Haus stecken bleibt.
    "sakhal": {
        "viktor": (5490.0, 10108.0),    # West-Stadt         ~3,2 km NW vom Lager
        "birgit": (10869.0, 6305.0),    # Slum-Stadt         ~3,5 km SO
        "igor":   (6086.0, 7299.0),     # SW-Dorf (naechste)  ~1,7 km SW
        "konrad": (12703.0, 7278.0),    # Ost-Stadt          ~5,0 km O
    },
    # Chernarus-Lager 4233/8512 (West-Hochland nahe Vybor/Mil). Hoehen 195-307 m
    # = kontinentales Hochland (kein Wasser). Keine toedliche Kaelte.
    "chernarus": {
        "viktor": (6032.0, 7792.0),     # Zentral-Dorf       ~1,9 km SO (naechste)
        "birgit": (2704.0, 5248.0),     # Zelenogorsk        ~3,6 km SW
        "igor":   (3422.0, 12939.0),    # NW-Dorf            ~4,5 km NW
        "konrad": (9556.0, 8847.0),     # Ost-Stadt          ~5,3 km O
    },
    # Livonia-Lager 7900/6700 (Lukow-Airfield, zentral). Hoehen 186-389 m, huegelig.
    "enoch": {
        "viktor": (4991.0, 9869.0),     # NW-Dorf            ~4,3 km NW
        "birgit": (11398.0, 9604.0),    # NO-Dorf            ~4,6 km NO
        "igor":   (6008.0, 4219.0),     # SW-Dorf (naechste)  ~3,1 km SW
        "konrad": (11094.0, 4274.0),    # SO-Dorf            ~4,0 km SO
    },
}


def log(msg: str):
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] [supervisor] {msg}", flush=True)


def write_status(text: str):
    # Atomar (tmp + rename): die Mod pollt die Datei und darf nie einen
    # halben Status sehen
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass
    log("STATUS: " + text)


def agent_home_dir(aid: str) -> str:
    if aid == "viktor":
        return os.path.join(REPO_DIR, "agent_home")
    return os.path.join(REPO_DIR, "agent_homes", aid)


def load_roster() -> dict:
    with open(ROSTER_FILE, "r", encoding="utf-8") as f:
        return {a["id"]: a for a in json.load(f)["agents"]}


# --- Fremd-Backends (Ports muessen zu run_agent.resolve_backend passen) ---
CCR_PORT = 3456      # claude-code-router: openai/ google/ xai/
LLAMA_PORT = 8080    # llama-server: local/ (Gemma 4 E4B)
# Starter-Prozesse merken: verhindert Doppelstart (z.B. zweiter
# 5-GB-Gemma-Download), wenn der User waehrend des Wartens erneut Start drueckt
BACKEND_PROCS: dict[str, subprocess.Popen] = {}
# Von main gepflegt: zuletzt verarbeitete Request-Sequenz. wait_port bricht
# ab, sobald eine NEUE Request eintrifft (User soll nicht 20 min auf einen
# haengenden Backend-Start warten muessen, ohne stoppen zu koennen).
LAST_SEQ = {"seq": ""}


def port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def request_seq() -> str:
    try:
        with open(REQUEST_FILE, "r", encoding="utf-8", errors="replace") as f:
            return (f.readline() or "").strip()
    except OSError:
        return ""


def wait_port(port: int, timeout_s: int, label: str) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_open(port):
            log(f"{label} ist bereit (Port {port}).")
            return True
        if request_seq() != LAST_SEQ["seq"]:
            write_status(f"ABORTED: new command during {label} start")
            return False
        remaining = int(deadline - time.monotonic())
        write_status(f"WAIT for {label} (port {port}, {remaining}s left)...")
        time.sleep(5)
    return False


def spawn_backend(key: str, script: str) -> None:
    prev = BACKEND_PROCS.get(key)
    if prev and prev.poll() is None:
        log(f"{key}-Starter laeuft noch (PID {prev.pid}) - warte nur auf den Port.")
        return
    BACKEND_PROCS[key] = subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File",
         os.path.join(REPO_DIR, "tools", script)],
        creationflags=subprocess.CREATE_NEW_CONSOLE)


def ensure_backends(cfg: dict) -> bool:
    """Startet claude-code-router / llama-server, wenn ein gewaehltes
    Modell sie braucht. Blockiert, bis die Ports antworten."""
    models = [a["model"].lower() for a in cfg["agents"].values() if a["enabled"]]
    needs_ccr = any(m.startswith(("openai/", "google/", "xai/")) for m in models)
    needs_llama = any(m.startswith("local/") for m in models)

    if needs_ccr and not port_open(CCR_PORT):
        write_status("STARTING cloud router (OpenAI/Google/Grok)...")
        spawn_backend("ccr", "start_router.ps1")
        if not wait_port(CCR_PORT, 120, "Cloud-Router"):
            write_status("ERROR: cloud router not ready - "
                         "check the 'start_router' window (npm/API keys).")
            return False

    if needs_llama and not port_open(LLAMA_PORT):
        write_status("STARTING llama-server (first time: ~5 GB Gemma download!)...")
        spawn_backend("llama", "start_llama_gemma.ps1")
        if not wait_port(LLAMA_PORT, 1200, "llama-server"):
            write_status("ERROR: llama-server not ready - "
                         "check the 'start_llama_gemma' window.")
            return False

    return True


class Arena:
    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.voice_procs: list[subprocess.Popen] = []
        self.orch_proc: subprocess.Popen | None = None
        self.br_proc: subprocess.Popen | None = None
        self.pending_birgit: dict | None = None     # Mission: wartet auf Befreiung
        self._birgit_calm_since: float | None = None

    def running_ids(self) -> list[str]:
        return [aid for aid, p in self.procs.items() if p.poll() is None]

    def check_birgit_release(self) -> None:
        """Mission: die Gefangene (Birgit) erscheint erst, wenn der Trupp das
        Kopa-Gefaengnis ERREICHT und GESAEUBERT hat. Heuristik: mind. ein Kampf-
        Agent <= 45 m am Ziel UND seit >= 30 s kaempft KEINER mehr (Banditen tot/
        weg). So laeuft sie nicht als Gefangene mitten unter den Banditen los."""
        pb = self.pending_birgit
        if not pb:
            return
        from bridge import Bridge, DEFAULT_PROFILE
        tx, tz = pb["target"]
        near = False
        fighting = False
        for aid in pb["squad"]:
            st = Bridge(DEFAULT_PROFILE, aid).read_state()
            if not st:
                continue
            npc = st.get("npc") or {}
            if not npc.get("alive"):
                continue
            dx = npc.get("pos_x", 0.0) - tx
            dz = npc.get("pos_z", 0.0) - tz
            if (dx * dx + dz * dz) ** 0.5 <= 45.0:
                near = True
            if npc.get("fighting"):
                fighting = True
        if near and not fighting:
            if self._birgit_calm_since is None:
                self._birgit_calm_since = time.monotonic()
            elif time.monotonic() - self._birgit_calm_since >= 30.0:
                self._launch_birgit_freed()
                self.pending_birgit = None
        else:
            self._birgit_calm_since = None

    def _launch_birgit_freed(self) -> None:
        pb = self.pending_birgit
        if not pb:
            return
        sx, sz = pb["spawn"]
        args = [sys.executable, os.path.join(DAEMON_DIR, "run_agent.py"),
                "--npc-id", pb["aid"], "--name", pb["name"],
                "--model", pb["model"], "--voice", pb["voice"],
                "--language", pb.get("language", "de"),
                "--spawn-x", str(sx), "--spawn-z", str(sz),
                "--faction", "civilian",
                "--idle", str(pb["idle"]), "--turn-limit", str(pb["turns"]),
                "--no-voice-procs", "--no-mic", "--no-tp",
                "--mission", MISSION_BRIEF_FREED]
        if pb["character"] and os.path.exists(pb["character"]):
            args += ["--character", pb["character"]]
        if pb["loadout"]:
            args += ["--loadout", pb["loadout"]]
        proc = subprocess.Popen(
            args,
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.procs[pb["aid"]] = proc
        log(f"MISSION: {pb['name']} BEFREIT - spawnt jetzt am Gefaengnis "
            f"{pb['spawn']} und schliesst sich dem Trupp an.")

    def stop(self):
        # Sanft beenden ueber Stop-Flag-Dateien: CTRL_BREAK erreicht
        # CREATE_NEW_CONSOLE-Prozesse NICHT (eigene Konsole, Signal verpufft
        # ohne Fehler). Der Runner prueft die Flag in seiner 2-s-Schleife,
        # sichert das Inventar und despawnt selbst (finally-Pfad).
        stopped = list(self.procs.keys())
        for aid, proc in self.procs.items():
            if proc.poll() is None:
                try:
                    with open(os.path.join(agent_home_dir(aid), "stop.flag"),
                              "w", encoding="utf-8") as f:
                        f.write("stop\n")
                except OSError:
                    pass
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass
        deadline = time.monotonic() + 45
        for aid, proc in self.procs.items():
            remaining = max(1.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
                log(f"{aid} beendet.")
            except subprocess.TimeoutExpired:
                # Letzte Eskalation: ganzen Prozessbaum killen - sonst
                # ueberleben node/cli.js + dayz_mcp.py als Waisen und
                # steuern den Slot weiter (zwei Gehirne auf einer Bridge)
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
                log(f"{aid} hart beendet (Timeout, Prozessbaum gekillt).")
            try:
                os.remove(os.path.join(agent_home_dir(aid), "stop.flag"))
            except OSError:
                pass
        self.procs = {}
        for vp in self.voice_procs:
            if vp.poll() is None:
                vp.terminate()
                try:
                    vp.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    vp.kill()
        self.voice_procs = []

        # Orchestrator (eigene Konsole) sauber beenden
        if self.orch_proc and self.orch_proc.poll() is None:
            self.orch_proc.terminate()
            try:
                self.orch_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.orch_proc.kill()
            log("Orchestrator beendet.")
        self.orch_proc = None

        # BR-Monitor beenden (laeuft sonst bis zum Zeitlimit weiter)
        if self.br_proc and self.br_proc.poll() is None:
            self.br_proc.terminate()
            try:
                self.br_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.br_proc.kill()
            log("BR-Monitor beendet.")
        self.br_proc = None

        # Sicherheitsnetz: uebrig gebliebene Koerper despawnen (z.B. wenn
        # ein Runner hart starb, bevor sein finally lief)
        if stopped:
            sys.path.insert(0, DAEMON_DIR)
            try:
                from bridge import Bridge, DEFAULT_PROFILE
                for aid in stopped:
                    try:
                        b = Bridge(DEFAULT_PROFILE, aid)
                        state = b.read_state() or {}
                        if (state.get("npc") or {}).get("spawned"):
                            r = b.run("despawn", timeout=12)
                            log(f"{aid}: Nach-Despawn -> {r.get('status')}")
                    except Exception as e:
                        log(f"{aid}: Nach-Despawn fehlgeschlagen: {e}")
            except Exception as e:
                log(f"Despawn-Sicherheitsnetz nicht verfuegbar: {e}")

    def start(self, cfg: dict):
        self.stop()
        roster = load_roster()
        # Menue-Overrides (Stimme + Sprache pro Slot, aus dem "av:"-Segment) in
        # den Roster ziehen, damit Discord-Voice-Bots UND run_agent denselben
        # Wert nutzen. agents.json bleibt der Default, wenn das Menue nichts setzt.
        for aid, acfg in cfg.get("agents", {}).items():
            if aid in roster:
                if acfg.get("voice"):
                    roster[aid]["voice"] = acfg["voice"]
                if acfg.get("language"):
                    roster[aid]["language"] = acfg["language"]
        camp_x, camp_z = cfg["camp"]
        started = []

        # Karte ermitteln (start_game.bat hat sie gewaehlt). Auf nicht-
        # Chernarus-Karten den Chernarus-Default-Lagerpunkt durch den
        # map-eigenen Landpunkt ersetzen (4233/8512 liegt z.B. auf Sakhal im
        # Meer). Hat der Spieler im Menue einen EIGENEN Punkt gesetzt (Koords
        # weichen vom Default ab), bleibt der respektiert.
        amap = active_map()
        if amap != "chernarus":
            dist = abs(camp_x - CHERNARUS_DEFAULT[0]) + abs(camp_z - CHERNARUS_DEFAULT[1])
            if dist < 2.0 and amap in MAP_CAMPS:
                camp_x, camp_z = MAP_CAMPS[amap]
                log(f"Karte '{amap}': Lagerpunkt auf {camp_x}/{camp_z} gesetzt.")
        # Auf Sakhal die Winter-Loadouts waehlen (dicke Kleidung beim Spawn)
        winter = (amap == "sakhal")
        # Ambient-AI-Patrouillen der Karte per Menue-Toggle (wirkt ab Neustart)
        set_patrols(amap, cfg.get("patrols", False))

        # Skript-Mission (Menue-Knopf "Mission"): ueberschreibt pro Agent Spawn,
        # Rally und Briefing. Die Banditen sind eine feste Raiders-Patrouille der
        # Karte (laedt beim Serverstart) - hier wird nur der Rettungstrupp + die
        # Gefangene gesetzt.
        mission = MISSIONS.get(cfg.get("mission") or "")
        if mission:
            if amap != mission["map"]:
                log(f"!!! MISSION '{cfg['mission']}' braucht Karte "
                    f"{mission['map_label']} ({mission['map']}), aktiv ist '{amap}'. "
                    f"Server auf {mission['map_label']} starten, sonst spawnt die "
                    f"Mission an der falschen Stelle.")
            # Missions-Rollen erzwingen (auch wenn das Menue sie aus hatte) - ohne
            # die Gefangene gibt es nichts zu befreien.
            roles = {"viktor": "jaeger", "igor": "bauer",
                     "konrad": "exmilitaer", "birgit": "sanitaeter"}
            for mid in mission["combat_agents"] + [mission["captive"]]:
                a = cfg["agents"].get(mid)
                if a:
                    a["enabled"] = True
                else:
                    cfg["agents"][mid] = {"enabled": True, "model": "sonnet",
                                          "persona": roles.get(mid, "")}
            # Banditen brauchen die AI-Patrouillen global AN - den Menue-patrols-
            # Toggle hier ueberstimmen (wirkt ab dem Serverstart der Missionskarte).
            set_patrols(mission["map"], True)
            log(f"MISSION Birgit-Rettung: Trupp startet Lukow {mission['start']}, "
                f"Ziel Kopa-Gefaengnis {mission['target']}, Birgit als Gefangene "
                f"dort. Banditen bewachen das Gefaengnis (feste Patrouille).")

        # Spawn-Modus folgt dem Menue-Toggle "Gruppe" - auch im BR. GETRENNT =
        # verstreut + Marsch zum Treffpunkt-Showdown (volles Erlebnis). GRUPPE =
        # alle eng am Lager -> sofortiges Gefecht (schneller BR-Test; wer zuerst
        # spawnt, ist im Vorteil).
        separate = not cfg.get("group")
        if separate and amap in SCATTER_TOWNS:
            log(f"Spawn-Modus: GETRENNT (verschiedene Staedte) - jeder Agent "
                f"spawnt in einer anderen Stadt und marschiert zum Treffpunkt "
                f"{camp_x:.0f}/{camp_z:.0f} (Coop, ohne Spieler).")
        elif separate:
            log(f"Spawn-Modus: GETRENNT (~90 m) - Agenten spawnen verstreut und "
                f"sammeln sich am Treffpunkt {camp_x:.0f}/{camp_z:.0f}.")
        else:
            log("Spawn-Modus: GRUPPE - Agenten spawnen eng beisammen am Lager.")

        # Battle-Royale: Loadout sicherstellen + Modus klar ankuendigen
        if cfg["hostile"]:
            ensure_br_loadout()
            log("BATTLE ROYALE aktiv: Free-for-all (auch der Spieler ist Ziel), "
                "1 Leben/kein Respawn, Funkstille, einheitliches Leicht-Loadout.")

        # Fremd-Backends (Router/llama-server) zuerst hochfahren
        if not ensure_backends(cfg):
            return

        # Effektive Namen (Menue darf umbenennen) VOR Router und Runnern
        # publizieren - voice_router (Funk-Adressierung) und
        # run_agent.load_roster_names (Chat-Routing) lesen sie beim Start.
        active = []
        for aid, acfg in cfg["agents"].items():
            if acfg["enabled"] and aid in roster:
                eff = acfg.get("name") or roster[aid].get("name", aid.capitalize())
                active.append({"id": aid, "name": eff})
        try:
            with open(ACTIVE_ROSTER_FILE, "w", encoding="utf-8") as f:
                json.dump({"agents": active}, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log(f"active_roster.json nicht schreibbar: {e}")

        # Voice: eigener Discord-Bot + Kanal pro Agent (discord.json),
        # Rest ueber den Sammel-Bot; dazu EIN Mikro-Router fuer alle
        if cfg["mic"]:
            self.voice_procs += start_voice_bots(cfg, roster)
        if cfg["mic"] and os.environ.get("ELEVENLABS_API_KEY"):
            ids = ",".join(a for a, c in cfg["agents"].items() if c["enabled"])
            if ids:
                self.voice_procs.append(subprocess.Popen(
                    [sys.executable, os.path.join(DAEMON_DIR, "voice_router.py"),
                     "--selected", ids],
                    creationflags=subprocess.CREATE_NEW_CONSOLE))

        for aid, acfg in cfg["agents"].items():
            if not acfg["enabled"] or aid not in roster:
                continue
            a = roster[aid]
            # Spawn-Position: getrennt + Stadtliste fuer die Karte -> jeder in
            # einer ANDEREN Stadt (km weit), Marsch zum Treffpunkt (Lager). Sonst
            # getrennt = ~90m-Streuung, Gruppe = ~2m eng beisammen.
            town = SCATTER_TOWNS.get(amap, {}).get(aid) if separate else None
            if town:
                spawn_x, spawn_z = town
            else:
                doff = (SPAWN_OFFSETS if separate else GROUP_OFFSETS).get(aid, (0.0, 0.0))
                spawn_x, spawn_z = camp_x + doff[0], camp_z + doff[1]

            # Mission ueberschreibt Spawn/Rally/Briefing pro Agent:
            # - Rettungstrupp (Viktor/Igor/Konrad): eng am Start (Lukow), Rally = Kopa
            # - Gefangene (Birgit): direkt am Zielort (Kopa), kein Rally
            mission_brief = ""
            mission_rally = (camp_x, camp_z) if separate else None
            if mission and aid in mission["combat_agents"]:
                soff = MISSION_OFFSETS.get(aid, (0.0, 0.0))
                spawn_x = mission["start"][0] + soff[0]
                spawn_z = mission["start"][1] + soff[1]
                mission_rally = mission["target"]
                mission_brief = MISSION_BRIEF_SQUAD.format(
                    tx=int(mission["target"][0]), tz=int(mission["target"][1]))
            # Agenten bleiben IMMER civilian - die BR-Aggro kommt deterministisch
            # aus IsuAgentRegistry.s_BrMode (Mod), nicht aus der Fraktions-Matrix.
            # So bleibt der zivile Friendly-Fire-Schutz fuer den Coop-Modus intakt.
            faction = "civilian"
            eff_name = acfg.get("name") or a.get("name", aid.capitalize())

            # Persona: Menue-Schluessel -> daemon/characters/<key>.md,
            # Fallback: character-Pfad aus agents.json
            character = ""
            persona_key = (acfg.get("persona") or "").strip().lower()
            if persona_key:
                cand = os.path.join(CHARACTERS_DIR, persona_key + ".md")
                if os.path.exists(cand):
                    character = cand
                else:
                    log(f"{aid}: Persona '{persona_key}' unbekannt - nehme Default.")
            if not character and a.get("character"):
                character = os.path.join(REPO_DIR, a["character"])

            # Mission-Gefangene (Birgit) wird NICHT am Anfang gespawnt: als
            # Gefangene wuerde sie mitten unter den Banditen loslaufen (auch zum
            # Treffpunkt) und sterben. Sie erscheint erst, wenn der Trupp das
            # Gefaengnis erreicht UND gesaeubert hat (check_birgit_release in der
            # Hauptschleife). Hier nur ihre Startparameter merken.
            if mission and aid == mission["captive"]:
                self.pending_birgit = {
                    "aid": aid, "model": acfg["model"], "name": eff_name,
                    "voice": a.get("voice", ""), "language": a.get("language", "de"),
                    "character": character,
                    "loadout": a.get("loadout") or "",
                    "spawn": mission["target"], "target": mission["target"],
                    "squad": list(mission["combat_agents"]),
                    "idle": cfg["idle"], "turns": cfg["turns"],
                }
                self._birgit_calm_since = None
                log(f"MISSION: {eff_name} wartet als Gefangene im Kopa-Gefaengnis "
                    f"- spawnt erst, wenn der Trupp dort ist und die Banditen "
                    f"erledigt sind.")
                continue

            args = [sys.executable, os.path.join(DAEMON_DIR, "run_agent.py"),
                    "--npc-id", aid,
                    "--name", eff_name,
                    "--model", acfg["model"],
                    "--voice", a.get("voice", ""),
                    "--language", a.get("language", "de"),
                    "--spawn-x", str(spawn_x),
                    "--spawn-z", str(spawn_z),
                    "--faction", faction,
                    "--idle", str(cfg["idle"]),
                    "--turn-limit", str(cfg["turns"]),
                    "--no-voice-procs", "--no-mic", "--no-tp"]
            # Getrennter Spawn: dem Agenten den Treffpunkt (= Lager) mitgeben.
            # run_agent macht daraus den Erst-Auftrag "geh allein zum Treffpunkt
            # und vereinige dich mit der Gruppe" - und nutzt ihn auch nach Respawn.
            if mission_rally:
                args += ["--rally-x", str(mission_rally[0]),
                         "--rally-z", str(mission_rally[1])]
            if character and os.path.exists(character):
                args += ["--character", character]
            if mission_brief:
                args += ["--mission", mission_brief]
            # Battle-Royale: Free-for-all + 1 Leben + kein Inventar-Restore
            # (jeder Lauf startet gleich) + faires, fuer ALLE identisches Leicht-
            # Loadout. Sonst das rollenspezifische Loadout (ggf. Winter-Variante).
            if cfg["hostile"]:
                args += ["--br", "--no-respawn", "--no-restore"]
                loadout = BR_LOADOUT
            elif mission and aid in mission["combat_agents"]:
                # Elite-Mission-Loadout + --no-restore: ueberspringt das langsame
                # Inventar-Wiederherstellen (Konrad-Startverzug) und gibt sofort
                # eine suppressierte Elitewaffe + Munition + Ausruestung.
                args += ["--no-restore"]
                loadout = MISSION_LOADOUTS.get(aid, a.get("loadout"))
            else:
                loadout = a.get("loadout")
                if loadout and winter:
                    # IsuViktorLoadout.json -> IsuViktorLoadout_Winter.json,
                    # wenn die Winter-Variante existiert
                    cand = loadout[:-5] + "_Winter.json" if loadout.endswith(".json") else loadout + "_Winter"
                    if os.path.exists(os.path.join(SERVER_LOADOUTS, cand)):
                        loadout = cand
            if loadout:
                args += ["--loadout", loadout]

            # Eigene Konsole (sichtbares Fenster) + eigene Prozessgruppe
            # (CTRL_BREAK erreicht nur diesen Runner)
            proc = subprocess.Popen(
                args,
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP)
            self.procs[aid] = proc
            started.append(f"{eff_name} ({acfg['model']})")
            log(f"gestartet: {aid} name={eff_name} model={acfg['model']} "
                f"persona={persona_key or 'default'} faction={faction}")
            time.sleep(3)

        # Orchestrator (Menue-Toggle "Orchestr."): Schiedsrichter/Lagezentrum
        # ueber den Squad. AN = gemeinsames Lagebild (squad_state.json) +
        # ratenbegrenzter Funk an alle; AUS = die vier laufen unabhaengig
        # (sauberer Modell-Benchmark). Erst NACH den Runnern starten, damit die
        # Bridges schon Zustand schreiben.
        if cfg.get("orch") and active and not cfg["hostile"]:
            orch_ids = ",".join(a["id"] for a in active)
            orch_args = [sys.executable,
                         os.path.join(DAEMON_DIR, "orchestrator.py"),
                         "--agents", orch_ids,
                         "--camp-x", str(camp_x),
                         "--camp-z", str(camp_z)]
            # (Kein --hostile: der Orchestrator laeuft per Gate oben NUR im Coop;
            # im BR wird er gar nicht gestartet -> Funkstille R1.)
            # Optionaler periodischer Lagebericht (sichtbares Lebenszeichen):
            # $env:ISU_ORCH_HEARTBEAT=120 vor dem Start setzen. Unset = aus.
            hb = os.environ.get("ISU_ORCH_HEARTBEAT")
            if hb:
                orch_args += ["--heartbeat", hb]
            self.orch_proc = subprocess.Popen(
                orch_args, creationflags=subprocess.CREATE_NEW_CONSOLE)
            log(f"Orchestrator gestartet (Lagezentrum ueber {orch_ids}).")

        # Battle-Royale-Monitor: liest die state-Dateien direkt, loggt Spawns,
        # Treffer, Tode und am Ende den Sieger. Eigene Konsole, endet selbst
        # (nur noch einer lebt, oder Zeitlimit).
        if cfg["hostile"] and started:
            try:
                self.br_proc = subprocess.Popen(
                    [sys.executable, os.path.join(DAEMON_DIR, "_br_monitor.py")],
                    creationflags=subprocess.CREATE_NEW_CONSOLE)
                log("BR-Monitor gestartet (Kampf-Telemetrie + Sieger).")
            except Exception as e:
                log(f"BR-Monitor-Start fehlgeschlagen: {e}")

        # Frueh gestorbene Voice-Bots melden (falscher Token, fehlender
        # Kanal) - sonst bleibt ein Slot still stumm, waehrend alles
        # "LAEUFT" meldet
        dead_bots = [p for p in self.voice_procs if p.poll() is not None]
        if dead_bots:
            log(f"WARNUNG: {len(dead_bots)} Voice-Bot(s) sofort wieder "
                f"beendet - Token/Kanal im jeweiligen Fenster pruefen.")

        mode = "HOSTILE" if cfg["hostile"] else "neutral"
        orch_note = " +Orch" if (cfg.get("orch") and started) else ""
        if started:
            write_status(f"RUNNING ({mode}){orch_note}: " + ", ".join(started))
        else:
            write_status("STOPPED (no agents selected)")


def ensure_br_loadout() -> None:
    """BR-Loadout ins Server-Verzeichnis kopieren (frischer Pull ohne manuellen
    Deploy). Ueberschreibt immer aus der Repo-Quelle, damit Loadout-Aenderungen
    ankommen. Idempotent, fehlertolerant."""
    src = os.path.join(REPO_DIR, "mod", "loadouts", BR_LOADOUT)
    dst = os.path.join(SERVER_LOADOUTS, BR_LOADOUT)
    try:
        if not os.path.exists(src):
            log(f"WARNUNG: BR-Loadout-Quelle fehlt: {src}")
            return
        os.makedirs(SERVER_LOADOUTS, exist_ok=True)
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
    except OSError as e:
        log(f"BR-Loadout-Deploy fehlgeschlagen ({dst}): {e}")


def set_patrols(amap: str, enabled: bool) -> None:
    """Ambient-AI-Patrouillen der Karte an/aus (Expansion AIPatrolSettings).
    Setzt NUR das Top-Level-"Enabled" (erstes Vorkommnis), Rest unangetastet.
    Wirkt ab dem naechsten Server-Neustart (Expansion liest es beim Start)."""
    mission = MISSION_DIRS.get(amap)
    if not mission:
        log(f"Patrouillen: Karte '{amap}' ohne Mission-Mapping - uebersprungen.")
        return
    path = os.path.join(MPMISSIONS, mission, "expansion", "settings",
                        "AIPatrolSettings.json")
    if not os.path.exists(path):
        log(f"Patrouillen: AIPatrolSettings fuer '{amap}' nicht da - uebersprungen.")
        return
    val = "1" if enabled else "0"
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines):
            if '"Enabled"' in ln:
                indent = ln[:len(ln) - len(ln.lstrip())]
                comma = "," if ln.rstrip().endswith(",") else ""
                lines[i] = f'{indent}"Enabled": {val}{comma}\n'
                break
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"Patrouillen ({amap}): {'AN' if enabled else 'AUS'} gesetzt "
            f"(wirkt ab naechstem Server-Neustart).")
    except OSError as e:
        log(f"Patrouillen-Schreiben fehlgeschlagen: {e}")


def parse_command(line: str) -> dict | None:
    parts = [p.strip() for p in line.strip().split("|") if p.strip()]
    if not parts:
        return None
    action = parts[0].lower()
    if action == "stop":
        return {"action": "stop"}
    if action != "start":
        return None

    cfg = {"action": "start", "agents": {}, "hostile": False,
           "camp": (4233.7, 8512.2), "idle": 120, "turns": 10, "mic": True,
           "group": False, "orch": False, "patrols": False, "mission": ""}
    for part in parts[1:]:
        fields = part.split(":")
        key = fields[0].lower()
        if key in ("viktor", "birgit", "igor", "konrad") and len(fields) >= 3:
            # Totes Modell abfangen: claude-fable-5 (Max-Plan wie API) hat seit
            # 2026-06-14 keinen Zugriff mehr (Sofort-Abbruch, 0 Tokens, der Slot
            # bleibt stumm). Egal was das Menue schickt - auf sonnet umbiegen,
            # statt einen Slot mit einem nicht existierenden Modell zu verbrennen.
            model = fields[2].strip()
            if "fable" in model.lower():
                log(f"{key}: Modell '{model}' ist API-tot - ersetze durch 'sonnet'.")
                model = "sonnet"
            agent = {"enabled": fields[1] == "1", "model": model}
            if len(fields) >= 4 and fields[3].strip():
                agent["persona"] = fields[3].strip()
            if len(fields) >= 5:
                # Name darf theoretisch ":" enthalten - Rest wieder joinen
                name = ":".join(fields[4:]).strip()
                if name:
                    agent["name"] = name
            # update statt assign: ein evtl. zuvor geparstes "av:"-Segment
            # (Stimme/Sprache) im selben Dict nicht ueberschreiben.
            cfg["agents"].setdefault(key, {}).update(agent)
        elif key == "av" and len(fields) >= 4:
            # av:<aid>:<voice>:<lang> - Stimme + Sprache pro Slot, entkoppelt vom
            # Slot-Tupel (dessen Name ':' enthalten darf). lang ist das LETZTE
            # Feld, voice der Rest dazwischen (ElevenLabs-Namen haben kein ':').
            aid = fields[1].lower()
            if aid in ("viktor", "birgit", "igor", "konrad"):
                lang = fields[-1].strip().lower()
                voice = ":".join(fields[2:-1]).strip()
                slot = cfg["agents"].setdefault(aid, {})
                if voice:
                    slot["voice"] = voice
                if lang:
                    slot["language"] = lang
        elif key == "hostile" and len(fields) >= 2:
            cfg["hostile"] = fields[1] == "1"
        elif key == "camp" and len(fields) >= 2:
            xz = fields[1].split(",")
            if len(xz) == 2:
                cfg["camp"] = (float(xz[0]), float(xz[1]))
        elif key == "idle" and len(fields) >= 2:
            cfg["idle"] = max(30, int(float(fields[1])))
        elif key == "turns" and len(fields) >= 2:
            cfg["turns"] = max(0, int(float(fields[1])))
        elif key == "mic" and len(fields) >= 2:
            cfg["mic"] = fields[1] == "1"
        elif key == "group" and len(fields) >= 2:
            cfg["group"] = fields[1] == "1"
        elif key == "orch" and len(fields) >= 2:
            cfg["orch"] = fields[1] == "1"
        elif key == "patrols" and len(fields) >= 2:
            cfg["patrols"] = fields[1] == "1"
        elif key == "mission" and len(fields) >= 2:
            cfg["mission"] = fields[1].strip().lower()
    return cfg


def main() -> int:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    arena = Arena()
    last_seq = ""
    # Stale Request aus einer frueheren Session WEGLOESCHEN, statt nur ihren seq
    # zu merken: der Mod-Zaehler s_Seq (IsuArenaControl) faengt nach jedem
    # Server-Neustart wieder bei 1 an. Ein gemerkter alter seq (z.B. "1" aus der
    # letzten Session) wuerde den ersten echten Klick (seq "1") faelschlich als
    # "schon gesehen" verwerfen -> Symptom "erster Klick nur WARTE, zweiter
    # startet". Datei weg = sauberer Start, jeder neue seq zaehlt.
    if os.path.exists(REQUEST_FILE):
        try:
            os.remove(REQUEST_FILE)
        except OSError:
            pass

    LAST_SEQ["seq"] = last_seq
    write_status("READY - in-game menu: Insert")
    log(f"Supervisor laeuft. Warte auf Befehle in {REQUEST_FILE}")

    bad_tries = 0
    try:
        while True:
            time.sleep(2.0)
            # Mission: Birgit erscheinen lassen, sobald der Trupp Kopa erreicht
            # und die Banditen erledigt hat (no-op ausserhalb der Mission).
            arena.check_birgit_release()
            try:
                with open(REQUEST_FILE, "r", encoding="utf-8", errors="replace") as f:
                    seq = (f.readline() or "").strip()
                    cmd_line = (f.readline() or "").strip()
            except OSError:
                continue
            if not seq or seq == last_seq or not cmd_line:
                continue
            log(f"Befehl #{seq}: {cmd_line[:160]}")
            try:
                cfg = parse_command(cmd_line)
            except Exception as e:
                # Zerrissener Read (Mod schreibt nicht atomar): seq NICHT
                # verbrauchen, der naechste 2-s-Tick liest vollstaendig.
                # Nach 3 Fehlversuchen aufgeben (dauerhaft kaputter Befehl).
                bad_tries += 1
                if bad_tries >= 3:
                    last_seq = seq
                    LAST_SEQ["seq"] = seq
                    bad_tries = 0
                    write_status(f"ERROR: unreadable command ({e})")
                else:
                    log(f"Befehl unlesbar ({e}) - Versuch {bad_tries}/3.")
                continue
            bad_tries = 0
            last_seq = seq
            LAST_SEQ["seq"] = seq
            if not cfg:
                write_status("ERROR: command not understood")
                continue
            if cfg["action"] == "stop":
                write_status("STOPPING...")
                arena.stop()
                write_status("STOPPED")
            else:
                write_status("STARTING...")
                try:
                    arena.start(cfg)
                except Exception as e:
                    write_status(f"ERROR on start: {e}")
    except KeyboardInterrupt:
        log("Supervisor beendet - stoppe Agenten...")
        arena.stop()
        write_status("SUPERVISOR OFF")
        return 0


if __name__ == "__main__":
    sys.exit(main())

