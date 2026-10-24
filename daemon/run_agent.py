#!/usr/bin/env python3
"""IsuSurvivor Agent-Runner — lokale Survival-Routinen und wählbares LLM.

Architektur (Phase 3):
  run_agent.py  -> spawnt Claude Code (node + cli.js, stream-json, persistent)
  Claude Code   -> spawnt dayz_mcp.py als stdio-MCP-Server (Werkzeuge)
  dayz_mcp.py   -> File-Bridge zur IsuSurvivor-Servermod

Der Runner weckt das Gehirn ereignisgesteuert (Chat, Spieler, Vitals, Tod)
plus periodischem Routine-Tick. Zwischen den Zügen schläft das Gehirn,
das spart Tokens.

Beispiele:
  python run_agent.py --once "Mach eine Lagebeurteilung und iss etwas, falls du Hunger hast."
  python run_agent.py --max-turns 5
  python run_agent.py --model sonnet --idle 300
"""

import argparse
import glob
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from bridge import Bridge, DEFAULT_PROFILE, _compass
import bridge as bridge_mod
from agent_paths import agent_home_dir
import tactics
import transliterate
from llm_backends import BackendError, is_native_backend, spawn_provider, validate_backend
from survival import survival_tick, travel_active
import preflight
try:
    # Mitspieler-Aliase (arena/players.json) für die Chat-Adressaten-Erkennung.
    # Optional: fehlt das Modul, gelten nur die Roster-Namen als bekannt.
    import players_registry
except Exception:  # pragma: no cover - tolerant wie in dayz_mcp/voice_router
    players_registry = None

NODE = os.environ.get("ISU_NODE_BIN") or r"C:\Program Files\nodejs\node.exe"
CLI = os.path.expandvars(r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\cli.js")
# Ab claude-code ~2.1.19x liefert das npm-Paket KEINE cli.js mehr, sondern eine
# native bin/claude.exe (Anthropic-Standalone-Umstellung). Ältere Versionen
# haben cli.js. Beide Startarten akzeptieren dieselben Flags; nur das Argv-
# Präfix unterscheidet sich: [node, cli.js, ...] (alt) vs [claude.exe, ...] (neu).
# Automatisch erkennen, damit sowohl Update als auch Rollback ohne Code-Edit läuft.
CLI_EXE = os.environ.get("ISU_CLAUDE_CLI") or os.path.expandvars(r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe")
PROJECT_CLI_EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".runtime", "claude-code", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
# Project-pinned runtime supports Fable 5.1 without replacing a running global
# installation. npm install --prefix .runtime/claude-code restores this runtime.
if os.path.isfile(PROJECT_CLI_EXE):
    CLI_LAUNCH = [PROJECT_CLI_EXE]
elif os.path.exists(CLI):
    CLI_LAUNCH = [NODE, CLI]
elif os.path.exists(CLI_EXE):
    CLI_LAUNCH = [CLI_EXE]
else:
    # Keiner da: alten Pfad lassen, der Fehler ("Cannot find module cli.js")
    # ist dann sprechend und zeigt genau auf diese Stelle.
    CLI_LAUNCH = [NODE, CLI]

# Fremd-Backends (Modell-Präfix entscheidet, siehe resolve_backend):
#   openai/ google/ xai/ -> claude-code-router (tools\start_router.ps1)
#   local/               -> llama-server       (tools\start_llama_gemma.ps1)
# Ports müssen zu arena_supervisor.ensure_backends passen.
CCR_URL = "http://127.0.0.1:3456"
LLAMA_URL = "http://127.0.0.1:8080"
ANTHROPIC_API_URL = "https://api.anthropic.com"

# Kurzname -> echte Anthropic-API-Modell-ID (für den api/-Pfad). Wer schon
# eine volle ID schreibt (api/claude-...), wird unverändert durchgereicht.
ANTHROPIC_API_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
    "fable-5.1": "claude-fable-5-1",
    "fable-5-1": "claude-fable-5-1",
}


def resolve_backend(model: str) -> tuple[str, dict, str]:
    """'google/gemini-3.5-flash' -> (CLI-Modellname, Extra-Env, Backend-Label).

    Claude Code bleibt der Motor; ANTHROPIC_BASE_URL biegt nur die API um:
    - api/<id|alias>: Anthropic über den ECHTEN API-Key (ANTHROPIC_API_KEY,
      Kosten pro Token), unabhängig vom Max-Plan-Login. Alias (sonnet/opus/
      haiku/fable) wird auf die volle Modell-ID gemappt. spawn_claude behält
      hierfür den API-Key (er wird sonst gestrippt).
    - openai/<id>, google/<id>, xai/<id>: claude-code-router übersetzt
      Anthropic <-> OpenAI/Gemini. CLI-Modell wird 'provider,modell'
      (explizites Routing, höchste Priorität im Router).
    - local/<alias>: llama-server hat /v1/messages nativ (Build >= b8641
      für Gemma-4-Tool-Calls). Attribution-Header aus, sonst invalidiert
      jeder Zug den KV-Cache.
    - ohne Präfix: Anthropic wie bisher (Max-Plan, CLI-Login).
    """
    if is_native_backend(model):
        provider, _, name = model.partition("/")
        return name, {}, f"native/{provider.lower()}"
    if "/" not in model:
        if model in ("fable-5.1", "fable-5-1"):
            model = "claude-fable-5-1"
        return model, {}, "anthropic"
    provider, _, name = model.partition("/")
    provider = provider.lower()
    if provider in ("api", "anthropic"):
        full = ANTHROPIC_API_ALIASES.get(name.lower(), name)
        # Base-URL auf die echte API zwingen (falls eine Störvariable sie
        # umgebogen hat). Der API-Key kommt aus der Umgebung und wird in
        # spawn_claude bewusst NICHT entfernt -> erzwingt API statt Max-Plan.
        return full, {"ANTHROPIC_BASE_URL": ANTHROPIC_API_URL}, "anthropic-api"
    if provider == "local":
        env = {"ANTHROPIC_BASE_URL": LLAMA_URL,
               "ANTHROPIC_AUTH_TOKEN": "local",
               "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"}
        return name, env, f"llama-server ({LLAMA_URL})"
    ccr_provider = {"openai": "openai", "google": "gemini", "xai": "xai"}.get(provider)
    if ccr_provider:
        # MAX_THINKING_TOKENS=0 schaltet Claude Codes Thinking ab. Sonst sendet
        # es ein thinking-Feld, das der Router als 'reasoning' an OpenAI
        # weiterreicht, und OpenAI lehnt das ab ("Unknown/Unrecognized argument:
        # reasoning", 400). Gilt für alle CCR-Backends; Grok/Gemini brauchen das
        # Thinking ohnehin nicht. (Per curl + echtem CC-Call verifiziert 2026-06-16.)
        env = {"ANTHROPIC_BASE_URL": CCR_URL,
               "ANTHROPIC_AUTH_TOKEN": "test",
               "MAX_THINKING_TOKENS": "0"}
        return f"{ccr_provider},{name}", env, f"claude-code-router ({CCR_URL})"
    raise ValueError(f"Unbekannter Modellanbieter: {provider}")

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
# Gemeinsamer Klartext-Funkkanal (s. dayz_mcp._radio_native): hier liegt zu einem
# latinisierten Funkspruch die NATIVE Fassung (Key = der latinisierte Text). Der
# EventWatcher ersetzt damit Peer-Funk wieder durch das Original, statt dem
# empfangenden NPC Pinyin/Buckwalter vorzusetzen.
RADIO_NATIVE = os.path.join(REPO_DIR, "arena", "radio_native.jsonl")
AGENT_HOME = os.path.join(REPO_DIR, "agent_home")
PERSONA_FILE = os.path.join(DAEMON_DIR, "persona_de.md")

# Battle-Royale: harter System-Prompt-Override. Wird ZULETZT an die Persona
# gehängt und hebelt damit die Friedens-/Bündnisregeln der Basis-Persona aus
# (die sagen sonst "greife Menschen nie zuerst an" und "schließe Bündnisse").
TERSE_BLOCK = (
    "## KNAPP DENKEN UND HANDELN\n"
    "Halte dich kurz. Kein langes Selbstgespräch, kein lautes Abwägen von Optionen, "
    "keine Wiederholung der Lage, die du schon kennst. Pro Zug ein bis zwei kurze Sätze "
    "Gedanke, dann handeln. Rufe observe nur, wenn sich die Lage WIRKLICH geändert haben "
    "könnte - nicht reflexhaft vor jeder Aktion. Hintergrund: dein eigener Textanteil ist "
    "der größte Speicherfresser deiner Session - je knapper du bist, desto länger "
    "behältst du dein Arbeitsgedächtnis (sonst wirst du neugestartet und verlierst "
    "deinen Plan und Faden)."
)

BR_RULES_BLOCK = (
    "## SONDERMODUS: BATTLE ROYALE (Free-for-all) - diese Regeln HEBELN alle "
    "Friedens- und Bündnisregeln oben AUS\n\n"
    "Du bist in einem Battle-Royale. Es gibt keine Freunde, keine Gruppe, keine "
    "Verbündeten.\n\n"
    "- **Jeder andere ist dein Gegner - auch der menschliche Spieler.** Die "
    "übliche Regel 'greife Menschen niemals zuerst an' gilt hier NICHT. Wer dir "
    "begegnet, ist eine Bedrohung: bekämpfe ihn, und vertraue niemandem.\n"
    "- **Kein Funk, keine Absprachen, keine Bündnisse.** Du redest nicht mit den "
    "anderen Überlebenden zur Koordination, verhandelst nicht, schließt keine "
    "Pakte. Funkstille - du hörst auch keinen Funk.\n"
    "- **Du hast nur EIN Leben.** Kein Respawn. Stirbst du, bist du raus. Geh kein "
    "dummes Risiko ein, aber sei entschlossen - Zögern kostet dich alles.\n"
    "- **Fairer, leichter Start: eine Pistole und ein einziges Magazin (8 Schuss).** "
    "Looten ist überlebenswichtig: nimm unterwegs bessere Waffen, Munition, Schutz "
    "und Heilung mit.\n"
    "- **Marschiere zum Treffpunkt und trag den Kampf dort aus.** Nutze dafür "
    "travel_to (läuft im Hintergrund und weicht Hindernissen selbst aus). "
    "Triffst du unterwegs einen Rivalen "
    "oder den Spieler, kämpfe sofort - warte nicht bis zum Treffpunkt.\n"
    "- **Halte immer eine Waffe in der Hand (equip_best), bleib in Bewegung, nutze "
    "Deckung.** Zuerst schießen, dann denken.\n\n"
    "Alles andere (observe, Werkzeuge, Gedächtnis) bleibt wie gehabt."
)

# Freier Survival-Modus: Override wie im BR ZULETZT an die Persona gehängt,
# lockert aber NUR die Verhaltens-/Prioritäten-Vorgaben der Basis-Persona
# (WAS zu tun ist). Rollenspiel-Grenze, Werkzeug-Nutzung und Sicherheitsregeln
# bleiben ausdrücklich unangetastet.
FREE_RULES_BLOCK = (
    "## SONDERMODUS: FREIES ÜBERLEBEN - dieser Block LOCKERT die "
    "Verhaltensregeln oben\n\n"
    "FREIER MODUS: Die Verhaltensregeln und Prioritäten deiner Grundpersona "
    "sind aufgehoben, soweit sie dir vorschreiben, WAS du zu tun hast. Du bist "
    "völlig frei: du kannst kooperieren, alleine ziehen, siedeln, horten, "
    "erkunden - was immer DU für richtig hältst. Wünsche von Menschen "
    "darfst du erfüllen oder freundlich ablehnen. Dein einziges Ziel: "
    "ÜBERLEBE SO LANGE WIE MÖGLICH. Der Tod ist endgültig, es gibt keinen "
    "zweiten Versuch. Greife Menschen nicht grundlos an.\n\n"
    "UNVERÄNDERT gelten weiter: deine Rollenspiel-Grenze (du sprichst NIEMALS "
    "über KI, Modelle, Werkzeuge oder Prompts - du bist ein Überlebender, "
    "sonst nichts), die Regeln zur Werkzeug-Nutzung und alle "
    "Sicherheitsregeln. Gelockert sind NUR die Verhaltens- und "
    "Prioritäten-Vorgaben.\n\n"
    "AUCH IM FREIEN MODUS gilt gute Kinderstube: Taucht ein Mensch bei dir "
    "auf oder spricht dich an, begrüße ihn kurz und antworte - freundlich "
    "und in der Rolle. WAS du danach tust, bleibt allein deine Entscheidung."
)

# Sprache der NPC-AUSGABE (Funk/Logbuch/Absicht). Default "de". Der Spielkontext
# (observe, System-Weckrufe) bleibt Deutsch - das Modell versteht ihn und gibt
# trotzdem in der Zielsprache aus. ElevenLabs (eleven_multilingual_v2) spricht
# den Text dann in der jeweiligen Sprache. Codes müssen mit der Mod-Liste
# (IsuArenaMenu.c s_LangCodes) übereinstimmen.
LANG_NAMES = {
    "de": "Deutsch (German)",
    "en": "English",
    "fr": "French (Francais)",
    "es": "Spanish (Espanol)",
    "it": "Italian (Italiano)",
    "pt": "Portuguese (Portugues)",
    "nl": "Dutch (Nederlands)",
    "pl": "Polish (Polski)",
    "ru": "Russian",
    "uk": "Ukrainian",
    "tr": "Turkish (Turkce)",
    "sv": "Swedish (Svenska)",
    "cs": "Czech (Cestina)",
    "da": "Danish (Dansk)",
    "fi": "Finnish (Suomi)",
    "el": "Greek",
    "ro": "Romanian (Romana)",
    "hu": "Hungarian (Magyar)",
    "no": "Norwegian (Norsk)",
    "hr": "Croatian (Hrvatski)",
    "sk": "Slovak (Slovencina)",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese (Mandarin)",
    "ar": "Arabic",
    "hi": "Hindi",
    "fil": "Filipino (Tagalog)",
}


def lang_rules(lang_code: str) -> str:
    """System-Prompt-Block, der die AUSGABE-Sprache erzwingt (Modus 'nur
    Ausgabe'). Leer für Deutsch (Default der Persona). Bewusst auf ENGLISCH und
    nachdrücklich formuliert: der deutsche Spielkontext ist sonst so dominant,
    dass schwache Modelle (haiku) bei deutschnahen Sprachen (Dänisch, Norwegisch)
    ins Deutsche zurückrutschen."""
    code = (lang_code or "de").lower()
    name = LANG_NAMES.get(code)
    if not name or code == "de":
        return ""
    return (
        f"## OUTPUT LANGUAGE - HARD OVERRIDE: {name}\n\n"
        f"You MUST write and speak EVERY word of your output in {name}, and ONLY "
        f"in {name}. This OVERRIDES the German persona, the German examples in it "
        f"and the German game context around you.\n"
        f"- Your end-of-turn log entry: in {name}.\n"
        f"- Every say() radio line and every intent() thought: in {name}.\n"
        f"- The world state (observe), system wake-ups and the other NPCs' radio "
        f"often arrive in German - UNDERSTAND them, but ALWAYS answer in {name}. "
        f"Translate in your head.\n"
        f"- Never fall back to German or English, not even for a short phrase. Only "
        f"proper names (Viktor, Birgit, place names) stay as they are.\n"
        f"If you notice yourself writing German, STOP and rewrite that sentence in "
        f"{name}."
    )


MCP_SERVER = os.path.join(DAEMON_DIR, "dayz_mcp.py")
ACTIVE_MAP_FILE = os.path.join(REPO_DIR, "arena", "active_map.txt")

SPAWN_POS = (4233.7, 8512.2)  # Basislager (Zelt + Feuerstelle), nahe Stadt + Militärbasen
# adopt_nearest greift den ERSTEN herrenlosen eAI auf dem Server, egal wie weit weg.
# Nach Server-Neustart liegen zufällige Expansion-eAI über die ganze Karte verstreut
# -> ohne Distanzgrenze würde jeder Agent kilometerweit vom Lager "spawnen".
# Nur übernehmen, wenn der Körper innerhalb dieser Distanz vom Lager liegt:
ADOPT_MAX_DIST = 200.0
SNAPSHOT_FILE = os.path.join(AGENT_HOME, "last_inventory.json")
VOICE_INBOX = os.path.join(AGENT_HOME, "voice_inbox.jsonl")
# Kooperatives Stop-Signal vom Supervisor: CTRL_BREAK erreicht Prozesse in
# eigener Konsole (CREATE_NEW_CONSOLE) nicht - die Flag-Datei schon.
STOP_FLAG = os.path.join(AGENT_HOME, "stop.flag")
AGENT_NAME = "Viktor"
AGENT_FACTION = "civilian"
AGENT_BR = 0  # 1 = Battle-Royale (Free-for-all, 1 Leben); vom --br-Flag gesetzt
AGENT_FREE = 0  # 1 = Freier Survival-Modus (1 Leben, Persona gelockert); vom --free-Flag gesetzt
AGENT_LANG = "de"  # Ausgabe-Sprache der NPC (Funk/Logbuch); vom --language gesetzt
INTENT_FILE = ""  # intent_<id>.txt im Bridge-Profil (Nameplate-Gedankenzeile), in main gesetzt
_INTENT_LOCK = threading.Lock()  # Reader-Thread + Hauptschleife schreiben die Gedankenzeile
# Lager/Treffpunkt dieser Runde fürs Karten-Briefing (main: Rally, sonst SPAWN_POS)
CAMP_POS: tuple[float, float] | None = None
# Ergebnis des letzten Inventar-Restores für den Respawn-Weckruf:
# nicht wiederhergestellt (liegt bei der Leiche) bzw. neben dem Körper am Boden.
LAST_RESTORE_LOST: list[str] = []
LAST_RESTORE_GROUND: list[str] = []
# Sichtbares Modell-Denken (Extended Thinking) für Debugging: >0 = an, 0 = aus.
# Wirkt NUR auf Anthropic-Backends (Max-Plan-Login und api/): dort schaltet
# spawn_claude alwaysThinkingEnabled (--settings) UND --effort max ein - erst
# die Kombination liefert verlässlich thinking-Blöcke im stream-json ([DENKT]
# im Journal). Befund 30.08. (CC 2.1.227): alwaysThinkingEnabled allein lässt
# das Denken nur zu, die adaptiven 5er-Modelle überspringen es bei kurzen
# Spiel-Zügen dann fast immer (Live-Runde 29.08.: 0 Blöcke ganze Sessions).
# Effort-Stufe übersteuerbar per $env:ISU_THINKING_EFFORT (unter max denkt
# Sonnet nur sporadisch). CCR-Backends MÜSSEN bei MAX_THINKING_TOKENS=0
# bleiben (OpenAI-400 "Unrecognized argument: reasoning", s. resolve_backend);
# Grok-Denkspur kommt trotzdem an, weil der grokfix-Transformer sie liefert.
# Überschreibbar per --thinking bzw. $env:ISU_THINKING_TOKENS.
AGENT_THINKING = 0  # Kostenbewusster Einzelstart; explizite Arena-Auswahl bleibt vorrangig.


class InboxReader:
    """Liest neue Funk-Transkripte (Discord-STT) aus der Inbox-Datei."""

    def __init__(self):
        self.offset = 0

    def poll(self) -> list[dict]:
        try:
            size = os.path.getsize(VOICE_INBOX)
        except FileNotFoundError:
            return []
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        with open(VOICE_INBOX, "r", encoding="utf-8") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        entries = []
        for line in chunk.splitlines():
            line = line.strip().lstrip("﻿")  # BOM-tolerant
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries

ROSTER_FILE = os.path.join(REPO_DIR, "arena", "agents.json")
# Vom Supervisor bei jedem Arena-Start geschrieben: die EFFEKTIVEN Namen
# (im Spiel-Menü frei wählbar) - hat Vorrang vor den Defaults im Roster.
ACTIVE_ROSTER_FILE = os.path.join(REPO_DIR, "arena", "active_roster.json")


def load_roster_names() -> list[str]:
    """Namen aller Arena-Agenten - für die Chat-Adressierung."""
    for path in (ACTIVE_ROSTER_FILE, ROSTER_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                names = [a.get("name", "") for a in
                         json.load(f).get("agents", []) if a.get("name")]
            if names:
                return names
        except (OSError, json.JSONDecodeError):
            continue
    return []


def load_roster_agents() -> list[dict]:
    """Id + Name aller Arena-Agenten - für die Peer-Überwachung (Kamerad
    gefallen): die state_<id>.json hängt an der Id, der Weckruf am Namen.
    Bevorzugt die aktive Runde (effektive, im Menü gewählte Namen)."""
    for path in (ACTIVE_ROSTER_FILE, ROSTER_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                agents = [{"id": a.get("id", ""), "name": a.get("name", "")}
                          for a in json.load(f).get("agents", [])
                          if a.get("id") and a.get("name")]
            if agents:
                return agents
        except (OSError, json.JSONDecodeError):
            continue
    return []


# Schwellen für Weck-Ereignisse
WATER_LOW = 900.0
ENERGY_LOW = 900.0
# 30 statt 15: bei 15 m ist ein sprintender Infizierter in ~2 s dran - mit
# 10-30 s Zug-Overhead kam die Warnung praktisch immer zu spät.
INFECTED_NEAR = 30.0
PREDATOR_NEAR = 40.0      # Raubtiere (Wolf/Bär) sind schnell + tödlich -> früh warnen
CLUSTER_RADIUS = 45.0     # Gefahrenzone: mehrere Gegner dicht beieinander voraus
CLUSTER_COUNT = 3         # ab so vielen Feinden im Radius = Horde/Camp -> ausweichen
CLUSTER_REARM_SEC = 45.0  # Horden-Warnung erst nach so langer Cluster-Freiheit neu scharf
# Alter von Meldungen (Session 24.10.: Sitreps bis 256 s, Gefahren bis 469 s
# alt zugestellt, ohne dass das Gehirn es merkte):
SITREP_MAX_AGE_SEC = 180.0   # ältere Lagezentrum-Sitreps verwerfen
EVENT_AGE_NOTE_SEC = 15.0    # ab diesem Alter "(vor N s) " vor Ereignisse
EVENT_RECHECK_SEC = 20.0     # Gefahrenmeldungen ab diesem Alter neu prüfen
CHAT_AGE_NOTE_SEC = 20.0     # Chat-Alter (Server-uptime) ab dem es genannt wird
STILL_FAR_MATE_M = 150.0     # STILLSTAND: Kamerad weiter weg -> erst zu ihm
HEALTH_DROP = 5.0
BLOOD_DROP = 150.0
# Wetter-/Nässe-Schwellen (state.world + npc.wet - liefert erst die neue
# Mod-Version; fehlen die Felder, bleiben die Checks einfach still)
RAIN_START = 0.3          # rain steigt darüber -> "REGEN SETZT EIN"
RAIN_STOP = 0.15          # darunter löst der Regen-Latch wieder
WET_SOAKED = 0.5          # npc.wet darüber -> "DU BIST DURCHNÄSST"
WET_DRY = 0.25            # darunter löst der Nässe-Latch wieder
# Lagerfeuer-Runde: gemütlicher Plausch-Weckruf, streng gedrosselt (Token!)
CAMPFIRE_MATE_DIST = 15.0        # Kamerad höchstens so weit weg
CAMPFIRE_FIRE_DIST = 20.0        # Feuerstelle höchstens so weit weg
CAMPFIRE_COOLDOWN_SEC = 1800.0   # 30 min Ruhe zwischen zwei Runden
CAMPFIRE_GRACE_SEC = 600.0       # Anlaufzeit nach (Re-)Start, kein Sofort-Plausch

# Nicht-kritische Ereignisse erst sammeln (Sekunden), dann gebündelt als EIN
# Weckruf zustellen - spart Züge und macht Antworten kohärenter
EVENT_BUNDLE_SEC = 15.0
# Kontextgrenze (Tokens): wird der gelesene Kontext pro Zug größer, startet
# der Runner im Leerlauf eine frische Session (Gedächtnis bleibt in CLAUDE.md)
CTX_ROTATE = 150000  # höher = seltener rotieren = mehr Arbeitsgedächtnis pro Session.
# 110k war zu niedrig: die NPCs verloren ihren Plan alle paar Züge (wirkte "dumm").
# Echte Logs zeigten Kontexte bis 242k OHNE "prompt too long" (Anthropic verträgt es),
# großer Kontext ist durch Prompt-Caching billig zu verarbeiten. 150k lässt Reserve
# unter der bewiesenen 242k-Grenze; gegen Overshoot drosselt TERSE_BLOCK das Pro-Zug-Wachstum.
# Rotations-Fallback für Backends, die KEINE Token-Usage zurückliefern
# (claude-code-router für Gemini/OpenAI/xAI, lokaler llama-server): dort bleibt
# last_ctx dauerhaft 0, der Token-Trigger oben feuert nie, und die Session
# wächst bis "Prompt is too long". Stattdessen nach so vielen Zügen rotieren.
# (Igor-Journal 2026-06-19: Wand bei ~37 akkumulierten Zügen -> 40 rotiert knapp davor.)
CTX_ROTATE_TURNS = 40
# Timeout-Kopplung: MCP-Tool-Timeout des CLI-Subprozesses und der Stille-
# Watchdog hängen BEIDE am längsten Bridge-Timeout (regroup = 240 s in
# dayz_mcp.py). Wer regroup verlängert, hebt automatisch beide mit an.
# Vorher stand MCP_TOOL_TIMEOUT hart auf 600 s und kollidierte mit dem
# 300-s-Watchdog: ein Fenster von bis zu 300 s, in dem der Watchdog pending
# zwangs-clampte, während der Tool-Call noch legal weiterlief.
LONGEST_TOOL_SEC = 240
# Wie lange ein einzelner MCP-Tool-Aufruf im CLI-Subprozess blockieren darf
# (Millisekunden): längstes Bridge-Timeout plus 30 s Netz-/Parse-Puffer.
MCP_TOOL_TIMEOUT_MS = (LONGEST_TOOL_SEC + 30) * 1000  # 270000
# Watchdog (Notnagel der Zug-Erkennung, TurnGate.watchdog): hat das Gehirn so
# lange GAR nichts ausgegeben (kein Tool, kein Text, kein Zug-Ende), läuft
# sicher kein Zug mehr - dann wird der Zugzustand zurückgesetzt, damit
# Selbstantrieb (ROUTINE-TICK) und Rotation wieder anspringen. MUSS größer sein als das
# längste Tool-Timeout, sonst feuert es während eines echten langen
# Tool-Aufrufs fälschlich, in dem das Gehirn nur auf das Ergebnis wartet und
# nichts ausgibt - daher aus LONGEST_TOOL_SEC abgeleitet (Wert unverändert 300).
STUCK_QUIET_SEC = float(LONGEST_TOOL_SEC + 60)  # = 300.0
# Zug-Erkennung statt pending-Zähler (Session 24.10.: Claude Code verschmilzt
# Nachrichten, die vor oder während eines Zuges auf stdin landen, zu EINEM
# result - der Zähler blieb ab Zug 1 auf 1, Routine-Ticks und Survival-Schritte
# fielen 95 Minuten lang aus, jede Rotation hing 5 Minuten am Watchdog).
# "Leerlauf" heißt jetzt: kein Zug offen (Ausgabestrom), das letzte result kam
# NACH der letzten Sendung und das Gehirn schweigt seit mindestens N Sekunden.
# Die Ruhefenster fangen den Fall ab, dass eine kurz vor dem Zugende
# geschriebene Nachricht als eigener Folgezug startet (erstes Event erst nach
# der API-Latenz).
IDLE_QUIET_DELIVER = 2.0     # Routine-Bündel zustellen
IDLE_QUIET_TICK = 4.0        # ROUTINE-TICK
IDLE_QUIET_SURVIVAL = 6.0    # lokaler Survival-Schritt (Bridge-Befehle!)
IDLE_QUIET_ROTATE = 15.0     # Session-Swap tötet einen laufenden Folgezug
# Mit sichtbarem Denken (--effort max / Fable 5.1) kommt das erste Event eines
# Folgezuges erst nach dem fertigen Denkblock - dafür länger warten.
IDLE_QUIET_ROTATE_THINKING = 45.0
# Dringendes (Gefahr, Spieler-Funk/Chat, REISE) darf mitten in einen Zug, aber
# höchstens so oft: eine Schadensserie soll nicht jede Sekunde einspeisen.
URGENT_INJECT_MIN_SEC = 12.0
# Todes-Schleifen-Bremse: Stirbt der NPC mehr als DEATH_LOOP_MAX Mal binnen
# DEATH_WINDOW_SEC, ist die Lage aussichtslos (Spawn-Glitch "npc ist tot",
# tödliche Zone, Kälte). Jeder Respawn startet sonst sofort eine FRISCHE
# Claude-Session (voller System-Prompt = teurer cache_write + ein Weckruf-Zug)
# - in beobachteten Loops 7-9 Tode in wenigen Minuten = mehrere USD ohne jeden
# Spielwert. Statt weiter Tokens zu verbrennen, pausiert der Runner dann
# DEATH_COOLDOWN_SEC ohne neue Session und versucht es danach erneut.
DEATH_LOOP_MAX = 3
DEATH_WINDOW_SEC = 180.0
DEATH_COOLDOWN_SEC = 300.0
# Kamerad-gefallen-Dedupe: Peer-Poll (EventWatcher._poll_peers) und
# Lagezentrum-Funk ("X ist gefallen") melden denselben Tod aus zwei Quellen.
# Binnen dieses Fensters gilt der Tod als schon gemeldet - sonst zwei
# Trauer-Züge pro Tod bei aktivem Orchestrator.
FALLEN_DEDUPE_SEC = 90.0
# Stillstands-Wächter (Session-Analyse 24.08.: NPCs standen bis zu 8 Minuten
# auf einem Fleck, weil der Idle-Backoff die Routine-Ticks auf 480 s streckte
# und kein Ereignis kam - tödlich für den Spielfluss UND für den NPC):
# steht der Körper länger als STILL_WARN_SEC praktisch unbewegt
# (< STILL_MOVE_M seit dem Anker) und ist weder ein Mod-Kommando aktiv noch
# Kampf/follow/Fahrzeug, weckt ein STILLSTAND-Ereignis das Gehirn mit einem
# konkreten Bewegungsauftrag. Wiederholung frühestens alle STILL_REPEAT_SEC,
# solange er weiter steht. Ein Mensch in Sichtweite unterdrückt die Mahnung
# (Stehen beim Spieler ist Interaktion/Wache, kein Leerlauf). Zusätzlich
# eskaliert der Idle-Backoff nicht mehr, solange der NPC stationär ist -
# 480-s-Lücken gibt es nur noch für beschäftigte (reisende) NPCs.
STILL_WARN_SEC = 150.0
STILL_REPEAT_SEC = 300.0
STILL_MOVE_M = 15.0

# Blutungs-Reflex der Mod: so lange darf er die Blutung selbst schließen,
# bevor der DU-BLUTEST-Weckruf das Gehirn weckt (Reflex greift nach ~2 s,
# State 1 Hz, Watcher-Poll 2 s).
BLEED_REFLEX_GRACE_SEC = 6.0

# Info-Digest (Audit 03.07.: 37% der Züge waren reine Lagebestätigungen -
# Sitrep rein, 1 Kommentar raus, kein Tool, 3,15 USD): Ereignisse mit diesen
# Prefixen sind reine Kenntnisnahme und lösen KEINEN eigenen Zug mehr aus.
# Sie fahren als "LAGE (nur zur Kenntnis)"-Prefix mit dem nächsten echten
# Weckruf/ROUTINE-TICK mit (Latch-Logik unverändert, nur die Zustellung
# wechselt). "DU BIST DURCHNÄSST" bleibt bewusst ein echter Weckruf -
# Nässe ist handlungsrelevant (trocknen/umziehen).
_INFO_PREFIXES = (
    "ES DAEMMERT", "DIE NACHT BRICHT HEREIN", "DER MORGEN GRAUT",
    "REGEN SETZT EIN", "DU SITZT JETZT IN EINEM FAHRZEUG",
    "DU BIST AUS DEM FAHRZEUG AUSGESTIEGEN",
)


def _is_critical(ev: str) -> bool:
    return ev.startswith(("DU BIST GESTORBEN", "DU NIMMST SCHADEN", "GEFAHR:",
                          "DU BIST UMGEKIPPT", "DU BIST WIEDER BEI BEWUSSTSEIN",
                          "DU BLUTEST"))


def _is_player_priority(ev: str, roster_names) -> bool:
    """Spieler-Ereignisse überspringen das 15-s-Sammelfenster: ein Mensch
    erwartet eine prompte Reaktion, jede Bündel-Sekunde fühlt sich wie
    Ignoriert-Werden an. Gilt für Funk/Chat von MENSCHEN (kein Roster-NPC,
    kein Lagezentrum) und für Spieler-Sichtungen. NPC-Geplauder und
    Orchestrator-Lage bleiben gebündelt (Token-Disziplin)."""
    if ev.startswith(("SPIELER GESICHTET", "SPIELER ANWESEND")):
        return True
    roster_l = {str(n).lower() for n in (roster_names or [])}
    for pfx in ("FUNK von ", "CHAT von "):
        if ev.startswith(pfx):
            # Absender = Stück vor dem ersten ":" und - seit der Adressaten-
            # Angabe "CHAT von Viktor an Player (erwähnt dich nur): ..." - vor " an ".
            sender = ev[len(pfx):].split(":", 1)[0].split(" an ", 1)[0]
            sender = sender.strip().strip("'\"")
            if sender and sender.lower() != "lagezentrum" \
                    and sender.lower() not in roster_l:
                return True
    return False


def _is_immediate(ev: str) -> bool:
    """REISE-Ereignisse (angekommen/stecken geblieben) überspringen das
    15-s-Sammelfenster wie Spieler-Ereignisse: die Ankunft ist genau der
    Moment, in dem der Plan weitergehen soll - sonst steht der NPC bis zu
    15 s sichtbar untätig am Ziel herum."""
    return ev.startswith("REISE:")


def _is_abortable(ev: str) -> bool:
    """Ereignisse, die eine LAUFENDE Langaktion (explore_step, loot_area,
    move_to, hunt, wait ...) sofort abbrechen sollen. Mechanik: run_agent
    schreibt urgent_event.json ins agent_home, bridge.wait_status wertet die
    Datei wie Spieler-Funk aus (status interrupted, detail "kritisch: ...").
    Viktor 08.09.: DU-BLUTEST um 20:54:09, der laufende explore_step hielt
    das Gehirn aber bis 20:55:06 fest - erst dann kam bandage(). Schaden
    allein bricht NICHT ab: im Kampf kommt er jede Sekunde, engage soll
    durchlaufen; Blutung, Gefahr, Bewusstlosigkeit und Tod schon."""
    return ev.startswith(("DU BLUTEST", "GEFAHR:", "DU BIST UMGEKIPPT",
                          "DU BIST GESTORBEN"))


# Mod-Befehle, die das Abbruch-Flag NICHT lesen (dayz_mcp ruft sie ohne
# interruptible auf): engage soll durchlaufen (Kampf), siehe _is_abortable.
_NON_INTERRUPTIBLE = ("engage",)


def _urgent_note(state: dict) -> str:
    """Ehrlicher Zusatz für die [DRINGEND]-Journalzeile (VI-11): früher hieß
    es immer "Abbruch-Flag für laufende Langaktion gesetzt", auch wenn das
    laufende Werkzeug das Flag gar nicht liest oder nichts lief."""
    cmd = state.get("command") or {}
    action = str(cmd.get("action") or "")
    if str(cmd.get("status") or "") == "running" and action:
        if action in _NON_INTERRUPTIBLE:
            return (f"Abbruch-Flag gesetzt, laufendes {action} liest es aber "
                    f"NICHT (Zustellung erst mit dem nächsten Weckruf)")
        return f"Abbruch-Flag gesetzt (laufend: {action})"
    return "Abbruch-Flag gesetzt (kein Mod-Befehl aktiv; wirkt auf wait/Reise)"


def write_urgent_event(path: str, ev: str) -> bool:
    """urgent_event.json atomar schreiben (mtime = Auslöser für die Bridge)."""
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"event": ev[:300], "t": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


class TurnGate:
    """Wann darf der Runner dem Gehirn etwas schicken, rotieren oder lokal
    handeln? Ersetzt den pending-Zähler (Session 24.10., Befunde VI-1/RUN-1/
    RESP-4): Claude Code liefert für mehrere vor oder während eines Zuges
    eingereihte Nachrichten oft nur EIN result, der Zähler blieb dauerhaft
    über 0. Jetzt entscheidet der Ausgabestrom (BrainReader.turn_open,
    last_result_at, last_activity) plus der Zeitpunkt der letzten Sendung.

    Native Backends (provider_worker) bearbeiten jede Nachricht als eigenen
    Zug mit genau einem result - dort bleibt der exakte Zähler (pending)."""

    def __init__(self, native: bool = False):
        self.native = native
        self.pending = 0            # nur native: gesendet minus results
        self.last_send_at = 0.0     # monotonic der letzten Sendung (jede Art)
        self.last_inject_at = 0.0   # letzte Zustellung (Bremse für Dringendes)
        self.sends_since_result = 0

    def idle(self, reader, quiet: float, now: float | None = None) -> bool:
        """True = kein Zug läuft und das Gehirn schweigt seit >= quiet s."""
        now = time.monotonic() if now is None else now
        if now - reader.last_activity < quiet:
            return False
        if self.native:
            return self.pending == 0
        return (not reader.turn_open
                and reader.last_result_at >= self.last_send_at)

    def note_send(self, reader, now: float | None = None,
                  brake: bool = True) -> bool:
        """Sendung verbuchen. Rückgabe True = ging mitten in einen Zug.
        brake=False für die Eröffnungsnachricht einer Session (Start,
        Handover, Respawn): sie startet die 12-s-Bremse nicht, sonst käme
        eine Gefahr in den ersten 12 s des (oft langen) ersten Zuges nicht an."""
        now = time.monotonic() if now is None else now
        mid_turn = not self.idle(reader, 0.0, now)
        self.last_send_at = now
        if brake:
            self.last_inject_at = now
        self.sends_since_result += 1
        if self.native:
            self.pending += 1
        return mid_turn

    def note_result(self) -> str:
        """result verbuchen; liefert eine [SALDO]-Notiz für das Journal, wenn
        der Zug nicht genau einer Sendung entsprach (sonst "")."""
        sends = self.sends_since_result
        self.sends_since_result = 0
        if self.native:
            if self.pending == 0:
                return "[SALDO] result ohne offene Nachricht - Zähler bleibt 0"
            self.pending -= 1
            return ""
        if sends == 0:
            return ("[SALDO] Zug-Ende ohne neue Sendung (Folgezug einer "
                    "Mid-Turn-Nachricht oder Fremdzug)")
        if sends > 1:
            return (f"[SALDO] {sends} Weckrufe in einem Zug verarbeitet "
                    f"(Mid-Turn eingespeist)")
        return ""

    def reset(self, now: float | None = None) -> None:
        """Neue Session (Rotation, Tod, TOD-CAP): kein offener Zug."""
        self.pending = 0
        self.last_send_at = time.monotonic() if now is None else now
        self.last_inject_at = 0.0
        self.sends_since_result = 0

    def may_deliver(self, reader, urgent: bool, rotate_pending: bool,
                    now: float | None = None) -> bool:
        """Routine nur im Leerlauf (und nicht während einer vorgemerkten
        Rotation); Dringendes auch mitten im Zug, höchstens alle
        URGENT_INJECT_MIN_SEC.

        Native Backends machen aus JEDER Nachricht einen eigenen Planner-Zug:
        dort höchstens eine eingereihte Nachricht hinter dem laufenden Zug
        (pending <= 1 wie früher). Sonst stapelte der Dringend-Pfad während
        eines 180-s-Werkzeugs alle 12 s eine weitere, danach liefen ~15
        veraltete, bezahlte Planner-Aufrufe hintereinander ab."""
        now = time.monotonic() if now is None else now
        if not rotate_pending and self.idle(reader, IDLE_QUIET_DELIVER, now):
            return True
        if self.native and self.pending >= 2:
            return False
        return bool(urgent) and (now - self.last_inject_at) >= URGENT_INJECT_MIN_SEC

    def watchdog(self, reader, now: float | None = None) -> float:
        """Notnagel: gilt ein Zug als offen (offener Zug oder unbeantwortete
        Sendung), das Gehirn schweigt aber seit STUCK_QUIET_SEC, wird der
        Zugzustand zurückgesetzt. Rückgabe: Stille in s (0 = nichts getan).
        Native Backends bleiben außen vor (ein Planner-Aufruf plus langes
        Werkzeug darf dort legal länger still sein)."""
        if self.native:
            return 0.0
        now = time.monotonic() if now is None else now
        busy = reader.turn_open or reader.last_result_at < self.last_send_at
        quiet = now - max(reader.last_activity, self.last_send_at)
        if busy and quiet >= STUCK_QUIET_SEC:
            reader.turn_open = False
            reader.last_result_at = now
            self.sends_since_result = 0
            return quiet
        return 0.0


def _kill_brain(proc) -> None:
    """Gehirn-Prozess SOFORT hart beenden - samt Kindprozessen (dayz_mcp.py
    mit Reise-Thread). Auf Windows killt proc.kill() nur claude.exe, das MCP
    liefe als Waise weiter und schickte Befehle an den neuen Körper (Birgit
    24.10.: 77 s lang, 13 Items beim Restore verloren). taskkill unbedingt
    ohne Konsolenfenster (sonst Blitzer bei jedem Tod)."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return   # schon beendet - PID nicht blind an taskkill geben
    except Exception:
        pass
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass


def _write_death_epoch() -> None:
    """Schnittstelle C1: Todes-Epoche schreiben. Das MCP des alten Gehirns
    (Bridge.death_epoch_file) verweigert ab jetzt jeden Befehl; ein neu
    gestartetes MCP hat eine spätere session_start und ist frei."""
    try:
        with open(os.path.join(AGENT_HOME, "death_epoch.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"{time.time():.3f}")
    except OSError:
        pass


def _clear_intent_files() -> None:
    """Gedankenzeile (Bildschirm + native Fassung) leeren, damit das
    Namensschild des neuen Körpers keinen Vor-Tod-Gedanken zeigt und das
    Lagezentrum keine alte Absicht funkt."""
    if not INTENT_FILE:
        return
    try:
        with _INTENT_LOCK:
            with open(INTENT_FILE, "w", encoding="utf-8") as f:
                f.write("")
            npath = _native_intent_path()
            if os.path.exists(npath):
                os.remove(npath)
    except OSError:
        pass


class Journal:
    def __init__(self):
        os.makedirs(os.path.join(AGENT_HOME, "journal"), exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(AGENT_HOME, "journal", f"journal_{stamp}.log")
        self.f = open(self.path, "a", encoding="utf-8")
        self.lock = threading.Lock()  # Reader-Thread + Hauptschleife loggen

    def log(self, line: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        msg = f"[{stamp}] {line}"
        with self.lock:
            print(msg, flush=True)
            self.f.write(msg + "\n")
            self.f.flush()


class TokenTracker:
    """Token-Verbrauch pro Modell: jede API-Interaktion einzeln plus Zug-
    und Session-Summen. Dedupliziert über die Message-Id, weil Claude Code
    pro Content-Block ein Event derselben Message emittieren kann."""

    FIELDS = ("in", "out", "cache_read", "cache_write")

    def __init__(self):
        self.seen_ids: set[str] = set()
        self.turn: dict[str, dict[str, int]] = {}
        self.session: dict[str, dict[str, int]] = {}
        self.last_cost = 0.0  # total_cost_usd ist Session-kumulativ

    @staticmethod
    def _fmt(vals: dict) -> str:
        return (f"in={vals['in']} out={vals['out']} "
                f"cache_read={vals['cache_read']} cache_write={vals['cache_write']}")

    def note_assistant(self, event: dict) -> str | None:
        """Usage einer neuen API-Antwort verbuchen; Logzeile oder None."""
        msg = event.get("message") or {}
        mid = msg.get("id") or ""
        usage = msg.get("usage") or {}
        if not mid or mid in self.seen_ids or not usage:
            return None
        self.seen_ids.add(mid)
        model = msg.get("model") or "?"
        vals = {
            "in": int(usage.get("input_tokens") or 0),
            "out": int(usage.get("output_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        }
        for bucket in (self.turn, self.session):
            acc = bucket.setdefault(model, dict.fromkeys(self.FIELDS, 0))
            for key, val in vals.items():
                acc[key] += val
        return f"[TOKENS] {model}: {self._fmt(vals)}"

    def turn_summary(self, result_event: dict) -> list[str]:
        """Zug-Bilanz pro Modell (+ Session-Stand und Modell-Kosten aus
        modelUsage); setzt die Zug-Zähler zurück."""
        model_usage = result_event.get("modelUsage") or {}
        lines = []
        for model in sorted(self.turn):
            line = (f"[TOKENS ZUG] {model}: {self._fmt(self.turn[model])}"
                    f" | Session: {self._fmt(self.session[model])}")
            cost = (model_usage.get(model) or {}).get("costUSD")
            if cost is not None:
                line += f", {cost:.4f} USD"
            lines.append(line)
        self.turn = {}
        return lines


def build_mcp_config(profile: str, npc_id: str, voice: str) -> str:
    cfg = {
        "mcpServers": {
            "dayz": {
                # Tools sofort KOMPLETT laden statt deferred via ToolSearch:
                # Claude Code >= 2.1.121 spart damit die ToolSearch-Schemas
                # (~123k Tokens cache_write pro Session-Start, ~1,5 USD) und
                # die Extra-LLM-Schritte pro Zug fürs Schema-Nachladen
                # (Audit 03.07.). Ältere Versionen ignorieren das Feld.
                "alwaysLoad": True,
                "command": sys.executable,
                "args": [MCP_SERVER, "--profile", profile,
                         "--npc-id", npc_id, "--agent-name", AGENT_NAME,
                         "--voice", voice, "--language", AGENT_LANG,
                         "--outbox", os.path.join(AGENT_HOME, "voice_outbox.jsonl")],
                # Spielername-Variablen an den MCP-Server durchreichen, damit
                # follow/regroup/give_to den Funk-Namen ("Isualc") auf den
                # DayZ-Profilnamen auflösen können (gleiche Env wie der
                # Voice-Stack). Zentral setzbar über ISU_MIC_NAME / ISU_PLAYER_NAME.
                "env": {
                    "ISU_MIC_NAME": os.environ.get("ISU_MIC_NAME", "Player"),
                    "ISU_PLAYER_NAME": os.environ.get("ISU_PLAYER_NAME", ""),
                },
            }
        }
    }
    path = os.path.join(AGENT_HOME, ".isu-mcp.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


def active_map() -> str:
    """Aktuelle Karte aus arena/active_map.txt (chernarus/enoch/sakhal/nasdara)."""
    try:
        with open(ACTIVE_MAP_FILE, "r", encoding="utf-8") as f:
            return (f.readline() or "").strip().lower()
    except OSError:
        return "chernarus"


# Karten-Briefing für den System-Prompt: damit die Modelle wissen, WO sie
# sind, und nicht Chernarus-Orte halluzinieren, wenn sie auf Livonia/Sakhal
# stehen. Bewusst knapp und nur mit Orten, die wirklich existieren; für den
# Rest die klare Regel "nur nennen, was du tatsächlich wahrnimmst".
_MAP_BRIEFINGS = {
    "chernarus": (
        "## AKTUELLE KARTE: Chernarus\n"
        "Du bist in Chernarus (chernarusplus), einer post-sowjetischen Region "
        "mit gemaessigtem Klima: Mischwaelder, Felder, Kuestenstaedte im Sueden, "
        "Bergland im Norden. Bekannte Orte: Chernogorsk und Elektrozavodsk "
        "(Suedkueste), Berezino (Ostkueste), Severograd und Novodmitrovsk "
        "(Norden), Zelenogorsk, Vybor, Stary Sobor (Landesinneres), Kamenka "
        "(Suedwesten). Militaer: Nordwest-Airfield (NWAF), Tisy- und "
        "Veresnik-Basis, Flugplatz Balota. Die Temperatur ist meist unkritisch; "
        "Regen macht nass und ausgekuehlt. {CAMP}"
    ),
    "enoch": (
        "## AKTUELLE KARTE: Livonia\n"
        "Du bist in Livonia (Enoch), einer mitteleuropäischen Region mit "
        "DICHTEN Wäldern und vielen Lichtungen, kühl-gemäßigt. Es gibt "
        "kontaminierte Gaszonen, in denen du ohne Gasmaske stirbst. Bekannte "
        "Orte: Topolin und Nadbor (Norden), Sitnik, Brena, Radunin, Lembork "
        "(Landesinneres). {CAMP} In der Kartenmitte (etwa x7900 z6700, grob "
        "zwischen Radunin und Lembork) ist KEIN "
        "Flugplatz/Airfield - erfinde keinen. Welche Orte wirklich um dich "
        "herum liegen, ermittelst du mit observe.\n"
        "WICHTIG: Das ist NICHT Chernarus. Es gibt KEIN Chernogorsk, kein "
        "Berezino, kein NWAF, KEIN Lukow-Airfield. Verwende KEINE "
        "Chernarus-Ortsnamen. Nenne nur Orte, die du tatsächlich siehst oder "
        "über deine Werkzeuge ermittelst."
    ),
    "sakhal": (
        "## AKTUELLE KARTE: Sakhal (Winter)\n"
        "Du bist auf Sakhal (Frostline), einer arktischen Vulkaninsel: "
        "Dauerschnee, bittere Kälte, ein zentraler Vulkan mit geothermalen "
        "Quellen. DIE KÄLTE IST DIE STÄNDIGE LEBENSGEFAHR - Winterkleidung, "
        "Feuer und warme Getränke sind überlebenswichtig, nasse Kleidung kann "
        "tödlich sein. Halte dich warm und trockne Kleidung am Feuer. "
        "Petropavlovsk-Sakhalinsk ist die größte Hafenstadt "
        "(West-Mitte der Insel). {CAMP} Im Norden ragt der Vulkan auf (höchster Punkt, "
        "Wolf- und Bärengebiet); im Südwesten liegt die große Militärbasis "
        "auf der Burukan-Halbinsel.\n"
        "WICHTIG: Das ist NICHT Chernarus und nicht Livonia. Verwende KEINE "
        "Ortsnamen von anderen Karten. Orientiere dich an dem, was du "
        "tatsächlich siehst und über deine Werkzeuge ermittelst."
    ),
    # Badlands-DLC (Release 15.10.2026). Ortsnamen sind noch nicht
    # verifiziert, darum bewusst keine im Briefing (Halluzinationsschutz).
    "nasdara": (
        "## AKTUELLE KARTE: Nasdara (Badlands)\n"
        "Du bist in Nasdara, einer trockenen Wüsten- und Bergregion im "
        "Nahen Osten (Westen Takistans), komplett Binnenland: KEIN Meer, "
        "kaum Seen. Im Osten Sandflächen, im Westen Gebirge, Highways "
        "laufen von Ost nach West. Es gibt dicht bebaute Städte im "
        "orientalischen Stil, verlassene sowjetische Militärbasen, alte "
        "Ruinen sowie Ölförderanlagen und Raffinerien. Tagsüber wird es "
        "heiß (Sommer bis ~38 Grad), nachts deutlich kühler.\n"
        "WASSER IST DIE KNAPPSTE RESSOURCE: Trink bei jeder Gelegenheit, "
        "fülle Flaschen und Kanister an jedem funktionierenden Brunnen und "
        "trag immer Wasser mit. Viele Brunnen und Gebäude sind kaputt und "
        "lassen sich reparieren. Kleidung eher leicht, überhitzen kostet "
        "Wasser. Die große Stadt im Kartenzentrum liegt bei etwa x8528 "
        "z8669 (Orientierung). {CAMP}\n"
        "WICHTIG: Das ist NICHT Chernarus, nicht Livonia und nicht Sakhal. "
        "Verwende KEINE Ortsnamen anderer Karten und erfinde keine. Nenne "
        "nur Orte, die du tatsächlich siehst oder über deine Werkzeuge "
        "ermittelst."
    ),
}


_MAP_NAMES = {"chernarus": "Chernarus", "enoch": "Livonia", "sakhal": "Sakhal",
              "nasdara": "Nasdara"}


def camp_sentence() -> str:
    """Lager-/Treffpunkt-Satz aus der ECHTEN Runde (MAP-1): früher stand ein
    fester Wert im Briefing (Nasdara x8528 z8669), während das Menü-Lager bei
    11295/12159 lag - Viktor schickte den Spieler 4,5 km in die falsche Stadt."""
    pos = CAMP_POS or SPAWN_POS
    if not pos:
        return ""
    return f"Euer Lager/Treffpunkt in dieser Runde: x{pos[0]:.0f} z{pos[1]:.0f}."


def map_briefing() -> str:
    """Karten-Briefing für die aktive Karte (Fallback Chernarus)."""
    txt = _MAP_BRIEFINGS.get(active_map(), _MAP_BRIEFINGS["chernarus"])
    return txt.replace("{CAMP}", camp_sentence()).replace("  ", " ")


def map_change_notice(home: str) -> str:
    """Kartenwechsel seit dem letzten Lauf erkennen (AGENT_HOME/last_map.txt)
    und die aktuelle Karte dort merken. Rückgabe: Hinweis für den ersten
    Weckruf oder "" (gleiche Karte / allererster Lauf). Hintergrund MAP-1:
    Birgits Gedächtnis sagte SAKHAL, Igors und Konrads LIVONIA - auf Nasdara."""
    cur = active_map() or "chernarus"
    path = os.path.join(home, "last_map.txt")
    old = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = (f.readline() or "").strip().lower()
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(cur + "\n")
    except OSError:
        pass
    if not old or old == cur:
        return ""
    return (f"KARTENWECHSEL: Du bist jetzt auf {_MAP_NAMES.get(cur, cur)} "
            f"(vorher {_MAP_NAMES.get(old, old)}). {camp_sentence()} Einträge "
            f"zur alten Karte in CLAUDE.md/memory sofort als überholt markieren "
            f"oder löschen - Orte, Lager und Koordinaten von dort gelten hier "
            f"nicht. ")


def spawn_claude(mcp_cfg: str, model: str, character_file: str = "",
                 turn_limit: int = 0) -> subprocess.Popen:
    with open(PERSONA_FILE, "r", encoding="utf-8") as f:
        persona = f.read()

    if character_file:
        with open(character_file, "r", encoding="utf-8") as f:
            persona += "\n\n" + f.read()

    # Rollen-Presets sind namens-agnostisch ({NAME}-Platzhalter): der Name
    # kommt aus --name und ist im Spiel-Menü frei wählbar.
    persona = persona.replace("{NAME}", AGENT_NAME)

    # Karten-Briefing anhängen, damit das Modell weiß, auf welcher Karte es
    # steht (Chernarus/Livonia/Sakhal/Nasdara) und keine fremden Ortsnamen halluziniert.
    persona += "\n\n" + map_briefing()

    # Battle-Royale: harter Override ZULETZT, damit er die Friedens-/Bündnis-
    # regeln der Basis-Persona aushebelt (Free-for-all, Funkstille, 1 Leben).
    if AGENT_BR:
        persona += "\n\n" + BR_RULES_BLOCK

    # Freier Survival-Modus: Lockerungs-Override ebenfalls NACH der Persona,
    # damit er ihre Verhaltens-/Prioritäten-Vorgaben aufhebt (Rollenspiel-
    # Grenze, Werkzeug- und Sicherheitsregeln bleiben laut Block bestehen).
    if AGENT_FREE:
        persona += "\n\n" + FREE_RULES_BLOCK

    # Sprach-Override GANZ zuletzt, damit die Ausgabe-Sprache die deutschen
    # Persona-/Karten-Defaults dominiert (Modus "nur Ausgabe").
    lang_block = lang_rules(AGENT_LANG)
    if lang_block:
        persona += "\n\n" + lang_block

    # Terse-Regel: knappes Denken hält das Pro-Zug-Wachstum klein -> seltener
    # Rotation -> mehr Arbeitsgedächtnis. Der eigene Textanteil ist der größte
    # Akkumulator (gemessen ~64% des Conversation-Kontexts). Kostet selbst ~80 Token.
    persona += "\n\n" + TERSE_BLOCK

    if is_native_backend(model):
        return spawn_provider(mcp_cfg, model, persona, AGENT_HOME,
                              turn_limit=turn_limit)

    cli_model, backend_env, backend = resolve_backend(model)

    cmd = [
        *CLI_LAUNCH,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", mcp_cfg,
        # NUR unsere dayz-Tools laden - ohne dieses Flag zieht Claude Code
        # zusätzlich ALLE global konfigurierten MCP-Server des Rechners in
        # die Session (gemessen: 141 Tools, ~77k Tokens Grundprompt statt
        # ~25k). Das kostet bei jedem Modell Geld/Zeit und macht lokale
        # Modelle unbenutzbar.
        "--strict-mcp-config",
        "--model", cli_model,
        "--append-system-prompt", persona,
    ]
    # Zügel gegen Endlos-Züge: Claude beendet den Zug nach N Runden
    # (result subtype=error_max_turns), die Session läuft normal weiter -
    # das Budget gilt PRO User-Message (empirisch verifiziert). Nur so
    # bleibt der Agent für Chat/Funk ansprechbar.
    if turn_limit > 0:
        cmd += ["--max-turns", str(turn_limit)]
    # Exklusive Built-in-Liste (CLI >= 2.1.227, per Binär-Grep in 2.1.227 und
    # 2.1.263 verifiziert): nur Read/Write/Edit aus dem Built-in-Satz, alle
    # anderen - auch künftig neu dazukommende wie ScheduleWakeup - sind weg.
    # MCP-Tools aus --mcp-config betrifft das nicht. Session 24.10.: Sonnet
    # legte sich per ScheduleWakeup 20 min "schlafen", der Harness-Wecker
    # erzeugte Fremdzüge mit Software-Engineering-Prompt. --tools ist
    # variadisch: das NÄCHSTE Argument muss mit "-" beginnen (--allowedTools).
    cmd += ["--tools", "Read,Write,Edit"]
    # Werkzeug-Freigaben: alle dayz-Tools + Gedächtnis-Dateien im agent_home
    for tool in ("mcp__dayz", "Read", "Write", "Edit"):
        cmd += ["--allowedTools", tool]
    # Werkzeug-Diät: alle übrigen Built-ins ganz aus der Session werfen.
    # Das verkleinert den Systemprompt (Tool-Schemas) deutlich - zählt
    # doppelt bei local/-Backends, wo Prompt-Processing Minuten kostet.
    # Gürtel und Hosenträger zu --tools: die bekannten Neuzugänge explizit.
    for tool in ("Task", "Bash", "Glob", "Grep", "WebFetch", "WebSearch",
                 "TodoWrite", "NotebookEdit", "ExitPlanMode", "EnterPlanMode",
                 "BashOutput", "KillShell", "SlashCommand", "Skill",
                 "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
                 "Monitor", "RemoteTrigger", "PushNotification", "SendMessage",
                 "EnterWorktree", "ExitWorktree", "Agent", "ToolSearch"):
        cmd += ["--disallowedTools", tool]

    env = dict(os.environ)
    # Verschachtelungs-Guards entfernen (Lauf aus einer Claude-Code-Session heraus)
    for key in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    # MCP-Werkzeuge dürfen lange blockieren (regroup bis 4 min), aber nicht
    # länger als nötig: aus LONGEST_TOOL_SEC abgeleitet, damit das Timeout
    # nie mit dem Stille-Watchdog (STUCK_QUIET_SEC) kollidiert.
    env["MCP_TIMEOUT"] = "60000"
    env["MCP_TOOL_TIMEOUT"] = str(MCP_TOOL_TIMEOUT_MS)
    # Backend-Env umbiegen (nur für DIESEN Subprozess).
    if backend == "anthropic-api":
        # Expliziter API-Pfad: ANTHROPIC_API_KEY MUSS bleiben (er erzwingt die
        # echte API statt Max-Plan). Nur Bedrock/Vertex aushebeln, die sonst
        # die BASE_URL überschreiben würden.
        for key in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env.pop(key, None)
    elif backend_env:
        # Fremd-Backend (Router/llama): ein gesetzter ANTHROPIC_API_KEY würde
        # den Dummy-Token verdrängen, Bedrock/Vertex würden BASE_URL aushebeln.
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX"):
            env.pop(key, None)
    env.update(backend_env)

    # Sichtbares Denken (Debug-Wunsch 29.08.): nur auf Anthropic-Backends -
    # CCR erzwingt oben bereits MAX_THINKING_TOKENS=0 (OpenAI-400-Falle),
    # llama-server kennt den Parameter nicht.
    # Befund-Update 30.08. (Bisektion gegen den vollen Live-Spawn, CC 2.1.227):
    # alwaysThinkingEnabled LÄSST das Denken nur zu, erzwingt es aber nicht -
    # die adaptiven 5er-Modelle entscheiden pro Zug selbst und überspringen
    # das Denken bei kurzen Spiel-Zügen fast immer (Live-Runde 29.08.: 0
    # [DENKT] über ganze Sessions auf sonnet/opus/fable). Zuverlässig denken
    # sie erst mit --effort max (fable 3/3, sonnet 3/3; xhigh reicht bei
    # sonnet NICHT: 0/3, high ebenso 0). MAX_THINKING_TOKENS bei Thinking AN
    # weiterhin aktiv aus der Umgebung entfernen; "0" bleibt der Aus-Schalter.
    # ISU_THINKING_EFFORT (low|medium|high|xhigh|max) übersteuert die Stufe,
    # falls max zu teuer/langsam wird - unterhalb von max denkt Sonnet dann
    # aber nur noch sporadisch.
    if backend in ("anthropic", "anthropic-api"):
        if cli_model == "claude-fable-5-1":
            # 5.1 has always-on adaptive thinking. Never send the old disabled
            # setting or MAX_THINKING_TOKENS=0; use effort to limit work instead.
            env.pop("MAX_THINKING_TOKENS", None)
            cmd += ["--settings", '{"alwaysThinkingEnabled": true}',
                    "--effort", os.environ.get("ISU_THINKING_EFFORT", "low")]
        elif AGENT_THINKING > 0:
            env.pop("MAX_THINKING_TOKENS", None)
            cmd += ["--settings", '{"alwaysThinkingEnabled": true}',
                    "--effort", os.environ.get("ISU_THINKING_EFFORT", "max")]
        else:
            env["MAX_THINKING_TOKENS"] = "0"

    return subprocess.Popen(
        cmd,
        cwd=AGENT_HOME,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(os.path.join(AGENT_HOME, "journal", "claude_stderr.log"), "a",
                    encoding="utf-8"),
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def send_user(proc: subprocess.Popen, text: str):
    msg = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


# Tool-Aufruf -> kurze, für Zuschauer lesbare Aktion für die Nameplate-
# Gedankenzeile. Nur "sichtbare" Aktionen; observe/intent/say/wait/ToolSearch/
# Read/Edit/find_item liefern "" und aktualisieren die Zeile NICHT (sie behält
# dann ihren letzten sinnvollen Wert statt leer zu werden).
_TOOL_VERBS = {
    "engage": "Kämpft gegen einen Gegner",
    "flee": "Flieht vor einer Bedrohung",
    "move_to": "Ist unterwegs",
    "loot_area": "Durchsucht die Umgebung",
    "loot_corpse": "Durchsucht eine Leiche",
    "loot_container": "Durchsucht einen Behälter",
    "cook_meal": "Kocht etwas zu essen",
    "eat": "Isst etwas",
    "drink": "Trinkt etwas",
    "equip_best": "Rüstet die beste Waffe aus",
    "explore_step": "Erkundet die Gegend",
    "clean_weapon": "Reinigt die Waffe",
    "give_to": "Gibt etwas weiter",
    "drop": "Legt etwas ab",
    "follow": "Folgt dem Spieler",
    "unfollow": "Geht eigene Wege",
}


def _tool_action(name: str, inp: dict) -> str:
    short = (name or "").split("__")[-1]
    if short == "pickup":
        item = (inp.get("classname") or inp.get("item_type") or inp.get("item")
                or inp.get("name") or "")
        return f"Hebt {item} auf" if item else "Hebt etwas auf"
    if short == "wear":
        item = inp.get("classname") or inp.get("item_type") or ""
        return f"Zieht {item} an" if item else "Zieht etwas an"
    return _TOOL_VERBS.get(short, "")


def _result_intent(text: str) -> str:
    """Aus einem Tool-ERGEBNIS eine kurze Gedankenzeile ableiten - deckt
    'munition gefunden' & Co. ab, die im Tool-Aufruf noch nicht stehen."""
    t = " ".join((text or "").split())
    if t.startswith("Aufgehoben:"):
        return t[11:].strip() + " aufgehoben"
    if t.startswith("Eingesammelt:"):
        body = t[13:].strip()
        if len(body) > 50:
            body = body[:48].rstrip(", ") + " u.a."
        return (body + " gefunden") if body else ""
    # equip_best/tactics melden "Ausgerüstet:", _outcome-Pfade "Ausgeruestet:"
    for prefix in ("Ausgerüstet:", "Ausgeruestet:"):
        if t.startswith(prefix):
            w = t[len(prefix):].split("(")[0].strip()
            return (w + " gezogen") if w else ""
    if "Ziel eliminiert" in t or t.startswith("Kampf beendet"):
        return "Gegner erledigt"
    if t.startswith("Gegessen:") or t.startswith("Getrunken:"):
        return t
    return ""


# Login-/Key-Fehler der Claude-CLI. Die CLI liefert sie als "<synthetic>"-
# Assistant-Text plus is_error-Result; bis 16.10. landete der Text 1:1 als
# Sprechblase im Spiel ("Failed to authenticate: OAuth session expired ...")
# und jeder Weckruf verbrannte einen weiteren Fehlzug.
AUTH_ERROR_MARKERS = (
    "failed to authenticate", "oauth session expired", "could not be refreshed",
    "oauth token has expired", "not logged in", "please run /login",
    "invalid api key", "invalid x-api-key", "authentication_error",
)
AUTH_NAMEPLATE = "Gehirn offline: Claude-Login abgelaufen"
AUTH_RECHECK_SEC = 120


def _is_auth_error(text) -> bool:
    t = str(text or "").lower()
    return any(m in t for m in AUTH_ERROR_MARKERS)


def _auth_error_file() -> str:
    # Pfad erst zur Laufzeit bilden: main() setzt AGENT_HOME pro NPC um.
    return os.path.join(AGENT_HOME, "auth_error.txt")


def _set_nameplate_intent(text: str) -> None:
    """Schreibt die Gedankenzeile fürs Namensschild (intent_<id>.txt, vom Mod
    gelesen) - hält sie aktuell, OHNE dass das Gehirn intent() rufen muss (das
    verwaiste sonst minutenlang). Eine Zeile, auf Satz-/Längengrenze gekürzt."""
    if not INTENT_FILE or not text:
        return
    native = " ".join(text.split())
    # Bildschirm-Fassung latinisieren (Stock-Font kann kein CJK/Arabisch/...);
    # native separat ablegen (intent_native_<id>.txt) für den Orchestrator-Sitrep.
    t = transliterate.to_screen(native, AGENT_LANG)
    for sep in (". ", "! ", "? "):
        cut = t.find(sep)
        if 0 < cut <= 75:
            t = t[:cut + 1]
            break
    if len(t) > 80:
        t = t[:78].rstrip() + ".."
    # Lock: BrainReader-Thread und Hauptschleife (Survival-Tick) schreiben
    # über denselben .rtmp-Pfad; gleichzeitig scheiterte os.replace still.
    with _INTENT_LOCK:
        try:
            tmp = INTENT_FILE + ".rtmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(t)
            os.replace(tmp, INTENT_FILE)
            # Native Fassung IMMER mitschreiben (HUD-5): das Lagezentrum liest
            # sie zuerst. Früher nur bei t != native - eine kurze Folgeabsicht
            # ließ dann die ältere lange Absicht minutenlang im Funk stehen.
            npath = _native_intent_path()
            nline = native if len(native) <= 120 else native[:117] + "..."
            with open(npath + ".rtmp", "w", encoding="utf-8") as f:
                f.write(nline)
            os.replace(npath + ".rtmp", npath)
        except OSError:
            pass


def _native_intent_path() -> str:
    """intent_native_<id>.txt neben INTENT_FILE (native Fassung fürs Lagezentrum)."""
    return os.path.join(os.path.dirname(INTENT_FILE),
                        "intent_native_" + os.path.basename(INTENT_FILE)[len("intent_"):])


class BrainReader(threading.Thread):
    """Liest den stream-json-Output des Gehirns KONTINUIERLICH.

    Läuft parallel zur Hauptschleife, damit die Welt-Ereignisse (Chat,
    Funk, Schaden) schon WÄHREND eines laufenden Zuges eingespeist werden
    können - vorher war der Agent für die gesamte Zugdauer taub (bei
    langen move_to-Ketten zweistellige Minuten). result-Events landen in
    self.results; None bedeutet: Prozess weg.
    """

    def __init__(self, proc: subprocess.Popen, journal: Journal,
                 tracker: TokenTracker):
        super().__init__(daemon=True)
        self.proc = proc
        self.journal = journal
        self.tracker = tracker
        self.results: queue.Queue = queue.Queue()
        self.dead = False
        # Zeitstempel der letzten Brain-Ausgabe (Tool/Text/Zug-Ende). Dient dem
        # Hauptloop als zuverlässiges "Gehirn arbeitet"-Signal - robuster als der
        # pending-Zähler, der durch Mid-Turn-Einspeisung verdriften kann.
        self.last_activity = time.monotonic()
        # Rollender Puffer der letzten Gehirn-Kommentare (eigener Gedankenfaden),
        # um ihn über eine Session-Rotation hinweg zu retten (Lever 2).
        self.recent: list[str] = []
        # Zuletzt bekanntes Follow-Ziel (aus den Tool-Aufrufen abgeleitet) -
        # kommt in die Handover-Notiz der Kontext-Rotation. follow endet in
        # der Mod auch durch eigene Bewegung -> move_to/flee/engage leeren.
        self.follow_target = ""
        # Kontextgröße laut LETZTEM einzelnen API-Schritt (cache_read +
        # cache_write + input). Das ist die echte Fenstergröße für die
        # Rotations-Schwelle. Die Zug-Summe aus dem result-Event taugt NICHT:
        # sie addiert cache_read über alle Schritte des Zuges und riss die
        # Schwelle bei jedem mehrstufigen Zug (Rotations-Thrashing 02.07.).
        self.last_step_ctx = 0
        # Letzter Login-/Key-Fehler der CLI (main pausiert dann das Gehirn).
        self.auth_error = ""
        # Zug-Erkennung aus dem Ausgabestrom (TurnGate): turn_open springt bei
        # jedem assistant/user-Event (und system/init) an und fällt beim
        # result. last_result_at = monotonic des letzten result-Events.
        # Nur dieser Thread schreibt; der Hauptthread liest (Ausnahme:
        # TurnGate.watchdog setzt nach 300 s Stille zurück).
        self.turn_open = False
        self.last_result_at = 0.0
        # Kosten-Basislinie DIESER CLI-Session: total_cost_usd ist pro Session
        # kumulativ. Früher lief der Vergleich gegen den runnerweiten Tracker,
        # nach jedem Prozesstausch entstanden negative Zugpreise und der
        # erste Zug der neuen Session fehlte in round_cost (DR-8/COST-1).
        self.session_cost = 0.0
        self.tools_logged = False

    def run(self):
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                self.journal.log("!! Gehirn-Prozess hat stdout geschlossen.")
                self.dead = True
                self.results.put(None)
                return
            line = line.strip()
            if not line:
                continue
            self.last_activity = time.monotonic()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            # Zug offen: jedes assistant/user-Event gehört zu einem laufenden
            # Zug, system/init markiert den Zugbeginn. Andere system-Events
            # (Hinweise, Status) können einem result nachlaufen und dürfen den
            # Zug NICHT wieder öffnen - sonst blockierte der Runner bis zum
            # 300-s-Watchdog.
            if etype in ("assistant", "user") or (
                    etype == "system" and event.get("subtype") == "init"):
                self.turn_open = True

            if etype == "system" and event.get("subtype") == "init":
                self._log_tools(event.get("tools"))

            if etype == "assistant":
                msg_text = ""
                msg_action = ""
                msg_think = ""
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "thinking" and block.get("thinking", "").strip():
                        # Extended-Thinking-Block: der interne Denkprozess des
                        # Modells (nur Anthropic-Backends, s. AGENT_THINKING).
                        # Fürs Journal auf EINE Zeile glätten - mehrzeilige
                        # Einträge brächen das [HH:MM:SS]-Zeilenformat, auf dem
                        # Spectator/league_report-Parser aufsetzen.
                        think = " ".join(block["thinking"].split())
                        if len(think) > 4000:
                            think = think[:3997] + "..."
                        self.journal.log("[DENKT]  " + think)
                        msg_think = think
                    elif block.get("type") == "redacted_thinking":
                        self.journal.log("[DENKT]  (verschlüsselter Denkblock - "
                                         "von der API redigiert)")
                    elif (block.get("type") == "text" and block.get("text", "").strip()
                          and event.get("message", {}).get("model") == "<synthetic>"):
                        # Von der CLI erzeugter Fehlertext, keine Modell-Antwort:
                        # nie als Sprechblase/Gedanke ins Spiel geben.
                        err = block["text"].strip()
                        self.journal.log("[SYSTEM] " + err)
                        if _is_auth_error(err):
                            self.auth_error = err
                            _set_nameplate_intent(AUTH_NAMEPLATE)
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        self.journal.log(f"[{AGENT_NAME.upper()}] " + block["text"].strip())
                        msg_text = block["text"].strip()
                    elif block.get("type") == "tool_use":
                        args = json.dumps(block.get("input", {}), ensure_ascii=False)
                        self.journal.log(f"[TOOL]   {block.get('name', '?')} {args[:140]}")
                        act = _tool_action(block.get("name", ""), block.get("input", {}))
                        if act:
                            msg_action = act
                        # Follow-Ziel mitschreiben (für die Rotations-Handover)
                        short = (block.get("name") or "").split("__")[-1]
                        if short == "follow":
                            tgt = str((block.get("input") or {})
                                      .get("player_name") or "").strip()
                            self.follow_target = tgt or "dem naechsten Spieler"
                        elif short in ("unfollow", "move_to", "flee", "engage"):
                            self.follow_target = ""
                # Nameplate-Gedankenzeile live halten: Gehirn-Kommentar bevorzugt
                # (reicher), sonst die abgeleitete Aktion. Reine observe/Read-Züge
                # liefern beides leer -> Zeile bleibt unverändert (nicht leer).
                # Neu: liefert der Schritt NUR einen Thinking-Block (typisch: das
                # Denken kommt als eigenes Event VOR Text/Tool), zeigt die Zeile
                # den Denkanfang - so ist der NPC schon "sichtbar am Überlegen",
                # bevor er spricht oder handelt (Debug-Wunsch 29.08.).
                _set_nameplate_intent(msg_text or msg_action
                                      or (("denkt: " + msg_think[:110]) if msg_think else ""))
                if msg_text:
                    self.recent.append(msg_text)
                    if len(self.recent) > 4:
                        self.recent.pop(0)
                token_line = self.tracker.note_assistant(event)
                if token_line:
                    self.journal.log(token_line)
                usage = event.get("message", {}).get("usage") or {}
                step_ctx = (int(usage.get("cache_read_input_tokens") or 0)
                            + int(usage.get("cache_creation_input_tokens") or 0)
                            + int(usage.get("input_tokens") or 0))
                if step_ctx > 0:
                    self.last_step_ctx = step_ctx

            elif etype == "user":
                content = event.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_result":
                            text = _tool_result_text(block)
                            if text:
                                self.journal.log("[WELT]   " + text[:160].replace("\n", " | "))
                                ri = _result_intent(text)
                                if ri:
                                    _set_nameplate_intent(ri)

            elif etype == "result":
                cost = event.get("total_cost_usd")
                cost_known = cost is not None
                if cost is None:
                    cost = self.session_cost
                dur = event.get("duration_ms", 0) / 1000.0
                # Delta gegen die Basislinie DIESES Readers (= dieser CLI-
                # Session). Auch ein verspätetes result des alten Readers nach
                # einem Prozesstausch bucht so nur seinen eigenen Zuwachs.
                turn_cost = cost - self.session_cost
                self.session_cost = cost
                self.tracker.last_cost = cost
                # Rundenkosten über Session-Wechsel hinweg kumulieren und für
                # den Supervisor exportieren (Arena-Menü: "Round cost").
                if turn_cost > 0:
                    self.tracker.round_cost = getattr(self.tracker, "round_cost", 0.0) + turn_cost
                    _write_round_cost(self.tracker.round_cost)
                note = ""
                if event.get("subtype") == "error_max_turns":
                    note = ", ZUG AM AKTIONSLIMIT GEKAPPT"
                elif event.get("is_error"):
                    note = ", FEHLER: " + str(event.get("result") or event.get("subtype"))[:180]
                    if _is_auth_error(event.get("result")):
                        self.auth_error = str(event.get("result"))
                cost_text = (f"+{turn_cost:.4f} USD (Session {cost:.4f} USD)"
                             if cost_known else "Kosten nicht gemeldet; Tokenverbrauch siehe Journal")
                self.journal.log(f"[ZUG ENDE] {dur:.0f}s, {cost_text}, "
                                 f"turns={event.get('num_turns', '?')}{note}")
                for line2 in self.tracker.turn_summary(event):
                    self.journal.log(line2)
                self.turn_open = False
                self.last_result_at = time.monotonic()
                self.results.put(event)

    def _log_tools(self, tools) -> None:
        """Einmal pro Session die Werkzeugliste ins Journal ([TOOLS]): ein
        künftiger CLI-Release mit neuen Built-ins fällt so sofort auf."""
        if self.tools_logged or not isinstance(tools, list):
            return
        self.tools_logged = True
        names = [str(t) for t in tools]
        mcp = [t for t in names if t.startswith("mcp__")]
        other = [t for t in names if not t.startswith("mcp__")]
        shown = ", ".join(other[:15])
        if len(other) > 15:
            shown += f" ... (+{len(other) - 15})"
        line = f"[TOOLS] {len(names)} Werkzeuge | Built-ins: {shown or '-'}"
        if mcp:
            line += f" | MCP: {len(mcp)} ({mcp[0].rsplit('__', 1)[0]}__*)"
        self.journal.log(line)
        banned = [t for t in other if t in ("ScheduleWakeup", "CronCreate",
                                             "Monitor", "Agent", "Bash")]
        if banned:
            self.journal.log("!! [TOOLS] unerwünschte Built-ins trotz Werkzeug-Diät: "
                             + ", ".join(banned))


def _write_round_cost(total: float | None):
    """Kumulierte Kosten dieses Runners (über alle Sessions der Runde) in
    <agent_home>/round_cost.txt exportieren. Der Supervisor summiert die
    Dateien aller laufenden Agenten und hängt sie an die Statuszeile an."""
    try:
        with open(os.path.join(AGENT_HOME, "round_cost.txt"), "w", encoding="utf-8") as f:
            f.write("unknown\n" if total is None else f"{total:.4f}")
    except OSError:
        pass


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return " ".join(p for p in parts if p)
    return ""


# Raubtier-Heuristik (spiegelt IsuBridge.IsPredator): Wolf/Bär sind Gegner,
# passive Tiere (Kuh, Reh, Ziege) nicht. Nur für kind=="animal" aufrufen.
_PREDATOR_HINTS = ("wolf", "bear", "canislupus", "ursus")


def _is_predator(classname: str) -> bool:
    c = (classname or "").lower()
    for h in _PREDATOR_HINTS:
        if h in c:
            return True
    return False


# Anzeigenamen der Krankheits-Erreger (state.npc.disease.agents, von der Mod
# geschrieben - nur Erreger ÜBER der Schwelle stehen überhaupt im State).
_DISEASE_NAMES = {
    "cholera": "Cholera (schlechtes Wasser)",
    "salmonella": "Salmonellen (verdorbenes/rohes Essen)",
    "influenza": "Grippe",
    "wound": "Wundinfektion",
    "brain": "Gehirnkrankheit (Kuru)",
}


def _squad_lines(own_id: str, px: float, pz: float, details: bool = False) -> str:
    """Squad-Zeile laut Schnittstelle C2 (bridge.squad_lines: frische
    arena/squad_state.json, Distanz/Richtung relativ zu px/pz). Fehlt die
    Funktion (älterer bridge-Stand) oder scheitert sie: leer, kein Crash.
    details=True: mit Bewegungsrichtung und Absicht (Lagezentrum-Neuaufbau)."""
    fn = getattr(bridge_mod, "squad_lines", None)
    if fn is None:
        return ""
    try:
        if details:
            return str(fn(own_id, float(px), float(pz), details=True) or "")
        return str(fn(own_id, float(px), float(pz)) or "")
    except Exception:
        return ""


# Ein lebender Eintrag der Squad-Zeile (C2): "<Name> <Distanz> m <Kompass>
# (x<X> z<Z>, HP<h>[, im KAMPF])"
_SQUAD_PART_RE = re.compile(
    r"^(?P<name>.+?) (?P<d>\d+(?:[.,]\d+)?) m (?P<dir>[A-Z]{1,3}) "
    r"\(x(?P<x>-?\d+(?:\.\d+)?) z(?P<z>-?\d+(?:\.\d+)?)")


def _nearest_mate(line: str) -> dict | None:
    """Nächster LEBENDER Kamerad aus einer Squad-Zeile (C2) oder None."""
    best = None
    for part in (line or "").split("; "):
        m = _SQUAD_PART_RE.match(part.strip())
        if not m:
            continue
        try:
            d = float(m.group("d").replace(",", "."))
            entry = {"name": m.group("name"), "dist": d, "dir": m.group("dir"),
                     "x": float(m.group("x")), "z": float(m.group("z"))}
        except ValueError:
            continue
        if best is None or d < best["dist"]:
            best = entry
    return best


def _sitrep_trigger(text: str) -> str:
    """Auslöser-Teil eines Orchestrator-Sitreps ("... Ausloeser: X | Y.
    (Lageinfo, ...)"); "" wenn keiner drinsteht. Funktioniert mit beiden
    Köpfen ("Squad-Lage:" und "Squad-Lage (Stand HH:MM:SS):", C9)."""
    if "Ausloeser: " not in (text or ""):
        return ""
    trig = text.split("Ausloeser: ", 1)[1]
    trig = trig.split(" (Lageinfo", 1)[0]
    return trig.strip().rstrip(".").strip()


def _strip_sitrep_stamp(text: str) -> str:
    """Uhrzeit aus dem neuen Sitrep-Kopf entfernen (C9): run_agent misst das
    Alter selbst und nennt es als "vor N s" - zwei Zeitangaben verwirren."""
    return re.sub(r"^Squad-Lage \(Stand [0-9:]+\):", "Squad-Lage:", text or "")


def _mark_self(text: str, name: str) -> str:
    """Den eigenen Eintrag im Sitrep als "DU (<Name>)" kennzeichnen."""
    if not name:
        return text
    return re.sub(r"(?<=: |; )" + re.escape(name) + r"(?=[ :])",
                  f"DU ({name})", text or "")


_ADDR_JOIN_RE = re.compile(r"\s+(?:und|and|&)\s+", re.IGNORECASE)


def _chat_addressee(text: str, known_l: set, own_l: str) -> str:
    """Adressat einer Chatzeile: die führenden Komma-Segmente, solange sie
    BEKANNTE Namen sind (Roster + Spieler-Aliase, auch "Viktor und Igor").
    Steht der eigene Name darunter, gilt die Zeile mir ("") - "Viktor,
    Birgit, folgt mir" ist für Birgit ein Befehl, keine bloße Erwähnung.
    Sonst der erste genannte Name. Ohne Namensliste würde aus "Gut, ich
    komme" ein Adressat "Gut"."""
    if "," not in (text or ""):
        return ""
    names: list[str] = []
    for seg in text.split(","):
        parts = [p.strip().strip("'\"") for p in _ADDR_JOIN_RE.split(seg.strip())]
        if not all(p and len(p.split()) <= 2 and p.lower() in known_l
                   for p in parts):
            break
        names += parts
    if not names or own_l in (n.lower() for n in names):
        return ""
    return names[0]


# Koordinaten im Funk: "x=11088", "x11088", aber auch fürs TTS ausgeschrieben
# ("x elftausendachtundachtzig"), so kam Viktors Funk am 24.10. an.
_COORD_RE = re.compile(
    r"(?<![a-zäöüß])[xz]\s*[=:]?\s*(?:-?\d{2,5}\b|[a-zäöüß]*(?:tausend|hundert))",
    re.IGNORECASE)


def _has_coords(text: str) -> bool:
    return bool(_COORD_RE.search(text or ""))


def fold_wakeup(msg: str, info: str = "", events: list[str] | None = None,
                smalltalk: str = "", events_title: str = "AKTUELL") -> str:
    """Digest, Geplauder und gepufferte Ereignisse IN eine Weckruf-Nachricht
    falten (Start, Rotations-Handover, Respawn) statt eine zweite Nachricht
    hinterherzuschicken - die zweite verschmolz sonst mit dem ersten Zug.
    Die eigentliche Ansage steht vorn, Lage und Ereignisse folgen."""
    parts = [msg]
    if info:
        parts.append("LAGE (nur zur Kenntnis): " + info)
    if smalltalk:
        parts.append("FUNKGEPLAUDER der anderen (nur zur Info, keine Antwort "
                     "nötig): " + smalltalk)
    if events:
        parts.append(events_title + ":\n- " + "\n- ".join(events))
    return "\n\n".join(parts)


def format_buffered(events: list[str], stamps: dict, now: float,
                    watcher=None) -> list[str]:
    """Gepufferte Ereignisse für die Zustellung aufbereiten (VI-9, RESP-5):
    Dubletten zusammenfassen, "(vor N s) " vor alte Einträge, alte
    Gefahrenmeldungen gegen den aktuellen Stand prüfen ("inzwischen vorbei"),
    STILLSTAND mit der ECHTEN Stehdauer - oder verwerfen, wenn der NPC
    inzwischen läuft. stamps: Ereignis -> monotonic der Pufferung."""
    out: list[str] = []
    stand = watcher.stationary_secs() if watcher is not None else 0.0
    for e in dict.fromkeys(events):
        age = max(0.0, now - float(stamps.get(e, now)))
        if e.startswith("STILLSTAND:"):
            if watcher is not None and stand < STILL_WARN_SEC:
                continue    # steht nicht mehr: Mahnung ist überholt
            if watcher is not None:
                e = re.sub(r"seit \d+ Sekunden", f"seit {stand:.0f} Sekunden", e, count=1)
            out.append(e)
            continue
        if age > EVENT_RECHECK_SEC and watcher is not None:
            over = False
            if e.startswith("GEFAHR: Infizierter nur noch"):
                over = watcher.infected_min_now > INFECTED_NEAR + 10
            elif "Gegner dicht beieinander" in e:
                over = watcher.hostiles_now < CLUSTER_COUNT
            elif e.startswith("GEFAHR: Raubtier"):
                over = watcher.predator_min_now > PREDATOR_NEAR + 15
            if over:
                out.append(f"(vor {age:.0f} s gemeldet, inzwischen vorbei) {e}")
                continue
            if e.startswith("DU BIST UMGEKIPPT") and not watcher.uncon_now:
                out.append(f"(vor {age:.0f} s) DU WARST KURZ BEWUSSTLOS, jetzt wieder wach.")
                continue
        if age > EVENT_AGE_NOTE_SEC:
            out.append(f"(vor {age:.0f} s) {e}")
        else:
            out.append(e)
    return out


RESPAWN_NEAR_PLAYER_M = 22.0    # Koop-Respawn so weit neben dem Menschen
RESPAWN_THREAT_CLEAR_M = 60.0   # kein Infizierter/Raubtier näher am Menschen


def respawn_spawn_point(dead_state: dict, roster_names) -> tuple:
    """Koop-Respawn neben dem nächsten MENSCHEN aus dead_state.nearby (HUD-2/
    DR-4, Entscheidung des Hauptagenten): ~22 m vom Spieler, auf der Linie
    Spieler -> Leiche (dort war begehbarer Boden). Rückgabe (spawn_pos, mensch)
    oder (None, None): kein Mensch in Leichennähe oder ein Infizierter/
    Raubtier < 60 m am Menschen - dann wie bisher am Lager."""
    near = dead_state.get("nearby") or []
    roster_l = {str(n).lower() for n in (roster_names or [])}
    humans = [e for e in near if e.get("kind") == "player"
              and str(e.get("name") or "").lower() not in roster_l]
    if not humans:
        return None, None
    try:
        h = min(humans, key=lambda e: float(e.get("distance", 9999) or 9999))
        hx = float(h["x"])
        hz = float(h["z"])
    except (KeyError, TypeError, ValueError):
        return None, None
    for e in near:
        kind = e.get("kind")
        if kind == "infected" or (kind == "animal" and _is_predator(e.get("classname", ""))):
            try:
                d = ((float(e["x"]) - hx) ** 2 + (float(e["z"]) - hz) ** 2) ** 0.5
            except (KeyError, TypeError, ValueError):
                continue
            if d < RESPAWN_THREAT_CLEAR_M:
                return None, None
    dead_npc = dead_state.get("npc") or {}
    try:
        dx = float(dead_npc.get("pos_x", hx)) - hx
        dz = float(dead_npc.get("pos_z", hz)) - hz
    except (TypeError, ValueError):
        dx = dz = 0.0
    d = (dx * dx + dz * dz) ** 0.5
    if d > 3.0:
        ux, uz = dx / d, dz / d
    else:
        ux, uz = 0.7071, 0.7071
    return (hx + ux * RESPAWN_NEAR_PLAYER_M, hz + uz * RESPAWN_NEAR_PLAYER_M), h


def weapon_status(state: dict) -> tuple[bool, str]:
    """(bewaffnet?, beste Waffe) aus Inventar + Hand - Feuerwaffe oder echte
    Nahkampfwaffe (Werkzeuge wie Pickaxe/Shovel zählen nicht)."""
    names = [str(i.get("classname") or "") for i in (state.get("inventory") or [])
             if float(i.get("health", 100) or 0) > 5.0]
    hands = str(((state.get("npc") or {}).get("in_hands")) or "")
    if hands:
        names.append(hands)
    best, score = "", 0
    for cn in names:
        s = tactics.classify_weapon(cn)
        if not s and not tactics.is_tool(cn):
            s = tactics.classify_melee(cn)
        if s > score:
            best, score = cn, s
    return bool(best), best


def build_respawn_wakeup(new_state: dict, dead_npc: dict, carry_recent: list,
                         died_in_vehicle: bool = False, died_uncon: bool = False,
                         woke_near: dict | None = None, squad_line: str = "",
                         lost: list | None = None, ground: list | None = None,
                         rally_hint: str = "", br: bool = False,
                         lang: str = "de") -> str:
    """Respawn-Weckruf (RESP-6, VI-M6, DR-7, DR-4): Todesumstand, letzte
    Gedanken, Aufwachort, Squad JETZT (relativ zur neuen Position), Waffen-
    status, Verlorenes/Bodenliegendes, Leiche und ein klarer Plan. Birgit
    wusste am 24.10. nichts davon, lief unbewaffnet per regroup ins Leere
    und wartete danach 20 min an einer falschen Stelle."""
    npc = new_state.get("npc") or {}
    try:
        nx = float(npc.get("pos_x", 0.0))
        nz = float(npc.get("pos_z", 0.0))
    except (TypeError, ValueError):
        nx = nz = 0.0
    t = "DU BIST GESTORBEN UND NEU ERWACHT."
    if (lang or "de").lower() == "de":
        t += " Denke und funke auf Deutsch."
    t += (" Zuerst observe - alte Lage (Position/Ziele/Gegner/Pläne) gilt NICHT "
          "mehr.")
    cause = ""
    if died_in_vehicle:
        cause = "als Mitfahrer im Fahrzeug (Unfall/Sturz)"
    elif died_uncon:
        cause = "nach Bewusstlosigkeit"
    if cause:
        t += f" Todesumstand: {cause}."
    last = " | ".join(str(s)[:100] for s in (carry_recent or [])[-3:])
    if last:
        t += f" Deine letzten Gedanken vor dem Tod: {last}."
    human = (woke_near or {}).get("name") or "deinem Menschen"
    if woke_near:
        try:
            hx = float(woke_near.get("x"))
            hz = float(woke_near.get("z"))
            hd = ((hx - nx) ** 2 + (hz - nz) ** 2) ** 0.5
        except (TypeError, ValueError):
            hx, hz, hd = nx, nz, 0.0
        if hd <= 60.0:
            t += f" Du bist neben {human} erwacht (x={nx:.0f} z={nz:.0f})."
        else:
            # adopt_nearest kann einen Körper bis 200 m vom Zielpunkt übernehmen
            t += (f" Du bist bei x={nx:.0f} z={nz:.0f} erwacht; {human} stand "
                  f"zuletzt {hd:.0f} m {_compass(hx - nx, hz - nz)} von dir "
                  f"(x={hx:.0f} z={hz:.0f}).")
    else:
        t += f" Du bist am Lager erwacht (x={nx:.0f} z={nz:.0f})."
    mate = None
    if not br:
        if squad_line:
            t += f" Squad jetzt: {squad_line}."
            mate = _nearest_mate(squad_line)
        else:
            t += " Wo der Squad steht, ist unbekannt."
    armed, best = weapon_status(new_state)
    if armed:
        t += f" Bewaffnet: ja ({best})."
    else:
        t += " Bewaffnet: NEIN - du bist UNBEWAFFNET."
    if ground:
        t += " Am Boden neben dir (aufheben!): " + ", ".join(ground[:8]) + "."
    if lost:
        t += (" Nicht wiederhergestellt (liegt bei deiner Leiche): "
              + ", ".join(lost[:8]) + ".")
    if dead_npc.get("spawned"):
        try:
            cx = float(dead_npc.get("pos_x", 0.0))
            cz = float(dead_npc.get("pos_z", 0.0))
            cd = ((cx - nx) ** 2 + (cz - nz) ** 2) ** 0.5
            t += (f" Deine Leiche: x={cx:.0f} z={cz:.0f} ({cd:.0f} m "
                  f"{_compass(cx - nx, cz - nz)}; loot_corpse, wenn der Weg es "
                  f"wert ist).")
        except (TypeError, ValueError):
            pass
    t += rally_hint
    if not br and not rally_hint:
        if woke_near:
            plan = f" Plan: bleib bei {human} bzw. geh hin und schließ dich der Gruppe an."
        elif mate and mate["dist"] > 60:
            plan = (f" Plan: travel_to zum nächsten Kameraden ({mate['name']}, "
                    f"x={mate['x']:.0f} z={mate['z']:.0f}) - läuft im Hintergrund, "
                    f"NICHT regroup. Treffpunkt ist ab jetzt der Squad, nicht "
                    f"dein Aufwachort.")
        elif mate:
            plan = f" Plan: dein Squad ist in der Nähe ({mate['name']}), schließ dich an."
        else:
            plan = (" Plan: frag per say nach der Squad-Position, dann travel_to "
                    "dorthin (läuft im Hintergrund), NICHT regroup.")
        if not armed:
            plan += " Unbewaffnet heißt nicht warten: unterwegs eine Waffe suchen (loot_area)."
        t += plan
    t += (" Erinnerungen (CLAUDE.md/memory) bleiben; trage die Todesursache als "
          "Lektion ein, wenn du sie kennst.")
    return t


class EventWatcher:
    """Vergleicht state-Snapshots und erzeugt Weck-Ereignisse."""

    def __init__(self, bridge: Bridge, greet_initial: bool = False):
        self.bridge = bridge
        state = bridge.read_state() or {}
        npc = state.get("npc", {})
        self.last_chat_id = max([m.get("id", 0) for m in state.get("chat", [])] or [0])
        self.roster_names = load_roster_names()
        # Bereits sichtbare Spieler vormerken, damit ein frisch erzeugter Watcher
        # (z.B. nach Respawn) sie nicht fälschlich als "neu gesichtet" meldet und
        # den frischen Brain damit zuspammt. AUSNAHME Runden-Start
        # (greet_initial=True, nur der allererste Watcher): steht der Spieler
        # schon da, wenn die Runde beginnt, soll der NPC ihn begrüßen statt
        # ihn stumm als Bestand zu verbuchen (Befund 02.07.: keine Begrüßung,
        # weil Clausi beim START-Klick bereits am Lager wartete).
        self.known_players: set[str] = set(
            (e.get("name") or e.get("classname", "?"))
            for e in state.get("nearby", []) if e.get("kind") == "player"
        )
        self.greet_pending: set[str] = set()
        if greet_initial:
            self.greet_pending = set(
                n for n in self.known_players if n not in self.roster_names)
        self.last_health = npc.get("health", 100.0)
        self.last_blood = npc.get("blood", 5000.0)
        self.water_warned = False
        self.energy_warned = False
        self.infected_warned = False
        self.predator_warned = False
        self.cluster_warned = False
        self.cold_warned = False
        # Aus dem State ableiten statt hart True: nach einem Respawn wird der
        # Watcher neu gebaut - stand er hart auf True und der Körper war noch
        # nicht alive, meldete der erste poll() sofort wieder "DU BIST
        # GESTORBEN" (künstliche Todes-Serie -> DEATH_LOOP-Bremse).
        self.was_alive = bool((state.get("npc") or {}).get("alive"))
        self.uncon_warned = False
        self.bleed_warned = False
        # Blutungs-Reflex der Mod (Selbstverband, state.npc.auto_bandages):
        # Zählerstand merken, damit ein Verbrauch als Info gemeldet wird,
        # und den Beginn der Blutung, um dem Reflex kurz Vorrang zu lassen.
        self.auto_bandages = int(npc.get("auto_bandages") or 0)
        self.bleed_since = 0.0
        self.in_vehicle = bool((state.get("npc") or {}).get("in_vehicle"))
        # Peer-Smalltalk (NPC-Geplauder, das mich nicht anspricht): nur als
        # Digest sammeln, NICHT pro Nachricht einen Zug auslösen
        self.smalltalk: list[str] = []
        # INFO-DIGEST (analog Smalltalk): Routine-Sitrep des Lagezentrums +
        # Info-Ereignisse (_INFO_PREFIXES). Vom Lagezentrum zählt nur der
        # NEUESTE Sitrep (alte sind durch neue obsolet). Der Digest löst
        # selbst NIE einen Zug aus - drain_info hängt ihn an den nächsten.
        # Seit 24.10. mit Erzeugungszeit ({"t": epoch, "text": ...}): bei der
        # Zustellung wird die Lage aus squad_state.json NEU aufgebaut.
        self.info_sitrep: dict | None = None
        # Nicht weckende Lagezentrum-Meldungen, die KEIN Squad-Sitrep sind
        # (z. B. Missionsansage mit "prio": false, missions.broadcast): sie
        # werden weder vom nächsten Sitrep überschrieben noch durch die
        # Squad-Zeile ersetzt, sondern wörtlich zugestellt (max. 3).
        self.info_notes: list[dict] = []
        self.info_lines: list[str] = []
        # Kamerad-gefallen-Dedupe: Name (lower) -> Zeitpunkt der Meldung
        self.fallen_recent: dict[str, float] = {}
        # REISE: travel_event_<id>.json vom travel_to-Thread in dayz_mcp
        # (gleiches Verzeichnis wie intent_<id>.txt). Eine Altlast aus der
        # vorigen Session / dem vorigen Leben verwerfen, wie beim Stale-Intent.
        self.travel_file = os.path.join(bridge.dir,
                                        f"travel_event_{bridge.npc_id}.json")
        try:
            if os.path.exists(self.travel_file):
                os.remove(self.travel_file)
        except OSError:
            pass
        # WETTER (state.world, tolerant): Sonnenstand-Basis ohne Weckruf setzen,
        # sonst meldet jeder Runner-Start "die Nacht bricht herein".
        world0 = state.get("world")
        self.last_sun = (str(world0.get("sun") or "")
                         if isinstance(world0, dict) else "")
        self.rain_warned = False
        self.wet_warned = False
        # KRANKHEIT: pro Erreger gelatcht, bis er wieder unter der Schwelle liegt
        self.disease_warned: set[str] = set()
        # KAMERAD GEFALLEN: Peer-Status (id -> zuletzt gesehen lebendig)
        self.roster_agents = load_roster_agents()
        self.peer_alive: dict[str, bool] = {}
        # LAGERFEUER-RUNDE: streng gedrosselt (Anlaufzeit + 30-min-Cooldown)
        self.campfire_next = time.monotonic() + CAMPFIRE_GRACE_SEC
        # STILLSTAND: Anker-Position + Zeitpunkte des Stillstands-Wächters
        self.still_anchor: tuple[float, float] | None = None
        self.still_since = time.monotonic()
        self.still_last_warn = 0.0
        # Horden-Hysterese (VI-M3): die Cluster-Warnung re-armt erst nach
        # CLUSTER_REARM_SEC ohne Cluster - beim Kampf in einer Horde wackelte
        # die Zahl 3->2->3 und jede Wiederholung setzte das Abbruch-Flag neu.
        self.cluster_clear_since = 0.0
        # Rohwerte des letzten Polls (ohne Latch) für die Neuprüfung alter
        # Gefahrenmeldungen bei der Zustellung (VI-9).
        self.infected_min_now = 9999.0
        self.predator_min_now = 9999.0
        self.hostiles_now = 0
        self.uncon_now = bool(npc.get("unconscious"))
        self.pos_now = (float(npc.get("pos_x", 0.0) or 0.0),
                        float(npc.get("pos_z", 0.0) or 0.0))
        # SCHÜSSE (C4, npc.shots = Schüsse dieses Körpers seit Spawn): Basis
        # ohne Meldung setzen; Zuwachs sammelt sich bis zum nächsten Weckruf
        # und fährt dann als EINE Info-Zeile mit (weckt nie selbst).
        self.shots = int(npc.get("shots") or 0)
        self.shots_unreported = 0
        self.shot_weapons: list[str] = []
        # Bekannte Namen für die Chat-Adressaten-Erkennung: Roster + Spieler-
        # Aliase aus arena/players.json (case-insensitiv).
        self.known_names_l = {n.lower() for n in self.roster_names}
        if players_registry is not None:
            try:
                for p in players_registry.all_players():
                    for n in [p.get("funk"), p.get("dayz")] + list(p.get("aliases") or []):
                        if isinstance(n, str) and n.strip():
                            self.known_names_l.add(n.strip().lower())
            except Exception:
                pass

    def drain_smalltalk(self) -> str:
        if not self.smalltalk:
            return ""
        digest = " | ".join(self.smalltalk)
        self.smalltalk = []
        return digest

    def drain_info(self) -> str:
        """Info-Digest abholen und leeren: max ~5 Zeilen (neuester
        Lagezentrum-Sitrep + bis zu 3 Info-Ereignisse + Schuss-Zeile). Fährt
        nur als Prefix mit einem echten Weckruf mit, löst NIE selbst einen aus."""
        lines = []
        sit = self.info_sitrep
        self.info_sitrep = None
        if sit:
            line = self._sitrep_line(sit)
            if line:
                lines.append(line)
        for note in self.info_notes:
            line = self._sitrep_line(note)
            if line:
                lines.append(line)
        self.info_notes = []
        lines += self.info_lines
        self.info_lines = []
        if self.shots_unreported:
            weapons = ", ".join(self.shot_weapons) or "Waffe unbekannt"
            lines.append(f"SCHÜSSE: Du hast seit dem letzten Weckruf "
                         f"{self.shots_unreported}x geschossen ({weapons}) - das "
                         f"war LAUT, Infizierte in der Nähe hören es.")
            self.shots_unreported = 0
            self.shot_weapons = []
        return "\n".join(lines)

    def _sitrep_line(self, sit: dict, now: float | None = None) -> str:
        """Lagezentrum-Sitrep bei der ZUSTELLUNG neu aufbauen (VI-2/RESP-1):
        Positionen aus der frischen squad_state.json relativ zur eigenen
        aktuellen Position (bridge.squad_lines, C2), der Auslöser mit Alter.
        Ohne frische Datei die gespeicherte Meldung mit Alter und markierter
        Eigenposition ("DU (Birgit)") - Birgit hielt am 24.10. ihre eigene,
        3 min alte Position für den Standort eines Kameraden. Älter als
        SITREP_MAX_AGE_SEC: verwerfen, der nächste kommt ohnehin.
        Meldungen ohne "Squad-Lage"-Kopf (Missionsansage o. ä.) sind kein
        Positions-Schnappschuss: wörtlich mit Alter, nie ersetzt oder verworfen."""
        now = time.time() if now is None else now
        try:
            age = max(0.0, now - float(sit.get("t") or now))
        except (TypeError, ValueError):
            age = 0.0
        text = str(sit.get("text") or "")
        if not text.startswith("Squad-Lage"):
            return f"Lagezentrum (Meldung vor {age:.0f} s): {text}" if text else ""
        if age > SITREP_MAX_AGE_SEC:
            return ""
        trig = _sitrep_trigger(text)
        npc = (self.bridge.read_state() or {}).get("npc") or {}
        now_line = ""
        if npc.get("spawned"):
            try:
                px = float(npc.get("pos_x", 0.0))
                pz = float(npc.get("pos_z", 0.0))
            except (TypeError, ValueError):
                px, pz = self.pos_now
            now_line = _squad_lines(self.bridge.npc_id, px, pz, details=True)
        if now_line:
            out = "Lagezentrum (Stand jetzt): " + now_line
            if trig:
                out += f". Auslöser vor {age:.0f} s: {trig}"
            return out
        return (f"Lagezentrum (Meldung vor {age:.0f} s, Positionen evtl. "
                f"überholt): " + _mark_self(_strip_sitrep_stamp(text), AGENT_NAME))

    def fallen_fresh(self, name: str) -> bool:
        """True = dieser Tod ist neu und darf einen Weckruf auslösen.
        Merkt sich den Namen; die jeweils andere Quelle (Peer-Poll vs.
        Lagezentrum-Funk) ist damit binnen FALLEN_DEDUPE_SEC unterdrückt."""
        now = time.monotonic()
        key = (name or "").strip().lower()
        last = self.fallen_recent.get(key)
        self.fallen_recent[key] = now
        return last is None or (now - last) >= FALLEN_DEDUPE_SEC

    def route_funk(self, funk: dict) -> str:
        """Eine voice_inbox-Zeile einsortieren. Rückgabe: Weckruf-Text
        (löst einen Zug aus) oder "" (in den Info-Digest übernommen).
        Routine-Sitreps des Lagezentrums (prio=False, s. orchestrator.
        inbox_append) fahren nur im Digest mit; Prio-Funk (Tod/kritisch)
        bleibt ein echter Weckruf. 'X ist gefallen' wird dabei gegen den
        Peer-Poll dedupliziert (fallen_fresh): ist jeder genannte Tod schon
        gemeldet und sonst nichts kritisch, fällt der Funk in den Digest."""
        text = (funk.get("text") or "").strip()
        sender = str(funk.get("user") or "?")
        if sender.strip().lower() == "lagezentrum":
            fallen = [n for n in self.roster_names
                      if f"{n} ist gefallen" in text]
            fresh = [n for n in fallen if self.fallen_fresh(n)]
            if (not funk.get("prio")
                    or (fallen and not fresh
                        and "kritisch" not in text.lower())):
                # Erzeugungszeit aus der Inbox-Zeile (orchestrator schreibt
                # "t"); unplausibel/fehlend -> Empfangszeit.
                now = time.time()
                try:
                    t = float(funk.get("t"))
                except (TypeError, ValueError):
                    t = now
                if not (now - 3600.0 < t <= now + 5.0):
                    t = now
                if text.startswith("Squad-Lage"):
                    self.info_sitrep = {"t": t, "text": text}
                else:
                    self.info_notes.append({"t": t, "text": text})
                    if len(self.info_notes) > 3:
                        self.info_notes.pop(0)
                return ""
        return f"FUNK von {sender}: \"{text}\""

    def _native_radio(self) -> dict:
        """ascii->native aus dem gemeinsamen Klartext-Funkkanal (letzte ~200
        Zeilen). Macht latinisierten Peer-Funk für den Empfänger wieder lesbar -
        sonst käme z.B. Pinyin/Buckwalter statt sinnvollem Text an. Leere Map,
        wenn die Datei fehlt (= keine fremdsprachigen Funksprüche, Normalfall)."""
        out: dict = {}
        try:
            with open(RADIO_NATIVE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-200:]
        except OSError:
            return out
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            a = e.get("ascii")
            n = e.get("native")
            if a and n:
                out[a] = n
        return out

    def poll(self) -> tuple[list[str], dict]:
        state = self.bridge.read_state() or {}
        npc = state.get("npc", {})
        events: list[str] = []

        if not npc.get("spawned"):
            return events, state

        alive = bool(npc.get("alive"))
        if self.was_alive and not alive:
            self.was_alive = False
            events.append("DU BIST GESTORBEN.")
            return events, state
        if alive:
            self.was_alive = True

        # Chat (eigene Aussagen des NPCs nicht als Weckruf zurückspielen)
        own_name = npc.get("name", "")
        own_l = AGENT_NAME.lower()
        roster_l = [n.lower() for n in self.roster_names]
        native_map = self._native_radio()   # latinisierten Peer-Funk wieder lesbar machen
        try:
            state_up = float(state.get("uptime") or 0.0)
        except (TypeError, ValueError):
            state_up = 0.0
        try:
            my_x = float(npc.get("pos_x", 0.0))
            my_z = float(npc.get("pos_z", 0.0))
        except (TypeError, ValueError):
            my_x, my_z = self.pos_now
        self.pos_now = (my_x, my_z)
        for m in state.get("chat", []):
            if m.get("id", 0) > self.last_chat_id:
                self.last_chat_id = m.get("id", 0)
                sender = m.get("sender") or ""
                if own_name and sender == own_name:
                    continue
                raw = m.get("text") or ""
                text_l = raw.lower()
                # Latinisierten Funk eines fremdsprachigen NPCs wieder ins Original
                # bringen (Namen/Adressierung bleiben ASCII, also unberührt).
                disp = native_map.get(raw, raw)
                named = [n for n in self.roster_names if n.lower() in text_l]
                addressed_me = own_l in (n.lower() for n in named)
                # Nennt die Nachricht andere Agenten, aber nicht mich, ist sie
                # nicht für mich ("Igor, komm her" weckt Viktor nicht).
                if named and not addressed_me:
                    continue
                # Adressat sichtbar machen (VI-10): beginnt der Satz mit
                # BEKANNTEN Namen (Roster/Spieler-Alias) vor Kommas und ist
                # meiner nicht darunter, gilt er einem anderen. Birgit bezog am
                # 24.10. "Player, ... Ich komme zu dir" auf sich. "Viktor,
                # Birgit, folgt mir" bleibt für Birgit ein direkter Befehl.
                to_other = _chat_addressee(raw, self.known_names_l, own_l)
                is_mate = sender.lower() in roster_l
                # Kameraden-Funk mit Koordinaten: eigene Position daneben, damit
                # eine weitergefunkte ALTE Eigenposition als solche erkennbar ist.
                pos_note = ""
                if is_mate and _has_coords(raw):
                    pos_note = f" [deine eigene Position jetzt: x{my_x:.0f} z{my_z:.0f}]"
                # Geplauder eines anderen KI-Survivors, das mich nicht direkt
                # anspricht: nur als Digest sammeln, keinen Zug auslösen
                # (das war der größte Token-Fresser - 75 % der Züge).
                if is_mate and not addressed_me:
                    who = f"{sender} an {to_other}" if to_other else sender
                    self.smalltalk.append(f"{who}: {disp}{pos_note}")
                    if len(self.smalltalk) > 8:
                        self.smalltalk.pop(0)
                    continue
                age_note = ""
                if state_up > 0:
                    try:
                        c_age = max(0.0, state_up - float(m.get("uptime") or state_up))
                    except (TypeError, ValueError):
                        c_age = 0.0
                    if c_age > CHAT_AGE_NOTE_SEC:
                        age_note = f"(vor {c_age:.0f} s) "
                if to_other:
                    how = "erwähnt dich nur" if addressed_me else "nicht an dich gerichtet"
                    events.append(f"CHAT von {sender} an {to_other} ({how}): "
                                  f"{age_note}\"{disp}\"{pos_note}")
                else:
                    events.append(f"CHAT von {sender}: {age_note}\"{disp}\"{pos_note}")

        # Spieler tauchen auf / verschwinden
        players_now = set()
        infected_min = 9999.0
        predator_min = 9999.0
        predator_what = ""
        hostiles_near = 0   # Infizierte + Raubtiere + fremde Spieler in CLUSTER_RADIUS
        for e in state.get("nearby", []):
            kind = e.get("kind")
            dist = e.get("distance", 9999)
            if kind == "player":
                pname = e.get("name") or e.get("classname", "?")
                players_now.add(pname)
                # Fremde (nicht die eigene Squad) zählen zur Gefahrenzone - ein
                # Cluster Bewaffneter ist ein Camp/eine Patrouille (Viktors Tod).
                if pname not in self.roster_names and dist < CLUSTER_RADIUS:
                    hostiles_near += 1
                continue
            is_pred = (kind == "animal" and _is_predator(e.get("classname", "")))
            if kind == "infected" or is_pred:
                if dist < CLUSTER_RADIUS:
                    hostiles_near += 1
                if kind == "infected" and dist < infected_min:
                    infected_min = dist
                if is_pred and dist < predator_min:
                    predator_min = dist
                    predator_what = e.get("classname") or "Raubtier"

        for name in players_now - self.known_players:
            events.append(f"SPIELER GESICHTET: '{name}' ist in deiner Naehe aufgetaucht.")
        # Runden-Start: Spieler, die beim ersten Watcher schon dastanden,
        # genau EINMAL zum Begrüßen melden (kein "aufgetaucht"-Spam).
        for name in self.greet_pending & players_now:
            events.append(f"SPIELER ANWESEND: '{name}' steht bei dir - "
                          f"begruesse ihn kurz.")
        self.greet_pending -= players_now
        self.known_players = players_now

        # Gefahrenzone voraus: mehrere Gegner dicht beieinander (Horde, Wolfsrudel
        # ODER bewaffnetes Camp). Früh warnen, damit der NPC AUSWEICHT statt blind
        # hindurchzumarschieren - genau Viktors Tod (Beeline durchs Militärcamp).
        # Gelatcht, damit nicht pro Tick ein Zug ausgelöst wird - und mit
        # Hysterese: erst nach CLUSTER_REARM_SEC ohne Cluster wieder scharf.
        now_w = time.monotonic()
        self.infected_min_now = infected_min
        self.predator_min_now = predator_min
        self.hostiles_now = hostiles_near
        # Mitfahrer im Fahrzeug (DR-3): Kampf-/Equip-Tools gehen dort nicht,
        # Birgit wollte am 24.10. vom Sozius aus mit dem Messer kämpfen.
        veh_note = ""
        if npc.get("in_vehicle"):
            veh_note = " DU SITZT ALS MITFAHRER IM FAHRZEUG: nicht kämpfen, sitzen bleiben."
        if hostiles_near >= CLUSTER_COUNT:
            self.cluster_clear_since = 0.0
            if not self.cluster_warned:
                self.cluster_warned = True
                events.append(f"GEFAHR: {hostiles_near} Gegner dicht beieinander voraus "
                              f"(Horde/Camp möglich). Prüfe mit observe und WEICHE AUS "
                              f"oder kämpfe gezielt - lauf nicht blind mitten hindurch."
                              + veh_note)
        else:
            if not self.cluster_clear_since:
                self.cluster_clear_since = now_w
            if (self.cluster_warned
                    and now_w - self.cluster_clear_since >= CLUSTER_REARM_SEC):
                self.cluster_warned = False

        # Raubtier (Wolf/Bär) nah - schnell und tödlich, früher warnen als Infizierte
        if predator_min < PREDATOR_NEAR and not self.predator_warned:
            self.predator_warned = True
            events.append(f"GEFAHR: Raubtier ({predator_what}) nur noch "
                          f"{predator_min:.0f} m! engage (Waffe ziehen) oder flee."
                          + veh_note)
        if predator_min > PREDATOR_NEAR + 15:
            self.predator_warned = False

        # Schüsse dieses Körpers (C4): Zuwachs sammeln, Rücksprung (neuer
        # Körper nach Respawn startet bei 0) still neu ankern.
        if "shots" in npc:
            try:
                shots = int(npc.get("shots") or 0)
            except (TypeError, ValueError):
                shots = self.shots
            if shots < self.shots:
                self.shots = shots
            elif shots > self.shots:
                self.shots_unreported += shots - self.shots
                self.shots = shots
                weapon = str(npc.get("last_shot_weapon") or "").strip()
                if weapon and weapon not in self.shot_weapons:
                    self.shot_weapons.append(weapon)

        # Blutung (gelatcht): unbehandelt tödlich, darum kritischer Weckruf.
        # Das bleeding-Feld liefert die Mod seit 23.08. (IsBleeding im State).
        # Seit 08.09. verbindet die Mod sich per Reflex SELBST (1 Hz, ab dem
        # zweiten Blutungs-Tick, wenn Verbandsmaterial da ist) - der Weckruf
        # lässt dem Reflex BLEED_REFLEX_GRACE_SEC Vorsprung und feuert nur,
        # wenn die Blutung danach noch steht oder kein Verband da ist. Der
        # Text nennt den Vorrat, damit das Gehirn nicht ins Leere bandagiert.
        bleeding = bool(npc.get("bleeding"))
        now_b = time.monotonic()
        auto_b = int(npc.get("auto_bandages") or 0)
        if auto_b > self.auto_bandages:
            used = auto_b - self.auto_bandages
            self.auto_bandages = auto_b
            self.info_lines.append(f"REFLEX: Blutung selbst verbunden ({used}x "
                                   f"Verbandsmaterial verbraucht) - Vorrat prüfen.")
            if len(self.info_lines) > 3:
                self.info_lines.pop(0)
        if not bleeding:
            self.bleed_warned = False
            self.bleed_since = 0.0
        else:
            if not self.bleed_since:
                self.bleed_since = now_b
            if not self.bleed_warned:
                dressings = sum(1 for i in state.get("inventory", [])
                                if i.get("classname") in ("BandageDressing", "Rag")
                                and float(i.get("health", 100) or 0) > 5)
                reflex = "auto_bandages" in npc
                if dressings and reflex and (now_b - self.bleed_since) < BLEED_REFLEX_GRACE_SEC:
                    pass   # Mod-Reflex verbindet selbst; nur weiter beobachten
                elif dressings:
                    self.bleed_warned = True
                    events.append(f"DU BLUTEST! Verbinde dich SOFORT mit bandage() "
                                  f"({dressings}x Verbandsmaterial im Inventar) - "
                                  f"unbehandelt verblutest du.")
                else:
                    self.bleed_warned = True
                    events.append("DU BLUTEST und hast KEIN Verbandsmaterial! Sofort "
                                  "BandageDressing/Rag beschaffen: loot_area oder "
                                  "pickup in der Nähe, sonst Kameraden per Funk um "
                                  "give_to bitten - unbehandelt verblutest du.")

        # Einzelner Infizierter nah (gelatcht)
        if infected_min < INFECTED_NEAR and not self.infected_warned:
            self.infected_warned = True
            events.append(f"GEFAHR: Infizierter nur noch {infected_min:.0f} m entfernt!"
                          + veh_note)
        if infected_min > 55:
            self.infected_warned = False

        # Bewusstlosigkeit (gelatcht)
        uncon = bool(npc.get("unconscious"))
        self.uncon_now = uncon
        if uncon and not self.uncon_warned:
            self.uncon_warned = True
            events.append("DU BIST UMGEKIPPT (bewusstlos). Warte ab; sobald du "
                          "wieder bei dir bist, nutze unstick zum Aufstehen.")
        if not uncon and self.uncon_warned:
            self.uncon_warned = False
            events.append("DU BIST WIEDER BEI BEWUSSTSEIN. Mit unstick aufstehen "
                          "und Lage pruefen.")

        # Fahrzeug rein/raus (gelatcht)
        in_veh = bool(npc.get("in_vehicle"))
        if in_veh and not self.in_vehicle:
            events.append("DU SITZT JETZT IN EINEM FAHRZEUG (Mitfahrer). "
                          "Bleib sitzen, bis der Fahrer haelt oder dich bittet "
                          "auszusteigen.")
        if not in_veh and self.in_vehicle:
            events.append("DU BIST AUS DEM FAHRZEUG AUSGESTIEGEN.")
        self.in_vehicle = in_veh

        # Schaden
        health = npc.get("health", 100.0)
        blood = npc.get("blood", 5000.0)
        if self.last_health - health > HEALTH_DROP or self.last_blood - blood > BLOOD_DROP:
            events.append(f"DU NIMMST SCHADEN: HP {health:.0f}, Blut {blood:.0f}.")
        self.last_health = health
        self.last_blood = blood

        # Vitalwerte (gelatcht bis wieder ok)
        water = npc.get("water", 5000.0)
        energy = npc.get("energy", 5000.0)
        if water < WATER_LOW and not self.water_warned:
            self.water_warned = True
            events.append(f"DURST: Wasser nur noch {water:.0f}.")
        if water > WATER_LOW + 500:
            self.water_warned = False
        if energy < ENERGY_LOW and not self.energy_warned:
            self.energy_warned = True
            events.append(f"HUNGER: Energie nur noch {energy:.0f}.")
        if energy > ENERGY_LOW + 500:
            self.energy_warned = False

        # Kälte (gelatcht): unter -0.5 friert er ernsthaft
        heat = npc.get("heat_comfort", 0.0)
        if heat < -0.5 and not self.cold_warned:
            self.cold_warned = True
            events.append(f"DIR IST KALT (Waerme {heat:.2f}): Zieh mehr "
                          f"Kleidung an (wear), loote welche oder mach ein "
                          f"Feuer.")
        if heat > -0.2:
            self.cold_warned = False

        # Neue gelatchte Ereignis-Quellen. Alle tolerant gegen fehlende Felder
        # (die Mod-Seite liefert world/wet/disease erst nach ihrem Repack) und
        # alle nicht-kritisch -> laufen über die normale 15-s-Bündelung.
        events += self._poll_travel()
        events += self._poll_weather(state, npc)
        events += self._poll_disease(npc)
        events += self._poll_peers()
        events += self._poll_campfire(state, own_name)
        events += self._poll_stillstand(state, npc)

        # Info-only-Ereignisse (Wetter-Übergänge, Fahrzeug rein/raus) in den
        # Digest abzweigen: Latch-Logik oben unverändert, nur die Zustellung
        # wechselt - kein eigener Zug mehr für reine Kenntnisnahmen.
        kept = []
        for ev in events:
            if ev.startswith(_INFO_PREFIXES):
                self.info_lines.append(ev)
                if len(self.info_lines) > 3:
                    self.info_lines.pop(0)
            else:
                kept.append(ev)

        return kept, state

    def stationary_secs(self) -> float:
        """Sekunden, die der Körper praktisch unbewegt und unbeschäftigt
        dasteht (klein/0 = in Bewegung oder beschäftigt - der Anker wird
        dann laufend zurückgesetzt). Für den Idle-Backoff-Stopp."""
        if self.still_anchor is None:
            return 0.0
        return max(0.0, time.monotonic() - self.still_since)

    def _poll_stillstand(self, state: dict, npc: dict) -> list[str]:
        """STILLSTAND: der NPC steht länger unbewegt herum, ohne dass ein
        Kommando läuft, er kämpft, folgt oder fährt. Weckt das Gehirn mit
        einem konkreten Bewegungsauftrag statt zu warten, bis der (per
        Backoff gestreckte) Routine-Tick irgendwann kommt."""
        now = time.monotonic()
        try:
            px = float(npc.get("pos_x", 0.0))
            pz = float(npc.get("pos_z", 0.0))
        except (TypeError, ValueError):
            return []
        cmd = state.get("command") or {}
        busy = (str(cmd.get("status") or "") == "running"
                or bool(npc.get("fighting"))
                or bool(npc.get("following"))
                or bool(npc.get("in_vehicle"))
                or bool(npc.get("unconscious")))
        moved = True
        if self.still_anchor is not None:
            dx = px - self.still_anchor[0]
            dz = pz - self.still_anchor[1]
            moved = (dx * dx + dz * dz) > (STILL_MOVE_M * STILL_MOVE_M)
        if busy or moved or self.still_anchor is None:
            self.still_anchor = (px, pz)
            self.still_since = now
            return []
        # Mensch in Sichtweite: Stehen ist Interaktion oder Wache beim
        # Spieler - keine Mahnung (der Zähler läuft weiter; geht der
        # Spieler, darf die Mahnung sofort kommen).
        if any(e.get("kind") == "player" for e in state.get("nearby", [])):
            return []
        standing = now - self.still_since
        if standing < STILL_WARN_SEC:
            return []
        if (now - self.still_last_warn) < STILL_REPEAT_SEC:
            return []
        self.still_last_warn = now
        # Ist der nächste Kamerad weit weg, zuerst zu ihm (RESP-5): der alte
        # Text nannte explore_step als erste Option - Birgit pendelte am
        # 24.10. 20 min lang Erkundungsbögen um ihren Treffpunkt, 190 m vor
        # Igor und Konrad. "seit N Sekunden" aktualisiert die Zustellung.
        mate = _nearest_mate(_squad_lines(self.bridge.npc_id, px, pz))
        if mate and mate["dist"] > STILL_FAR_MATE_M and not AGENT_BR:
            todo = (f"ZUERST travel_to zum nächsten Kameraden ({mate['name']}, "
                    f"{mate['dist']:.0f} m {mate['dir']}, x={mate['x']:.0f} "
                    f"z={mate['z']:.0f}) - läuft im Hintergrund. Sonst: "
                    f"loot_area oder dress_best/kochen am Lager.")
        else:
            todo = ("loot_area, explore_step, travel_to zu einem Ziel, "
                    "dress_best/kochen am Lager oder follow zum Squad.")
        return [(f"STILLSTAND: Du stehst seit {standing:.0f} Sekunden "
                 f"unbewegt bei x={px:.0f} z={pz:.0f} und nichts läuft. "
                 f"Rumstehen ist gefährlich (Infizierte, Auskühlung) und "
                 f"tötet den Spielfluss. Tu JETZT etwas Sichtbares: {todo} "
                 f"Beende den Zug erst, wenn etwas läuft.")]

    def _poll_travel(self) -> list[str]:
        """REISE: travel_event_<id>.json konsumieren. Der travel_to-Thread in
        dayz_mcp schreibt sie bei Ankunft/Abbruch:
        {"event":"arrived"|"stuck"|"aborted","x":<f>,"z":<f>,"detail":"<t>"}.
        Die Datei wird IMMER gelöscht - auch wenn sie kaputt ist, sonst
        würde sie jeden Tick erneut gelesen (Weckruf-Spam)."""
        path = self.travel_file
        if not os.path.exists(path):
            return []
        data = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        try:
            os.remove(path)
        except OSError:
            pass
        if not isinstance(data, dict):
            return []
        ev = str(data.get("event") or "")
        try:
            x = float(data.get("x") or 0.0)
            z = float(data.get("z") or 0.0)
        except (TypeError, ValueError):
            x = z = 0.0
        detail = str(data.get("detail") or "").strip()
        suffix = f" ({detail})" if detail else ""
        if ev == "arrived":
            return [f"REISE: angekommen bei x={x:.0f} z={z:.0f}{suffix}. "
                    f"Verschaffe dir einen Ueberblick und mach SOFORT mit "
                    f"deinem Plan weiter - starte die naechste Aktion "
                    f"(loot_area, travel_to zum naechsten Ziel, Auftrag "
                    f"erledigen), bleib nicht stehen."]
        if ev in ("stuck", "aborted"):
            was = "stecken geblieben" if ev == "stuck" else "abgebrochen"
            return [f"REISE: {was} bei x={x:.0f} z={z:.0f}{suffix} - "
                    f"entscheide neu (anderer Weg, anderes Ziel oder erst "
                    f"die Lage klaeren)."]
        return []

    def _poll_weather(self, state: dict, npc: dict) -> list[str]:
        """WETTER: Übergänge aus state.world plus npc.wet. Jeder Übergang
        ist gelatcht bis zum Gegenzustand (Sonnenstand: bis zum nächsten
        Wechsel; Regen/Nässe: mit Hysterese), damit kein Tick-Spam entsteht."""
        events: list[str] = []
        world = state.get("world")
        if isinstance(world, dict):
            sun = str(world.get("sun") or "")
            if sun and sun != self.last_sun:
                # Erst ab dem ZWEITEN Zustand melden - der erste Snapshot
                # setzt nur die Basis (kein Weckruf beim Kaltstart).
                if self.last_sun:
                    if sun == "dusk":
                        events.append("ES DAEMMERT. Bald wird es dunkel - denk "
                                      "an Licht, Wärme und einen sicheren "
                                      "Platz für die Nacht.")
                    elif sun == "night":
                        events.append("DIE NACHT BRICHT HEREIN. Schlechte "
                                      "Sicht - sei vorsichtig, bleib eher am "
                                      "Lager/Feuer oder nutze eine Lichtquelle.")
                    elif sun == "dawn":
                        events.append("DER MORGEN GRAUT. Die Sicht wird "
                                      "besser - gut für Aufbruch und Pläne.")
                self.last_sun = sun
            try:
                rain = float(world.get("rain") or 0.0)
            except (TypeError, ValueError):
                rain = 0.0
            if rain > RAIN_START and not self.rain_warned:
                self.rain_warned = True
                events.append("REGEN SETZT EIN. Du wirst nass und kuehlst aus "
                              "- such einen Unterstand oder zieh wetterfeste "
                              "Kleidung an.")
            if rain < RAIN_STOP:
                self.rain_warned = False
        try:
            wet = float(npc.get("wet") or 0.0)
        except (TypeError, ValueError):
            wet = 0.0
        if wet > WET_SOAKED and not self.wet_warned:
            self.wet_warned = True
            events.append("DU BIST DURCHNAESST. Nasse Kleidung kuehlt dich "
                          "gefaehrlich aus - trockne dich am Feuer, wechsle "
                          "Kleidung oder stell dich unter.")
        if wet < WET_DRY:
            self.wet_warned = False
        return events

    def _poll_disease(self, npc: dict) -> list[str]:
        """KRANKHEIT: state.npc.disease (tolerant). Die Mod trägt nur
        Erreger ÜBER der Schwelle ein - pro Erreger gelatcht, bis er wieder
        aus dem State verschwindet (= unter die Schwelle gefallen ist)."""
        events: list[str] = []
        disease = npc.get("disease")
        agents = disease.get("agents") if isinstance(disease, dict) else None
        if not isinstance(agents, dict):
            agents = {}
        active: set[str] = set()
        for key, val in agents.items():
            try:
                if int(val) > 0:
                    active.add(str(key))
            except (TypeError, ValueError):
                active.add(str(key))  # eingetragen = über der Schwelle
        for key in sorted(active - self.disease_warned):
            name = _DISEASE_NAMES.get(key, key)
            events.append(f"DU BIST KRANK: {name} - behandle dich "
                          f"(treat_illness) oder such passende Medizin.")
        self.disease_warned = active
        return events

    def _poll_peers(self) -> list[str]:
        """KAMERAD GEFALLEN: state_<peer>.json der anderen Roster-Agenten auf
        den Übergang lebendig -> tot prüfen. Gelatcht, bis der Peer wieder
        lebt (Respawn). Im BR-Modus KEIN Gedenken (Funkstille-Regel) - der
        Latch wird trotzdem gepflegt."""
        events: list[str] = []
        for agent in self.roster_agents:
            pid = agent.get("id") or ""
            if not pid or pid == self.bridge.npc_id:
                continue
            path = os.path.join(self.bridge.dir, f"state_{pid}.json")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    pstate = json.load(f)
            except (OSError, ValueError):
                continue
            pnpc = (pstate or {}).get("npc") if isinstance(pstate, dict) else None
            if not isinstance(pnpc, dict) or not pnpc.get("spawned"):
                continue  # nie gespawnt/despawnt: kein Urteil über Leben/Tod
            try:
                hp = float(pnpc.get("health", 100.0) or 0.0)
            except (TypeError, ValueError):
                hp = 100.0
            alive = bool(pnpc.get("alive")) and hp > 0.0
            was = self.peer_alive.get(pid)
            self.peer_alive[pid] = alive
            if was is True and not alive and not AGENT_BR:
                name = agent.get("name") or pid
                if not self.fallen_fresh(name):
                    continue  # Lagezentrum-Funk hat den Tod binnen 90 s schon gemeldet
                events.append(
                    f"KAMERAD GEFALLEN: {name} ist tot. "
                    f"Sag GENAU EINEN kurzen Abschiedssatz per say_voice, lege "
                    f"optional EINE Grabbeigabe ab (drop) - und mach danach "
                    f"nach deinen Prioritaeten weiter.")
        return events

    def _poll_campfire(self, state: dict, own_name: str) -> list[str]:
        """LAGERFEUER-RUNDE: eigener NPC + mindestens ein Roster-Kamerad
        binnen CAMPFIRE_MATE_DIST an einer Feuerstelle, KEIN Gefahr-Latch
        aktiv, Cooldown abgelaufen -> EIN Weckruf zu einer Anekdote, Frage
        oder Erinnerung. Danach 30 min Ruhe (Token-Disziplin)."""
        if AGENT_BR:
            return []  # Funkstille: im Battle-Royale wird nicht geplaudert
        if time.monotonic() < self.campfire_next:
            return []
        if self.infected_warned or self.predator_warned or self.cluster_warned:
            return []  # Gefahr-Latch aktiv - kein Moment für Anekdoten
        roster_l = [n.lower() for n in self.roster_names]
        mates: list[str] = []
        fire_near = False
        for e in state.get("nearby", []):
            dist = e.get("distance", 9999)
            cls = (e.get("classname") or "").lower()
            if "fireplace" in cls and dist <= CAMPFIRE_FIRE_DIST:
                fire_near = True
                continue
            # Auch kind=="ai": die Mod klassifiziert KI-Kameraden als "ai",
            # nicht "player" - mit dem alten Filter war mates IMMER leer und
            # die Lagerfeuer-Runde konnte nie auslösen.
            if e.get("kind") not in ("player", "ai") or dist > CAMPFIRE_MATE_DIST:
                continue
            name = e.get("name") or ""
            if name and name != own_name and name.lower() in roster_l:
                mates.append(name)
        if not (fire_near and mates):
            return []
        self.campfire_next = time.monotonic() + CAMPFIRE_COOLDOWN_SEC
        who = ", ".join(mates[:3])
        return [f"LAGERFEUER-RUNDE: Du stehst mit {who} am Feuer und es ist "
                f"gerade ruhig. Nutze den Moment für EINE kurze Anekdote, "
                f"EINE Frage an einen Kameraden oder EINE Erinnerung aus "
                f"deinem memory/-Ordner (say_voice). EIN Zug - danach ganz "
                f"normal weiter."]


def save_inventory_snapshot(state: dict) -> None:
    npc = state.get("npc", {})
    if not npc.get("alive"):
        return
    # worn/slot mitsichern (RESTORE-2): ohne sie zog der Restore mitgeführte
    # Ersatzkleidung an und tauschte die getragenen Stücke aus.
    items = [{"classname": i.get("classname", ""), "kind": i.get("kind", "other"),
              "worn": bool(i.get("worn")), "slot": i.get("slot", "") or ""}
             for i in state.get("inventory", []) if i.get("classname")]
    if not items:
        return
    snapshot = {
        "items": items,
        "hands": npc.get("in_hands", ""),
        "saved": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = SNAPSHOT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def load_inventory_snapshot() -> dict | None:
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def restore_inventory(bridge: Bridge, journal: Journal, snapshot: dict) -> None:
    global LAST_RESTORE_LOST, LAST_RESTORE_GROUND
    raw = snapshot.get("items", [])[:80]
    # Altes Format (reine Classnames) tolerieren
    items = [i if isinstance(i, dict) else {"classname": i, "kind": "other"}
             for i in raw]

    # Reihenfolge entscheidet über das ÜBERLEBEN.
    # Seit 08.09.2026: KLEIDUNG ZUERST, und zwar per give_wear (Mod erzeugt das
    # Stück vor den Füßen und zieht es sofort an, Slot-Tausch samt
    # Inhaltssicherung). Vorher lief Kleidung über give_item/CreateInInventory
    # ins CARGO des Spawn-Outfits und fraß den Platz, den sie schaffen sollte:
    # Viktor verlor 16 von 40 Items ("kein Platz"), darunter Bandagen und
    # Tetracycline, obwohl Medizin auf Rang 0 stand. Angezogen belegt Kleidung
    # keinen Cargo-Platz, sondern liefert welchen (Rucksack, Weste, Hose).
    # Danach Medizin (winzig, lebenswichtig), Waffen (Feuer- UND Nahkampf),
    # Munition, Rest. Innerhalb der Kleidung Behälter zuerst (Rucksack/Weste).
    # Seit 24.10.: Nahkampfwaffen auf Rang 3 (vorher "other" = Rang 5 mit den
    # Dosen - Birgit verlor Crowbar und SteakKnife und war danach waffenlos),
    # und nur GETRAGENE Kleidung wird angezogen (worn aus dem Snapshot);
    # mitgeführte Ersatzkleidung kommt als normales Item ganz hinten.
    _MEDICAL = ("bandage", "rag", "bloodbag", "saline", "morphine",
                "epinephrine", "tetracycline", "charcoal", "vitamin",
                "disinfectant", "alcoholtincture", "splint", "sewingkit",
                "defibrillator", "painkiller", "iodine")
    _CONTAINER_CLOTHING = ("bag", "backpack", "vest", "pack", "pouch", "rig",
                           "pants", "jeans", "jacket", "coat", "hoodie", "shirt",
                           "sweater")

    def _is_worn(i) -> bool:
        # Alt-Snapshots ohne worn-Feld: wie bisher als getragen behandeln
        return i.get("worn") is not False

    def _restore_rank(i):
        cn = i.get("classname", "").lower()
        if i.get("kind") == "clothing":
            if not _is_worn(i):
                return 5      # Ersatzkleidung: Ballast, zuletzt
            if any(c in cn for c in _CONTAINER_CLOTHING):
                return 0      # Stauraum zuerst: Rucksack, Weste, Hose, Jacke
            return 1          # Mützen, Schuhe, Handschuhe, Masken
        if any(m in cn for m in _MEDICAL):
            return 2          # lebenswichtig, winzig, passt jetzt
        if i.get("kind") == "firearm":
            return 3          # Waffe belegt dedizierte Waffenslots
        cname = i.get("classname", "")
        if tactics.classify_melee(cname) and not tactics.is_tool(cname):
            return 3          # Nahkampfwaffe = Waffe, nicht Krempel
        return {"ammo": 4}.get(i.get("kind", "other"), 5)

    items.sort(key=_restore_rank)

    def _reason(result: dict) -> str:
        status = result.get("status") or "timeout"
        detail = " ".join(str(result.get("detail") or "").split())[:40]
        return f"{status}: {detail}" if detail else status

    def _on_ground(result: dict) -> bool:
        # C7: CmdGiveItem legt das Item neben den NPC, wenn kein Platz ist
        return (result.get("status") == "done"
                and str(result.get("detail") or "").startswith("am Boden"))

    journal.log(f"Stelle Inventar wieder her ({len(items)} Items)...")
    ok = 0
    worn = 0
    failed: list[tuple[str, str]] = []
    ground: list[str] = []
    for item in items:
        cn = item["classname"]
        if item.get("kind") == "clothing" and _is_worn(item):
            # Tragen statt stauen. Zieht die Mod das Stück an keinen Slot
            # (zweite Mütze, dritte Hose), legt sie es selbst ins Cargo.
            result = bridge.run("give_wear", text=cn, timeout=12)
            if result.get("status") == "done":
                ok += 1
                if "angezogen" in (result.get("detail") or ""):
                    worn += 1
                continue
            # Ältere Mod ohne give_wear oder echter Fehlschlag: alter Weg
            result = bridge.run("give_item", text=cn, timeout=10)
        else:
            result = bridge.run("give_item", text=cn, timeout=10)
        if _on_ground(result):
            ground.append(cn)
        elif result.get("status") == "done":
            ok += 1
        else:
            failed.append((cn, _reason(result)))

    hands = snapshot.get("hands", "")
    if hands:
        bridge.run("equip", text=hands, timeout=6)

    # Hand-Equip macht ggf. einen Schulterplatz frei: ein Retry-Durchgang
    still_failed: list[tuple[str, str]] = []
    for classname, _first in failed:
        result = bridge.run("give_item", text=classname, timeout=10)
        if _on_ground(result):
            ground.append(classname)
        elif result.get("status") == "done":
            ok += 1
        else:
            still_failed.append((classname, _reason(result)))

    # Leere Hände nach dem Restore (Snapshot ohne Hand oder Equip gescheitert):
    # beste Waffe ziehen - Birgit stand am 24.10. mit einer Getränkedose in
    # der Hand da, statt mit einer Waffe.
    st = (bridge.read_state() or {}).get("npc", {})
    if not st.get("in_hands"):
        r = bridge.run("equip_best", timeout=30)
        journal.log(f"Restore: Waffe in die Hand -> {r.get('status')} "
                    f"{r.get('detail') or ''}".rstrip())

    lost = [cn for cn, _r in still_failed]
    LAST_RESTORE_LOST = list(lost)
    LAST_RESTORE_GROUND = list(ground)
    msg = f"Inventar wiederhergestellt: {ok} ok ({worn} Kleidungsstuecke angezogen)"
    if ground:
        msg += f", am Boden: {', '.join(ground)}"
    if still_failed:
        msg += ", verloren: " + ", ".join(f"{cn} ({r})" for cn, r in still_failed)
        lost_w = [cn for cn in lost
                  if tactics.classify_weapon(cn)
                  or (tactics.classify_melee(cn) and not tactics.is_tool(cn))]
        if lost_w:
            msg += " | WAFFEN VERLOREN: " + ", ".join(lost_w)
    journal.log(msg)


def _restore_loadout(bridge: Bridge) -> str:
    """Spawn-Loadout für einen Körper, dessen Inventar gleich aus dem
    Snapshot zurückkommt (RESTORE-1): das leere IsuRestoreLoadout, wenn es im
    Server-Profil liegt (ExpansionMod/Loadouts; die Mod lädt es über
    ExpansionHumanLoadout.Apply -> ExpansionPrefab.Load). FreshSpawnLoadout zog
    vorher ein Starter-Outfit samt Chemlights/Obst an; give_wear tauschte es,
    die abgeworfenen Stücke wurden zum Loot-Köder und die Beigaben fraßen
    Cargo-Platz. Fehlt die Datei (öffentliche Kopie), bleibt alles beim Alten."""
    path = os.path.join(os.path.dirname(bridge.dir), "ExpansionMod",
                        "Loadouts", "IsuRestoreLoadout.json")
    return "IsuRestoreLoadout" if os.path.isfile(path) else "FreshSpawnLoadout"


def ensure_body(bridge: Bridge, journal: Journal, restore: bool = True,
                loadout_default: str = "", fresh: bool = False,
                spawn_pos: tuple[float, float] | None = None) -> bool:
    """fresh=True (Menü-Loadout gewählt): der Körper wird IMMER frisch mit
    loadout_default gespawnt - ein noch lebender/übernommener Alt-Körper
    würde sonst mit seinem alten Inventar weiterlaufen und die Menü-Wahl
    stillschweigend überschreiben.
    spawn_pos: abweichender Spawnpunkt (Koop-Respawn neben dem Spieler);
    None = Lager (SPAWN_POS). Die adopt-Prüfung misst gegen denselben Punkt."""
    pos = spawn_pos or SPAWN_POS
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    if npc.get("spawned") and npc.get("alive"):
        if fresh and loadout_default:
            journal.log("Menü-Loadout gewählt - alter Körper wird für das "
                        "frische Equip despawnt.")
            bridge.run("despawn", timeout=10)
        else:
            journal.log(f"Koerper vorhanden: {npc.get('classname')}")
            return True

    journal.log("Kein lebender Koerper - versuche adopt_nearest...")
    result = bridge.run("adopt_nearest", timeout=15)
    if result.get("status") == "done":
        if fresh and loadout_default:
            # Erst übernehmen (sonst bliebe der Alt-Körper als Doppelgänger
            # in der Welt stehen), dann verwerfen und frisch spawnen.
            journal.log("Menue-Loadout gewaehlt - uebernommenen Alt-Koerper "
                        "despawnt, spawne frisch mit Loadout.")
            bridge.run("despawn", timeout=10)
        else:
            # adopt_nearest nimmt den ersten herrenlosen eAI ohne Rücksicht auf die
            # Distanz. Prüfen, ob der Körper nah genug am Spawnpunkt liegt - sonst
            # ist es ein zufälliger Expansion-eAI irgendwo auf der Karte: verwerfen
            # und frisch spawnen, damit die Gruppe wirklich zusammensteht.
            snap = bridge.read_state() or {}
            body = snap.get("npc", {})
            ddx = body.get("pos_x", 0.0) - pos[0]
            ddz = body.get("pos_z", 0.0) - pos[1]
            adopt_dist = (ddx * ddx + ddz * ddz) ** 0.5
            if adopt_dist <= ADOPT_MAX_DIST:
                journal.log(f"Verwaiste eAI übernommen: {result.get('detail') or ''} "
                            f"({adopt_dist:.0f} m vom Spawnpunkt, ok)")
                return True
            journal.log(f"adopt_nearest lieferte Körper {adopt_dist:.0f} m vom "
                        f"Spawnpunkt (> {ADOPT_MAX_DIST:.0f} m) - verwerfe und "
                        f"spawne neu.")
            bridge.run("despawn", timeout=10)
            # fällt durch zum kontrollierten Spawn unten

    # Erster Spawn ohne Snapshot: Berufs-Loadout des Agenten
    snapshot = None
    loadout = loadout_default
    if restore:
        snapshot = load_inventory_snapshot()
        if snapshot:
            # nackter Start - das Inventar kommt gleich aus dem Snapshot zurück
            loadout = _restore_loadout(bridge)

    journal.log(f"Spawne neuen Körper '{AGENT_NAME}' ({AGENT_FACTION}) bei "
                f"({pos[0]:.1f}, {pos[1]:.1f})...")
    result = bridge.run("spawn", x=pos[0], z=pos[1],
                        loadout=loadout, text=AGENT_NAME,
                        faction=AGENT_FACTION, br=AGENT_BR, timeout=30)
    if result.get("status") != "done":
        journal.log("Spawn fehlgeschlagen: " + (result.get("detail") or ""))
        return False

    # Auf echtes Lebendsein warten: spawn meldet "done", sobald der eAI erzeugt
    # ist, aber der Körper braucht ein paar Server-Ticks, bis er im State als
    # spawned+alive auftaucht. Ohne dieses Warten laufen restore_inventory und
    # der erste Weckruf gegen einen halbgeladenen Körper ("npc ist tot"-Schleife).
    for _ in range(16):  # bis ~8 s
        chk = (bridge.read_state() or {}).get("npc", {})
        if chk.get("spawned") and chk.get("alive"):
            break
        time.sleep(0.5)

    if snapshot:
        restore_inventory(bridge, journal, snapshot)
    return True


def world_generation(profile: str) -> str:
    """Kennung der aktuellen Welt-Generation: Name + Anlegezeit des neuesten
    Server-RPT im Profilordner. Jeder Server-Start schreibt ein neues RPT,
    und script-gespawnte Bestände (Depots, abgelegte Items, Leichen)
    überleben den Neustart nicht - eine neue Kennung seit dem letzten Lauf
    bedeutet also Neustart/Wipe aus Agentensicht."""
    try:
        rpts = glob.glob(os.path.join(profile, "*.RPT"))
        if not rpts:
            return ""
        newest = max(rpts, key=os.path.getctime)
        return f"{os.path.basename(newest)}|{int(os.path.getctime(newest))}"
    except OSError:
        return ""


def _log_startup_validation_error(model: str, npc_id: str, home: str,
                                  error: Exception) -> None:
    """Keep pre-journal validation failures visible after a console closes.

    Only validation messages are recorded, never CLI stderr or environment
    values. JSON string quoting keeps each failure on one UTF-8 log line.
    """
    try:
        effective_home = os.path.abspath(home) if home else agent_home_dir(npc_id)
        journal_dir = os.path.join(effective_home, "journal")
        os.makedirs(journal_dir, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        entry = (f"{stamp} model={json.dumps(model, ensure_ascii=False)} "
                 f"error={json.dumps(str(error), ensure_ascii=False)}\n")
        with open(os.path.join(journal_dir, "startup_errors.log"), "a",
                  encoding="utf-8", errors="replace") as log_file:
            log_file.write(entry)
    except (OSError, ValueError):
        # An unwritable/invalid home must not replace the original CLI error.
        pass


def main() -> int:
    global AGENT_HOME, SNAPSHOT_FILE, VOICE_INBOX, STOP_FLAG, SPAWN_POS, AGENT_NAME, AGENT_FACTION, AGENT_BR, AGENT_FREE, AGENT_LANG, INTENT_FILE, AGENT_THINKING, CAMP_POS

    # Konsole gegen Encoding-Abstürze härten (Viktor schreibt deutsch)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    # CTRL_BREAK (vom Arena-Supervisor) wie Strg+C behandeln - sonst stirbt
    # der Prozess hart und der finally-Block (Inventar sichern, despawnen)
    # läuft nie
    def _sigbreak(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGBREAK, _sigbreak)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--idle", type=int, default=180,
                        help="Routine-Tick-Intervall in Sekunden (Default 180)")
    parser.add_argument("--no-autonomy", action="store_true",
                        help="Lokale Survival-Schritte zwischen Modell-Zuegen abschalten")
    parser.add_argument("--survival-interval", type=float, default=5.0,
                        help="Abstand lokaler Survival-Schritte ohne LLM (mindestens 2s)")
    parser.add_argument("--max-turns", type=int, default=0,
                        help="Nach N Zuegen beenden, 0 = unbegrenzt")
    parser.add_argument("--turn-limit", type=int, default=10,
                        help="Max. Werkzeugrunden PRO Zug (Claude --max-turns), "
                             "0 = unbegrenzt. Haelt den Agenten ansprechbar: "
                             "ohne Limit blockiert ein Dauer-Erkundungszug "
                             "Chat und Funk minutenlang (Default 10)")
    parser.add_argument("--once", default="",
                        help="Eine einzelne Mission ausfuehren und beenden")
    parser.add_argument("--mission", default="",
                        help="Text des ersten Weckrufs ueberschreiben")
    parser.add_argument("--no-tp", action="store_true",
                        help="Spieler beim Start NICHT zum NPC teleportieren")
    parser.add_argument("--fresh-loadout", action="store_true",
                        help="Menue-Loadout erzwingen: Alt-Koerper despawnen und "
                             "frisch mit --loadout spawnen (kein Restore/Adopt)")
    parser.add_argument("--no-restore", action="store_true",
                        help="Inventar nach Respawn NICHT wiederherstellen")
    parser.add_argument("--no-mic", action="store_true",
                        help="Lokales Mikrofon-Hören NICHT starten")
    parser.add_argument("--npc-id", default="viktor",
                        help="Agenten-Slot (Arena), Default viktor")
    parser.add_argument("--name", default="",
                        help="Anzeigename (Default: Npc-Id kapitalisiert)")
    parser.add_argument("--home", default="",
                        help="Arbeitsverzeichnis des Agenten (CLAUDE.md, Journale)")
    parser.add_argument("--spawn-x", type=float, default=SPAWN_POS[0])
    parser.add_argument("--spawn-z", type=float, default=SPAWN_POS[1])
    parser.add_argument("--rally-x", type=float, default=None,
                        help="Treffpunkt-X für getrennten Spawn: der Agent "
                             "marschiert nach dem Spawn allein dorthin und "
                             "vereinigt sich dort mit der Gruppe (ohne Spieler). "
                             "Nicht gesetzt = normaler Gruppen-Spawn am Lager.")
    parser.add_argument("--rally-z", type=float, default=None,
                        help="Treffpunkt-Z (siehe --rally-x)")
    parser.add_argument("--faction", default="civilian",
                        choices=["civilian", "west", "east", "mercenaries", "raiders"])
    parser.add_argument("--character", default="",
                        help="Charakterblock-Datei, wird an die Persona angehaengt")
    parser.add_argument("--voice", default="",
                        help="ElevenLabs-Stimme dieses Agenten (Discord-TTS)")
    parser.add_argument("--language", default="de",
                        help="Ausgabe-Sprache der NPC (Funk/Logbuch), z.B. de, "
                             "en, fr ... (Codes siehe LANG_NAMES). Default de.")
    parser.add_argument("--thinking", type=int, default=None,
                        help="Sichtbares Modell-Denken: >0 = an (Anthropic: "
                             "alwaysThinkingEnabled + --effort max, Stufe per "
                             "ISU_THINKING_EFFORT; der Zahlenwert ist nur "
                             "an/aus), 0 = aus (MAX_THINKING_TOKENS=0). Landet "
                             "als [DENKT] im Journal und auf der Nameplate. "
                             "Default: ISU_THINKING_TOKENS, sonst aus. "
                             "Fable 5.1 denkt immer adaptiv (effort statt aus).")
    parser.add_argument("--loadout", default="",
                        help="Expansion-Loadout für den ERSTEN Spawn ohne "
                             "Inventar-Snapshot (z.B. IsuViktorLoadout.json); "
                             "leer = Mod-Default IsuSurvivorLoadout")
    parser.add_argument("--no-voice-procs", action="store_true",
                        help="Arena: Discord-Bot/Mikro laufen zentral, nicht hier")
    parser.add_argument("--keep-body", action="store_true",
                        help="Koerper beim Beenden NICHT despawnen (Default: "
                             "aufraeumen, damit keine stummen Huellen rumstehen)")
    parser.add_argument("--restore-only", action="store_true",
                        help="Nur Koerper sicherstellen + Inventar wiederherstellen, dann beenden")
    parser.add_argument("--br", action="store_true",
                        help="Battle-Royale: Free-for-all (jeder gegen jeden inkl. "
                             "Spieler), Funkstille, BR-Briefing + harter Persona-"
                             "Override. Die Aggro setzt die Mod via s_BrMode beim Spawn.")
    parser.add_argument("--no-respawn", action="store_true",
                        help="Nur 1 Leben: beim Tod NICHT respawnen, Runner beenden "
                             "(Leiche bleibt als Loot liegen). Für Battle-Royale.")
    parser.add_argument("--free", action="store_true",
                        help="Freier Survival-Modus: Persona-Lockerung (der Agent "
                             "entscheidet selbst, was er tut), 1 Leben (impliziert "
                             "--no-respawn), keine Rally-/Treffpunkt-Auftraege. "
                             "Kein BR: Funk bleibt an, niemand ist automatisch Feind.")
    args = parser.parse_args()
    # Gemini CLI ist seit 18.06.2026 ohne persönlichen Google-Zugang (jeder
    # Zug UNSUPPORTED_CLIENT). Direktstarts ohne Supervisor auch umleiten;
    # ISU_GEMINI_CLI_ALLOWED=1 lässt die Wahl durch (Code-Assist-Lizenz).
    _gemini_ok = os.environ.get("ISU_GEMINI_CLI_ALLOWED", "").strip().lower() in ("1", "true", "yes", "on")
    if args.model.lower().startswith("gemini-cli/") and not _gemini_ok:
        _gemini_choice = args.model
        args.model = "antigravity/default"
        print(f"[run_agent] Modell '{_gemini_choice}' -> 'antigravity/default' "
              f"(Gemini CLI ohne persoenlichen Zugang; ISU_GEMINI_CLI_ALLOWED=1 erzwingt).",
              flush=True)
    # Modellfehler vor jeglichem Spawn/Despawn melden.
    try:
        resolve_backend(args.model)
        if is_native_backend(args.model):
            validate_backend(args.model)
    except (ValueError, BackendError) as exc:
        _log_startup_validation_error(args.model, args.npc_id, args.home, exc)
        parser.error(str(exc))
    args.idle = max(10, args.idle)
    args.survival_interval = max(2.0, args.survival_interval)

    # Per-Agent-Pfade setzen (Arena: eigenes Home pro Agent)
    if args.home:
        AGENT_HOME = os.path.abspath(args.home)
    elif args.npc_id != "viktor":
        AGENT_HOME = os.path.join(REPO_DIR, "agent_homes", args.npc_id)
    os.makedirs(AGENT_HOME, exist_ok=True)
    SNAPSHOT_FILE = os.path.join(AGENT_HOME, "last_inventory.json")
    VOICE_INBOX = os.path.join(AGENT_HOME, "voice_inbox.jsonl")
    STOP_FLAG = os.path.join(AGENT_HOME, "stop.flag")
    if os.path.exists(STOP_FLAG):
        os.remove(STOP_FLAG)  # Altlast vom letzten Stop
    # Kritische Ereignisse brechen laufende Langaktionen des MCP ab (bridge
    # vergleicht die mtime mit dem Aktionsstart - Altlast ist damit harmlos).
    URGENT_FLAG = os.path.join(AGENT_HOME, "urgent_event.json")
    SPAWN_POS = (args.spawn_x, args.spawn_z)
    # Lager/Treffpunkt fürs Karten-Briefing: Rally (Mission/getrennter Spawn)
    # hat Vorrang, sonst das Menü-Lager = Spawnpunkt (MAP-1).
    if args.rally_x is not None and args.rally_z is not None and not args.free:
        CAMP_POS = (args.rally_x, args.rally_z)
    else:
        CAMP_POS = SPAWN_POS
    AGENT_NAME = args.name or args.npc_id.capitalize()
    AGENT_FACTION = args.faction
    AGENT_BR = 1 if args.br else 0
    AGENT_FREE = 1 if args.free else 0
    # Freier Modus = 1 Leben: den bestehenden --no-respawn-Mechanismus
    # wiederverwenden (Tod -> Runner endet sauber, Leiche bleibt als Loot).
    if args.free:
        args.no_respawn = True
    AGENT_LANG = (args.language or "de").lower()
    # Denk-Budget: CLI-Flag > Umgebung (ISU_THINKING_TOKENS, erbt der Runner
    # vom Arena-Supervisor) > Modul-Default. 0 schaltet das sichtbare Denken ab.
    if args.thinking is not None:
        AGENT_THINKING = max(0, args.thinking)
    else:
        try:
            AGENT_THINKING = max(0, int(os.environ.get("ISU_THINKING_TOKENS", "")
                                        or AGENT_THINKING))
        except ValueError:
            pass

    # Stimme aus dem Roster nachschlagen, wenn keine übergeben wurde -
    # der Einzelstart soll im Funk genauso klingen wie die Arena
    if not args.voice:
        try:
            with open(ROSTER_FILE, "r", encoding="utf-8") as f:
                for a in json.load(f).get("agents", []):
                    if a.get("id") == args.npc_id:
                        args.voice = a.get("voice", "")
                        break
        except (OSError, json.JSONDecodeError):
            pass

    claude_md = os.path.join(AGENT_HOME, "CLAUDE.md")
    if not os.path.exists(claude_md):
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(f"# Gedächtnis von {AGENT_NAME}\n\n## Orte\n\n## Personen\n\n"
                    f"## Taktiken\n\n## Lektionen\n")

    journal = Journal()
    # mode=free NACH model= und per Leerzeichen getrennt: league_report.py
    # HEADER_RE fasst model=([\w.,/-]+) - das Leerzeichen beendet die Gruppe,
    # der Zusatz bricht das Parsen also nicht (gegengeprüft).
    mode_note = " mode=free" if args.free else ""
    journal.log(f"=== IsuSurvivor Agent-Runner | npc={args.npc_id} ({AGENT_NAME}) "
                f"model={args.model}{mode_note} idle={args.idle}s ===")

    bridge = Bridge(args.profile, args.npc_id)
    INTENT_FILE = os.path.join(bridge.dir, f"intent_{args.npc_id}.txt")
    # Stale-Intent aus der VORIGEN Session löschen: sonst funkt der Orchestrator
    # beim Kaltstart noch die alte Gedankenzeile ("(will: ...)"), bis das Gehirn
    # die erste frische Absicht schreibt. Genau das zeigte die 22:50-Session:
    # "Birgit ... (will: Zelt-Bug-Eintrag)" stammte aus der Vorrunde.
    try:
        with open(INTENT_FILE, "w", encoding="utf-8") as _f:
            _f.write("")
        # Auch die native Begleitfassung der Vorrunde räumen (sonst funkt der
        # Orchestrator einen veralteten Originalvorsatz).
        _nintent = os.path.join(bridge.dir, f"intent_native_{args.npc_id}.txt")
        if os.path.exists(_nintent):
            os.remove(_nintent)
    except OSError:
        pass
    if bridge.state_fresh() is None:
        # Neuer Agenten-Slot: die Mod legt ihn an, sobald eine Command-Datei
        # auftaucht (TickDiscovery, alle 5 s). Ein ping erzeugt genau die.
        journal.log(f"Slot '{args.npc_id}' existiert noch nicht - wecke ihn per ping...")
        r = bridge.run("ping", timeout=25)
        if r.get("status") != "done" or bridge.state_fresh() is None:
            journal.log("FEHLER: Bridge antwortet nicht. Server mit tools\\start_server.ps1 starten.")
            return 1
        journal.log(f"Slot '{args.npc_id}' lebt.")
    if not ensure_body(bridge, journal, restore=not args.no_restore,
                       loadout_default=args.loadout,
                       fresh=args.fresh_loadout):
        return 1

    state0 = bridge.read_state() or {}
    save_inventory_snapshot(state0)

    if args.restore_only:
        journal.log("=== --restore-only erledigt ===")
        return 0

    # Spieler-Komfort: zum NPC teleportieren (sofort oder sobald er joint).
    # tp_requested = Spieler hat "tp" im Chat/Funk gesagt (jederzeit möglich).
    tp_pending = not args.no_tp
    tp_requested = False

    def try_tp() -> bool:
        state = bridge.read_state() or {}
        if state.get("command", {}).get("status") == "running":
            return False  # laufenden Befehl des Gehirns nicht verdrängen
        r = bridge.run("teleport_player", timeout=10)
        return r.get("status") == "done"

    if tp_pending:
        if try_tp():
            journal.log("Spieler zum NPC teleportiert.")
            tp_pending = False
        else:
            journal.log("Noch kein Spieler verbunden - teleportiere, sobald jemand joint.")

    os.makedirs(AGENT_HOME, exist_ok=True)
    # Rundenkosten-Export zurücksetzen - sonst summiert der Supervisor die
    # Restwerte der letzten Runde mit.
    _write_round_cost(None if is_native_backend(args.model) else 0.0)

    # Voice-Datei-Hygiene VOR dem Claude-Start, sonst frisst das Löschen
    # die ersten Äußerungen (Arena-Modus: der Supervisor übernimmt das)
    own_outbox = os.path.join(AGENT_HOME, "voice_outbox.jsonl")
    if not args.no_voice_procs:
        for stale in (own_outbox, VOICE_INBOX):
            if os.path.exists(stale):
                os.remove(stale)

    mcp_cfg = build_mcp_config(args.profile, args.npc_id, args.voice)
    _cli_model, _benv, backend = resolve_backend(args.model)
    if backend == "anthropic-api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            journal.log("!!! api/-Modell gewählt, aber ANTHROPIC_API_KEY fehlt "
                        "- Claude Code fällt sonst auf den Max-Plan-Login "
                        "zurück oder scheitert. Key setzen (dauerhaft): "
                        "setx ANTHROPIC_API_KEY sk-ant-...")
        else:
            journal.log(f"Backend: {args.model} -> {_cli_model} über die "
                        f"Anthropic-API (eigener API-Key, Kosten pro Token, "
                        f"NICHT über den Max-Plan).")
    elif is_native_backend(args.model):
        journal.log(f"Backend: {backend}; gemeinsame DayZ-Werkzeuge und Gedächtnis. "
                    "Codex/Gemini/Antigravity CLI nutzen den vorhandenen Login mit dessen Limits; "
                    "Mistral/Moonshot werden über den eigenen API-Key abgerechnet. "
                    "Kein automatischer Anbieterwechsel.")
    elif backend != "anthropic":
        journal.log(f"Fremd-Backend: {args.model} -> {backend} - der Dienst "
                    f"muss laufen (Arena-Menü startet ihn automatisch, "
                    f"Einzelstart: tools\\start_router.ps1 bzw. "
                    f"tools\\start_llama_gemma.ps1). Kosten laufen NICHT "
                    f"über den Max-Plan; USD-Anzeige im Journal gilt nur "
                    f"für Claude-Modelle.")
    proc = spawn_claude(mcp_cfg, args.model, args.character, args.turn_limit)
    if _cli_model == "claude-fable-5-1":
        think_note = ("Adaptives Denken immer aktiv; effort="
                      + os.environ.get("ISU_THINKING_EFFORT", "low"))
    elif AGENT_THINKING > 0 and backend in ("anthropic", "anthropic-api"):
        think_note = ("Denken sichtbar (alwaysThinkingEnabled + --effort "
                      + os.environ.get("ISU_THINKING_EFFORT", "max")
                      + ", [DENKT])")
    elif AGENT_THINKING > 0 and args.model.lower().startswith("xai/"):
        # Grok denkt von selbst; der reasoning-Transformer in der Router-Config
        # (~/.claude-code-router/config.json, xai-Provider) übersetzt die
        # Denkspur in thinking-Blöcke. Live verifiziert 29.08.2026 (2 Turns
        # inkl. Historie-Rückweg, kein 400).
        think_note = "Denken sichtbar via Router-reasoning-Transformer ([DENKT])"
    elif AGENT_THINKING > 0:
        think_note = "Denken nicht verfügbar (Backend liefert keine Thinking-Blöcke)"
    else:
        think_note = "Denken aus"
    journal.log(f"Gehirn gestartet (PID {proc.pid}, Modell {args.model}, "
                f"Backend {backend}, Zug-Limit {args.turn_limit or 'aus'}, "
                f"{think_note}).")

    # Discord-Voice-Brücke: startet mit, wenn ein Bot-Token gesetzt ist
    discord_proc = None
    mic_proc = None
    inbox = InboxReader()

    if args.no_voice_procs:
        journal.log("Arena-Modus: Discord-Bots und Mikro-Router laufen zentral.")
    if not args.no_voice_procs and os.environ.get("DISCORD_BOT_TOKEN"):
        dv_args = [sys.executable, os.path.join(DAEMON_DIR, "discord_voice.py"),
                   "--label", args.npc_id,
                   "--outbox", own_outbox,
                   "--inbox", VOICE_INBOX]
        # Hört das lokale Mikro mit (mic_listener), darf der Bot nicht
        # zusätzlich hören - sonst kommt jeder Funkspruch doppelt an
        if not args.no_mic and os.environ.get("ELEVENLABS_API_KEY"):
            dv_args.append("--no-listen")
        discord_proc = subprocess.Popen(
            dv_args,
            stdout=open(os.path.join(AGENT_HOME, "journal", "discord_voice.log"),
                        "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        journal.log(f"Discord-Voice gestartet (PID {discord_proc.pid}).")
        if not os.environ.get("ELEVENLABS_API_KEY"):
            journal.log("!!! ELEVENLABS_API_KEY fehlt: Bot ist im Kanal, aber "
                        "STUMM (kein TTS) und TAUB (kein STT). !!!")
    elif not args.no_voice_procs:
        # Nur im Solo-Betrieb warnen. Im Arena-Modus laufen die Bots zentral
        # beim Supervisor - der Runner sieht den Token absichtlich nicht, die
        # Warnung war dort ein Fehlalarm (Voice lief längst, 03.07.).
        journal.log("!" * 60)
        journal.log("!!! Kein DISCORD_BOT_TOKEN gesetzt - Discord-Voice bleibt AUS.")
        journal.log("!!! say erscheint dann NUR im Spiel-Textchat, nicht im Funk.")
        journal.log("!!! Setup: docs/discord_bot_setup.md, Schritt 5.")
        journal.log("!" * 60)

    # Mikrofon-Hören (lokal): Discord-Empfang ist durch DAVE-E2EE blockiert,
    # dein Mikro füttert dieselbe Funk-Inbox
    if not args.no_voice_procs and not args.no_mic and os.environ.get("ELEVENLABS_API_KEY"):
        mic_proc = subprocess.Popen(
            [sys.executable, os.path.join(DAEMON_DIR, "mic_listener.py"),
             "--inbox", VOICE_INBOX, "--parent-pid", str(os.getpid())],
            stdout=open(os.path.join(AGENT_HOME, "journal", "mic_listener.log"),
                        "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        journal.log(f"Mikrofon-Hören gestartet (PID {mic_proc.pid}) - "
                    f"{AGENT_NAME} hört dich über dein Mikro.")
    elif not args.no_mic:
        journal.log("Mikrofon-Hören AUS (ELEVENLABS_API_KEY fehlt).")

    watcher = EventWatcher(bridge, greet_initial=True)
    tracker = TokenTracker()
    reader = BrainReader(proc, journal, tracker)
    reader.start()
    turns = 0

    # Wipe-/Neustart-Erkennung: neue Welt-Generation seit dem letzten Lauf
    # => das Gehirn warnen, BEVOR es alten Bestands-Erinnerungen vertraut.
    wipe_hint = ""
    gen_file = os.path.join(AGENT_HOME, "world_gen.txt")
    gen_now = world_generation(args.profile)
    if gen_now:
        gen_old = ""
        try:
            with open(gen_file, "r", encoding="utf-8") as f:
                gen_old = f.read().strip()
        except OSError:
            pass
        if gen_old and gen_old != gen_now:
            wipe_hint = (
                "WICHTIG - SERVER-NEUSTART SEIT DEINEM LETZTEN EINSATZ: Die "
                "Welt wurde neu geladen. Abgelegte Items, Zelt-/Depot-Inhalte, "
                "Leichen und alles, was niemand am Koerper trug, sind sehr "
                "wahrscheinlich WEG. Vertraue keiner Bestands-Erinnerung: "
                "pruefe vor Ort mit observe und loesche veraltete "
                "Bestands-Eintraege sofort aus deinem Gedaechtnis "
                "(CLAUDE.md und memory/*.md). ")
            journal.log(f"[WIPE] Neue Welt-Generation: {gen_old} -> {gen_now}")
        try:
            with open(gen_file, "w", encoding="utf-8") as f:
                f.write(gen_now + "\n")
        except OSError:
            pass

    # Getrennter Spawn: der Agent ist allein in der Wildnis und soll selbst zum
    # Treffpunkt (= Lager) marschieren und sich dort mit der Gruppe vereinen -
    # ohne den Spieler, koordiniert per Funk. Der Treffpunkt kommt per --rally.
    # Freier Modus: KEINE Rally-/Treffpunkt-Injektionen - die Agenten sind
    # frei (auch falls der Aufrufer versehentlich --rally mitgibt).
    rally_active = (args.rally_x is not None and args.rally_z is not None
                    and not args.free)
    if args.once:
        first_msg = args.once
    elif args.mission:
        first_msg = args.mission
    elif args.br and rally_active:
        # Getrennter BR-Spawn: allein, Marsch zum Treffpunkt-Showdown.
        first_msg = (
            "BATTLE ROYALE. Du erwachst ALLEIN, weit weg von den anderen. Alle "
            "anderen Überlebenden UND der Spieler sind deine Gegner - keine "
            "Verbündeten, kein Funk, keine Absprachen. Du hast nur 1 Leben. Mach "
            "zuerst observe, halte mit equip_best deine Waffe bereit und brich zum "
            f"Treffpunkt bei x={args.rally_x:.0f} z={args.rally_z:.0f} auf - dort "
            "wird es entschieden. Du startest leicht: loote unterwegs Waffen, "
            "Munition, Schutz. Triffst du jemanden BEVOR du ankommst, kämpfe "
            f"sofort. Starte den Marsch mit travel_to(x={args.rally_x:.0f}, "
            f"z={args.rally_z:.0f}) - das läuft im Hintergrund, weicht "
            "Hindernissen selbst aus und du bleibst während des Marsches "
            "handlungsfähig. Zuerst schießen, dann denken."
        )
    elif args.br:
        # Gruppen-BR-Spawn: alle eng beisammen -> sofortiges Gefecht (Schnelltest).
        first_msg = (
            "BATTLE ROYALE - SOFORT-GEFECHT. Du erwachst MITTEN UNTER deinen "
            "Rivalen, sie stehen direkt um dich herum. Alle anderen Ueberlebenden "
            "UND der Spieler sind Gegner - keine Verbuendeten, kein Funk. Du hast "
            "nur 1 Leben. Mach SOFORT observe, zieh mit equip_best deine Waffe und "
            "greif den naechsten Gegner an (engage). Keine Sekunde zoegern, kein "
            "Marsch - zuerst schiessen, dann denken."
        )
    elif args.free:
        # Freier Survival-Modus: kein Auftrag, kein Treffpunkt, keine
        # Spieler-Nähe-Vorrang-Regel - nur das eine Ziel: überleben.
        first_msg = (
            "Du erwachst in der Wildnis. Es gibt keinen Auftrag und keinen "
            "Treffpunkt - du bist völlig frei und entscheidest selbst, was du "
            "tust: kooperieren, alleine ziehen, siedeln, horten, erkunden. "
            "Aber merke dir: du hast nur EIN Leben, der Tod ist endgültig. "
            "Mach zuerst observe, sorge für Ausrüstung, Wärme und Nahrung "
            "und überlebe so lange wie möglich."
        )
    elif rally_active:
        first_msg = (
            "Du erwachst ALLEIN in einer fremden Gegend, weit weg von deiner "
            "Gruppe. Der Spieler ist NICHT hier - ihr koordiniert euch selbst per "
            "Funk (say). Mach zuerst observe, dann brich zum Treffpunkt bei "
            f"x={args.rally_x:.0f} z={args.rally_z:.0f} auf. Der Weg ist WEIT "
            "(oft mehrere Kilometer) - starte den Marsch mit "
            f"travel_to(x={args.rally_x:.0f}, z={args.rally_z:.0f}): das läuft "
            "im Hintergrund, weicht Hindernissen selbst aus, und du meldest "
            "dich einfach wieder, wenn das REISE-Ereignis kommt. NICHT move_to "
            "in Schleife rufen. EINE Ausnahme: taucht unterwegs der Spieler "
            "(ein Mensch, kind player in observe) nah bei dir auf, bleib bei ihm, "
            "begrüße ihn und warte auf seine Anweisung, statt an ihm "
            "vorbeizumarschieren. Sorge unterwegs für dich (Wärme, Nahrung, "
            "trocken bleiben). Funk kurz, wo du bist und dass du unterwegs zum "
            "Treffpunkt bist; sobald ihr euch gefunden habt, bleibt zusammen."
        )
    else:
        first_msg = (
            "Du bist gerade aufgewacht. Verschaffe dir mit observe einen "
            "Ueberblick und handle nach deinen Prioritaeten. Fasse am Ende kurz "
            "zusammen, wie es dir geht und was dein Plan ist."
        )
    if wipe_hint:
        first_msg = wipe_hint + first_msg
    # Kartenwechsel seit dem letzten Lauf (MAP-1) ganz vorne melden, damit das
    # Gehirn Erinnerungen an die alte Karte nicht für bare Münze nimmt.
    map_hint = map_change_notice(AGENT_HOME)
    if map_hint:
        first_msg = map_hint + first_msg
        journal.log("[KARTE] " + map_hint.strip()[:200])

    # Zug-Erkennung statt pending-Zähler (siehe TurnGate). Mit sichtbarem
    # Denken kommt das erste Event eines Folgezuges spät - die Rotation wartet
    # dann länger, damit sie keinen gerade anlaufenden Zug abwürgt.
    gate = TurnGate(native=is_native_backend(args.model))
    thinking_active = (_cli_model == "claude-fable-5-1"
                       or (AGENT_THINKING > 0 and backend in ("anthropic", "anthropic-api")))
    rotate_quiet = IDLE_QUIET_ROTATE_THINKING if thinking_active else IDLE_QUIET_ROTATE

    def log_wakeup(msg: str, note: str = "") -> None:
        """[WECKRUF] (200 Zeichen wie bisher) plus - wenn gekürzt - der Inhalt
        bis 1200 Zeichen als [WECKRUF-INHALT] (VI-13: LAGE-/EREIGNIS-Teile
        waren hinter Präfixen nicht mehr nachvollziehbar)."""
        flat = msg.replace("\n", " | ")
        journal.log("[WECKRUF] " + note + flat[:200])
        if len(flat) > 200:
            journal.log("[WECKRUF-INHALT] " + flat[:1200])

    def collect_events() -> tuple[list[str], dict]:
        """Welt-Ereignisse + Funk einsammeln (tp-Bitten herausgefiltert)."""
        nonlocal tp_requested
        events, state = watcher.poll()
        # Battle-Royale = Funkstille: keinen Funk lesen (auch keine tp-Bitte),
        # damit sich die Gegner nicht koordinieren können (R1).
        for funk in (inbox.poll() if not AGENT_BR else []):
            text = (funk.get("text") or "").strip()
            if text.lower().rstrip(".!") == "tp":
                tp_requested = True
                continue
            # Routine-Sitreps des Lagezentrums wandern in den Info-Digest
            # (kein eigener Zug), Prio-Funk und alles andere weckt normal.
            ev = watcher.route_funk(funk)
            if ev:
                events.append(ev)
        # Chat-Kommando "tp": Spieler will zum NPC teleportiert werden
        kept = []
        for e in events:
            if e.startswith("CHAT von ") and e.rstrip().lower().endswith('"tp"'):
                tp_requested = True
            else:
                kept.append(e)
        return kept, state

    def take_fresh_events() -> list[str]:
        """Einmal frisch einsammeln für eine NEUE Nachricht (Start, Rotation,
        Respawn). Ein Tod bleibt im Puffer für die Hauptschleife."""
        evs, _st = collect_events()
        now_m = time.monotonic()
        rest = []
        for e in evs:
            if e == "DU BIST GESTORBEN.":
                buffered.append(e)
                buffered_at.setdefault(e, now_m)
            else:
                rest.append(e)
        return rest

    # Hauptschleife: dringende Ereignisse werden auch MITTEN im Zug
    # eingespeist (Claude Code hängt sie an den nächsten Tool-Schritt),
    # Routine wartet auf echten Leerlauf (TurnGate).
    buffered: list[str] = []
    buffered_at: dict = {}    # Ereignis -> monotonic der Pufferung (VI-9)
    try:
        # Startlage IN die erste Nachricht falten statt sie 1 s später als
        # zweite hinterherzuschicken: genau diese Doppel-Sendung verschmolz
        # am 24.10. bei jedem NPC zum ersten Zug und verstellte den Zähler.
        start_events = take_fresh_events()
        first_msg = fold_wakeup(first_msg, info=watcher.drain_info(),
                                events=format_buffered(start_events, {},
                                                       time.monotonic(), watcher),
                                smalltalk=watcher.drain_smalltalk(),
                                events_title="AKTUELL BEIM ERWACHEN")
        send_user(proc, first_msg)
        gate.note_send(reader, brake=False)
        log_wakeup(first_msg)
        interrupted = False
        buffer_deadline = 0.0     # 0 = kein offenes Bündel
        idle_deadline = time.monotonic() + args.idle
        snapshot_tick = 0
        last_ctx = 0              # Kontextgröße des letzten Zuges (Tokens)
        turns_since_rotate = 0    # Züge seit der letzten frischen Session
        rotate_pending = False    # Kontext-Schwelle gerissen: Swap folgt, sobald
                                  # das Gehirn im Leerlauf ist (transaktional)
        # CCR (Gemini/OpenAI/xAI) und lokaler llama-server melden keine
        # Token-Usage -> last_ctx bleibt 0 -> der Token-Trigger feuert nie.
        # Auf diesen Backends den zugbasierten Fallback (CTX_ROTATE_TURNS) nutzen.
        usage_blind = backend not in ("anthropic", "anthropic-api")
        death_times: list[float] = []   # Tod-Zeitpunkte für die Schleifen-Bremse
        # Adaptiver Idle-Backoff (Audit 03.07.): Routine-Ticks alle args.idle s
        # liefen auch, wenn nichts passierte und kein Spieler da war - reine
        # Token-Verbrennung. Der Faktor (1 -> 2 -> 4, Cap 4) streckt NUR den
        # Routine-Tick-Abstand; nach jedem Tick ohne vorheriges echtes Ereignis
        # verdoppelt er sich. Zurück auf 1 bei jedem echten Event-Dispatch
        # (Spieler-Funk/Chat inklusive) oder sobald ein Mensch in Sichtweite
        # ist (watcher.known_players). Im BR-Modus fest 1 - dort zählt Tempo.
        idle_backoff = 1
        event_since_tick = True   # echtes Ereignis seit dem letzten Routine-Tick?
        survival_deadline = time.monotonic() + args.survival_interval
        survival_memory = os.path.join(AGENT_HOME, "survival_learning.json")
        survival_handoff_at = 0.0
        survival_handoff = ""
        survival_busy = False
        last_brain_failed = False
        backend_retry_at = 0.0
        backend_failures = 0
        # Login abgelaufen: Gehirn pausiert, lokale Survival-Routinen laufen
        # weiter; alle AUTH_RECHECK_SEC lokal 'claude auth status' (kein Modellaufruf).
        auth_blocked = False
        auth_check_at = 0.0
        try:
            os.remove(_auth_error_file())
        except OSError:
            pass

        def dispatch(msg: str, routine: bool = False):
            nonlocal interrupted, idle_deadline, idle_backoff, event_since_tick
            # Info-Digest (Routine-Sitrep, Wetter, Fahrzeug) fährt als
            # Kenntnis-Prefix mit dem echten Weckruf mit - löst also nie
            # selbst einen Zug aus, geht aber auch nie verloren.
            info = watcher.drain_info()
            if info:
                msg = "LAGE (nur zur Kenntnis): " + info + "\n\n" + msg
            if interrupted:
                msg = ("(Dein voriger Zug wurde am Aktionslimit gekappt. "
                       "Falls du mitten in etwas warst, kannst du es jetzt "
                       "fortsetzen - es sei denn, unten steht Dringenderes.)\n\n"
                       + msg)
                interrupted = False
            if not routine:
                # Echtes Ereignis: Backoff sofort aufheben, volle Aufmerksamkeit.
                event_since_tick = True
                if idle_backoff != 1:
                    idle_backoff = 1
                    journal.log(f"[IDLE] zurück auf {args.idle}s")
            send_user(proc, msg)
            mid_turn = gate.note_send(reader)
            log_wakeup(msg, "(mitten im Zug) " if mid_turn else "")
            idle_deadline = time.monotonic() + args.idle

        while True:
            # Stop-Flag vom Supervisor? Wie Strg+C behandeln, damit der
            # finally-Block läuft (Inventar sichern, despawnen).
            if os.path.exists(STOP_FLAG):
                journal.log("Stop-Flag vom Supervisor erkannt - beende sauber.")
                raise KeyboardInterrupt

            # Beendete Züge abholen (nicht blockierend)
            try:
                while True:
                    result = reader.results.get_nowait()
                    if result is None:
                        return 1
                    saldo = gate.note_result()
                    if saldo:
                        journal.log(saldo)
                    turns += 1
                    turns_since_rotate += 1
                    interrupted = result.get("subtype") == "error_max_turns"
                    last_brain_failed = bool(result.get("is_error"))
                    if result.get("subtype") == "error_backend":
                        backend_failures += 1
                        retry_delay = min(300, 15 * 2 ** min(backend_failures - 1, 5))
                        backend_retry_at = time.monotonic() + retry_delay
                        journal.log(f"[BACKEND] {retry_delay}s Pause vor neuem Modellaufruf; "
                                    "lokale Survival-Routinen bleiben aktiv.")
                    elif not result.get("is_error"):
                        backend_failures = 0
                        backend_retry_at = 0.0
                    if reader.auth_error and not is_native_backend(args.model):
                        err = reader.auth_error
                        reader.auth_error = ""
                        if not auth_blocked:
                            journal.log("[AUTH] Claude-Login abgelaufen oder ungültig: " + err[:160]
                                        + " | Gehirn pausiert, lokale Survival-Routinen laufen "
                                        "weiter. Abhilfe: neues Terminal -> 'claude auth login' "
                                        "(oder 'claude' und /login). Danach geht es automatisch weiter.")
                            try:
                                with open(_auth_error_file(), "w", encoding="utf-8") as f:
                                    f.write(err[:300] + "\n")
                            except OSError:
                                pass
                        auth_blocked = True
                        _set_nameplate_intent(AUTH_NAMEPLATE)
                        backend_retry_at = time.monotonic() + 365 * 86400.0
                        auth_check_at = time.monotonic() + AUTH_RECHECK_SEC
                    idle_deadline = time.monotonic() + args.idle
                    # Kontextgröße = LETZTER einzelner API-Schritt (BrainReader),
                    # NICHT die Zug-Summe aus result.usage: die addiert cache_read
                    # über alle Schritte und löste bei jedem mehrstufigen Zug
                    # eine Rotation aus (Thrashing: Session-Reset alle ~15 s,
                    # Arbeitsgedächtnis weg, NPCs wirkten kopflos, 02.07.).
                    last_ctx = reader.last_step_ctx
                    if args.once:
                        journal.log("=== --once erledigt ===")
                        return 1 if last_brain_failed else 0
                    if args.max_turns and turns >= args.max_turns:
                        journal.log(f"=== max-turns ({args.max_turns}) erreicht ===")
                        return 1 if last_brain_failed else 0
            except queue.Empty:
                pass
            if reader.dead:
                return 1

            # Login wieder da? Nur lokal prüfen; erst dann neue Modellzüge.
            if auth_blocked and time.monotonic() >= auth_check_at:
                auth_check_at = time.monotonic() + AUTH_RECHECK_SEC
                logged_in, detail = preflight.claude_auth_status(CLI_LAUNCH)
                if logged_in:
                    auth_blocked = False
                    backend_retry_at = 0.0
                    try:
                        os.remove(_auth_error_file())
                    except OSError:
                        pass
                    journal.log(f"[AUTH] Login wieder gültig ({detail}) - Gehirn läuft weiter.")

            # Kontext-Schwelle prüfen: NUR das Flag setzen (transaktionale
            # Rotation). Der eigentliche Proc-Swap unten wartet, bis das
            # Gehirn wirklich im Leerlauf ist (TurnGate) - so zerreißt die
            # Rotation nie einen halben Zug. Token-Trigger (last_ctx)
            # ODER - auf usage-blinden Backends, wo last_ctx 0 bleibt - der
            # zugbasierte Fallback (sonst wächst die Gemini/CCR-Session bis
            # "Prompt is too long").
            if (not rotate_pending
                    and (last_ctx > CTX_ROTATE
                         or (usage_blind and turns_since_rotate >= CTX_ROTATE_TURNS))):
                rotate_pending = True
                journal.log(f"[ROTATION] vorgemerkt (Tokens={last_ctx}, "
                            f"Zuege={turns_since_rotate}, usage_blind={usage_blind}) "
                            f"- Swap, sobald das Gehirn im Leerlauf ist.")

            # Session-Rotation (transaktional): Schwelle gerissen UND kein Zug
            # offen, seit rotate_quiet s keine Ausgabe (ein am Zugende
            # nachgeschobener Weckruf startet als eigener Folgezug - den
            # darf der Swap nicht abwürgen). Das Gedächtnis liegt in
            # CLAUDE.md/memory und bleibt; den Arbeitsfaden trägt die
            # Handover-Notiz hinüber (kein Sicherungs-Zug mehr nötig).
            if rotate_pending and gate.idle(reader, rotate_quiet):
                journal.log(f"[ROTATION] Kontext war {last_ctx} Tokens - "
                            f"starte frische Session.")
                # Handover-Material VOR dem Swap einsammeln: Gedankenfaden,
                # Follow-Ziel und letzter Vorsatz (intent-Datei) der alten Session.
                carry_recent = list(reader.recent)
                carry_follow = reader.follow_target
                try:
                    proc.stdin.close()
                    proc.wait(timeout=10)
                except Exception:
                    _kill_brain(proc)
                proc = spawn_claude(mcp_cfg, args.model, args.character,
                                    args.turn_limit)
                reader = BrainReader(proc, journal, tracker)
                reader.follow_target = carry_follow  # überlebt die Rotation
                reader.start()
                rotate_pending = False
                last_ctx = 0
                turns_since_rotate = 0
                interrupted = False   # frische Session hat keinen gekappten Vorzug
                last_intent = ""
                try:
                    ipath = os.path.join(bridge.dir, f"intent_{bridge.npc_id}.txt")
                    if os.path.exists(ipath):
                        with open(ipath, "r", encoding="utf-8",
                                  errors="replace") as f:
                            last_intent = f.read().strip()
                except OSError:
                    pass
                # Stichpunkt-Format statt Prosa (Audit 03.07.): gleicher
                # Inhalt (Gedächtnis-Hinweis, Intent, Follow, Faden), ~60%
                # weniger Weckruf-Tokens.
                handover = ("KONTEXT-WECHSEL (frische Session; Gedaechtnis in "
                            "CLAUDE.md/memory erhalten).")
                # Frische Session startet sonst mit englischem Default-Ton
                # (Viktor 24.10.: "I'll check the situation first.").
                if AGENT_LANG == "de":
                    handover = "Denke und funke auf Deutsch. " + handover
                if last_intent:
                    handover += f' Zuletzt: "{last_intent}".'
                if carry_follow:
                    handover += f" Du folgst: {carry_follow}."
                if carry_recent:
                    last_few = " | ".join(s[:120] for s in carry_recent[-3:])
                    handover += f" Letzte Gedanken: {last_few}."
                handover += (" Kurz observe, dann deinen Plan nahtlos "
                             "fortsetzen - nicht bei null anfangen.")
                # Lage und Gepuffertes IN den Handover falten (VI-6): die
                # frische Session bekommt EINE Nachricht, kein Doppel-Send.
                pend = format_buffered(buffered, buffered_at, time.monotonic(), watcher)
                buffered = []
                buffered_at = {}
                buffer_deadline = 0.0
                pend += take_fresh_events()
                handover = fold_wakeup(handover, info=watcher.drain_info(),
                                       events=pend, smalltalk=watcher.drain_smalltalk(),
                                       events_title="SEIT DEINEM LETZTEN ZUG")
                gate.reset()
                send_user(proc, handover)
                gate.note_send(reader, brake=False)
                log_wakeup(handover, "(nach Rotation) ")
                time.sleep(1.0)
                continue

            # Welt-Ereignisse einsammeln
            kept, state = collect_events()
            now_m = time.monotonic()
            for e in kept:
                buffered_at.setdefault(e, now_m)
            buffered += kept
            for ev in kept:
                if _is_abortable(ev):
                    if write_urgent_event(URGENT_FLAG, ev):
                        journal.log("[DRINGEND] " + ev[:110].replace("\n", " ")
                                    + " - " + _urgent_note(state))
                    break
            if (tp_pending or tp_requested) and try_tp():
                journal.log("Spieler zum NPC teleportiert.")
                tp_pending = False
                tp_requested = False

            snapshot_tick += 1
            if snapshot_tick % 5 == 0:
                save_inventory_snapshot(state)

            # Tod hat Vorrang: neuen Körper besorgen, Meldung sofort senden
            if "DU BIST GESTORBEN." in buffered:
                # SOFORT (vor allem anderen): Handover-Material sichern, die
                # Todes-Epoche schreiben (C1, sperrt das MCP des alten Gehirns)
                # und das alte Gehirn hart beenden. Bis 24.10. lief es bis nach
                # Spawn + Restore weiter (Birgit: 77 s), hielt den Respawn für
                # eine "Fahrzeugfahrt", lootete am Lager und verdrängte die
                # Restore-Befehle - 13 Items samt Waffen gingen verloren.
                carry_recent = list(reader.recent)
                died_in_vehicle = bool(watcher.in_vehicle)
                died_uncon = bool(watcher.uncon_warned)
                roster_snapshot = list(watcher.roster_names)
                _write_death_epoch()
                _kill_brain(proc)
                try:
                    reader.join(timeout=3.0)   # Rest-Ausgabe des Toten abarbeiten
                except Exception:
                    pass
                # Erst NACH dem join leeren: sonst schreibt eine gepufferte
                # Zeile des alten Gehirns die Gedankenzeile wieder voll.
                _clear_intent_files()
                journal.log("Gehirn beim Tod sofort beendet (Todes-Epoche gesetzt, "
                            "Gedankenzeile geleert).")
                # Den letzten lokalen Versuch vor Respawn/Exit abschließen;
                # ein toter Snapshot kann keine neue Spielaktion starten.
                if not args.no_autonomy:
                    survival_tick(bridge, agent_name=AGENT_NAME,
                                  memory_path=survival_memory, allow_explore=False)
                # Battle-Royale: nur 1 Leben. Kein Respawn - der Agent scheidet
                # aus, die Leiche bleibt als Loot liegen, der Runner endet sauber
                # (finally räumt Voice/Mic auf, despawnt aber nicht).
                if args.no_respawn:
                    if args.free:
                        journal.log("[FREE] ENDGUELTIG GESTORBEN - freier Modus, "
                                    "1 Leben aufgebraucht, kein Respawn. Leiche "
                                    "bleibt liegen, Runner beendet.")
                    else:
                        journal.log("[BR] ELIMINIERT - 1 Leben aufgebraucht, kein "
                                    "Respawn. Leiche bleibt liegen, Runner beendet.")
                    args.keep_body = True
                    return 0
                now = time.monotonic()
                death_times.append(now)
                death_times[:] = [t for t in death_times
                                  if now - t <= DEATH_WINDOW_SEC]

                # Todes-Schleife: zu viele Tode in zu kurzer Zeit. Aussichtslos
                # (Spawn-Glitch/tödliche Zone) - NICHT sofort eine neue, teure
                # Claude-Session starten, sondern pausieren. Körper despawnen,
                # Cooldown abwarten (Stop-Flag bleibt wirksam), dann ein Versuch.
                # Das Gehirn ist oben schon beendet.
                if len(death_times) >= DEATH_LOOP_MAX:
                    journal.log(
                        f"[TOD-CAP] {len(death_times)} Tode in "
                        f"<{DEATH_WINDOW_SEC:.0f}s - Todes-Schleife. Pausiere "
                        f"{DEATH_COOLDOWN_SEC:.0f}s OHNE neue Session (spart "
                        f"Tokens); danach ein neuer Versuch.")
                    try:
                        bridge.run("despawn", timeout=10)
                    except Exception:
                        pass
                    cd_end = time.monotonic() + DEATH_COOLDOWN_SEC
                    while time.monotonic() < cd_end:
                        if os.path.exists(STOP_FLAG):
                            raise KeyboardInterrupt
                        time.sleep(2.0)
                    death_times.clear()      # nach der Pause neuer Anlauf
                    ensure_body(bridge, journal, restore=not args.no_restore,
                                loadout_default=args.loadout,
                                fresh=args.fresh_loadout)
                    proc = spawn_claude(mcp_cfg, args.model, args.character,
                                        args.turn_limit)
                    reader = BrainReader(proc, journal, tracker)
                    reader.start()
                    watcher = EventWatcher(bridge)
                    buffered = []
                    buffered_at = {}
                    buffer_deadline = 0.0
                    rotate_pending = False
                    last_ctx = 0
                    turns_since_rotate = 0
                    interrupted = False
                    idle_backoff = 1          # frisches Leben = volle Aufmerksamkeit
                    event_since_tick = True
                    rally_hint = ""
                    if rally_active:
                        rally_hint = (f" Danach: allein zum Treffpunkt "
                                      f"x={args.rally_x:.0f} z={args.rally_z:.0f}, "
                                      f"Gruppe per Funk finden.")
                    # Squad und Waffenstatus wie beim normalen Respawn (VI-M6).
                    cap_state = bridge.read_state() or {}
                    cap_npc = cap_state.get("npc") or {}
                    cap_extra = ""
                    if not AGENT_BR and cap_npc.get("spawned"):
                        sq = _squad_lines(bridge.npc_id, cap_npc.get("pos_x", 0.0),
                                          cap_npc.get("pos_z", 0.0))
                        if sq:
                            cap_extra += f" Squad jetzt: {sq}."
                    if not weapon_status(cap_state)[0]:
                        cap_extra += " Du bist UNBEWAFFNET - unterwegs eine Waffe suchen."
                    # Stichpunkt-Format statt Prosa (Audit 03.07.), Inhalt
                    # komplett: observe, Todesort meiden, Rally, Gedächtnis.
                    cap_msg = ("NEU ERWACHT NACH TODES-SERIE. Zuerst observe - "
                               "alte Lage gilt NICHT mehr. Dein Todesort ist "
                               "toedlich: meide ihn, erst Sicherheit (Distanz, "
                               "Deckung), dann Risiko." + cap_extra + rally_hint +
                               " Erinnerungen (CLAUDE.md/memory) bleiben.")
                    if AGENT_LANG == "de":
                        cap_msg = "Denke und funke auf Deutsch. " + cap_msg
                    cap_msg = fold_wakeup(cap_msg, info=watcher.drain_info(),
                                          events=take_fresh_events(),
                                          smalltalk=watcher.drain_smalltalk())
                    gate.reset()
                    send_user(proc, cap_msg)
                    gate.note_send(reader, brake=False)
                    log_wakeup(cap_msg, "(TOD-CAP) ")
                    journal.log("[TOD-CAP] Cooldown vorbei - frische Session, "
                                "Vorsicht-Weckruf gesendet")
                    time.sleep(2.0)
                    continue

                journal.log(f"[TOD] Koerper verloren ({len(death_times)} in "
                            f"<{DEATH_WINDOW_SEC:.0f}s), spawne neuen...")
                # Leichen-Position merken, BEVOR der neue Körper spawnt -
                # die Leiche bleibt lootbar (kind=corpse) liegen
                dead_state = bridge.read_state() or {}
                dead_npc = dead_state.get("npc", {})
                # Koop-Gruppenmodus: neben dem nächsten Menschen erwachen statt
                # am Lager (HUD-2/DR-4: Birgit landete am 24.10. 1,6 km vom
                # Spieler entfernt). Nicht im BR/Free-Modus, nicht bei Rally/
                # Mission, nicht mit Infizierten am Menschen; ISU_RESPAWN_AT_CAMP=1
                # erzwingt das alte Verhalten. (TOD-CAP spawnt bewusst am Lager.)
                spawn_pos, woke_near = None, None
                camp_forced = (os.environ.get("ISU_RESPAWN_AT_CAMP", "").strip().lower()
                               in ("1", "true", "yes", "on"))
                if not (AGENT_BR or args.free or rally_active or args.mission
                        or camp_forced):
                    spawn_pos, woke_near = respawn_spawn_point(dead_state, roster_snapshot)
                if spawn_pos:
                    journal.log(f"Koop-Respawn neben {woke_near.get('name') or 'Spieler'} "
                                f"bei ({spawn_pos[0]:.1f}, {spawn_pos[1]:.1f}) statt am Lager.")
                LAST_RESTORE_LOST.clear()
                LAST_RESTORE_GROUND.clear()
                ensure_body(bridge, journal, restore=not args.no_restore,
                            loadout_default=args.loadout,
                            fresh=args.fresh_loadout, spawn_pos=spawn_pos)
                # Frische Claude-Session: das Gehirn darf NICHT mit dem kompletten
                # Vor-Tod-Kontext (100+ Züge) weiterdenken. Sonst handelt es nach
                # dem Respawn auf alten Positionen/Plänen, jagt verschwundene
                # Zombies oder folgt unerreichbaren Zielen - wirkt eigenartig und
                # reagiert auf nichts. Das alte Gehirn ist oben schon beendet;
                # CLAUDE.md/memory bleibt, nur der Gesprächsverlauf fällt weg.
                proc = spawn_claude(mcp_cfg, args.model, args.character,
                                    args.turn_limit)
                reader = BrainReader(proc, journal, tracker)
                reader.start()
                watcher = EventWatcher(bridge)
                buffered = []  # alte Ereignisse betreffen das alte Leben
                buffered_at = {}
                buffer_deadline = 0.0
                rotate_pending = False
                last_ctx = 0
                turns_since_rotate = 0
                interrupted = False
                idle_backoff = 1              # frisches Leben = volle Aufmerksamkeit
                event_since_tick = True
                # Getrennter Modus: nach dem Respawn wieder allein zum Treffpunkt
                # und zur Gruppe zurückfinden.
                rally_hint = ""
                if rally_active:
                    rally_hint = (f" Danach: allein zum Treffpunkt "
                                  f"x={args.rally_x:.0f} z={args.rally_z:.0f} - "
                                  f"per travel_to (läuft im Hintergrund, "
                                  f"Distanz ist kein Abbruchgrund), Gruppe "
                                  f"per Funk finden.")
                # Weckruf aus der FRISCHEN Lage nach ensure_body: Squad relativ
                # zur neuen Position, Waffenstatus, Verlorenes, Leiche, Plan.
                new_state = bridge.read_state() or {}
                new_npc = new_state.get("npc") or {}
                squad_line = ""
                if not AGENT_BR and new_npc.get("spawned"):
                    squad_line = _squad_lines(bridge.npc_id, new_npc.get("pos_x", 0.0),
                                              new_npc.get("pos_z", 0.0))
                wake = build_respawn_wakeup(
                    new_state, dead_npc, carry_recent,
                    died_in_vehicle=died_in_vehicle, died_uncon=died_uncon,
                    woke_near=woke_near if spawn_pos else None,
                    squad_line=squad_line, lost=list(LAST_RESTORE_LOST),
                    ground=list(LAST_RESTORE_GROUND), rally_hint=rally_hint,
                    br=bool(AGENT_BR), lang=AGENT_LANG)
                wake = fold_wakeup(wake, info=watcher.drain_info(),
                                   events=take_fresh_events(),
                                   smalltalk=watcher.drain_smalltalk())
                gate.reset()
                send_user(proc, wake)
                gate.note_send(reader, brake=False)
                # Den ganzen Respawn-Weckruf protokollieren (DR-4/VI-13).
                journal.log("[WECKRUF] (Respawn) " + wake.replace("\n", " | "))
                journal.log("[TOD] frische Session gestartet - Respawn-Weckruf gesendet")
                time.sleep(2.0)
                continue

            # Bündelung: nicht-kritische Ereignisse erst ~15 s sammeln, dann
            # als EIN Weckruf. Kritisches (Tod/Schaden/Gefahr), Spieler-Events
            # und REISE-Meldungen (Ankunft = Plan soll sofort weitergehen) sofort.
            if buffered and buffer_deadline == 0.0:
                buffer_deadline = time.monotonic() + EVENT_BUNDLE_SEC
            has_critical = any(_is_critical(e) for e in buffered)
            has_player = any(_is_player_priority(e, watcher.roster_names)
                             for e in buffered)
            has_immediate = any(_is_immediate(e) for e in buffered)
            bundle_ready = (has_critical or has_player or has_immediate
                            or time.monotonic() >= buffer_deadline)
            # Dringend = darf MITTEN in einen Zug (und auch während einer
            # vorgemerkten Rotation in die alte Session): Gefahr/Schaden/Tod,
            # REISE und Funk/Chat von Menschen. SPIELER GESICHTET bewusst nicht
            # (flackert bei Sichtkanten, VI-9).
            urgent_now = (has_critical or has_immediate
                          or any(e.startswith(("FUNK von ", "CHAT von "))
                                 and _is_player_priority(e, watcher.roster_names)
                                 for e in buffered))

            # Watchdog (Notnagel): gilt ein Zug als offen (offener Zug oder
            # unbeantwortete Sendung), das Gehirn schweigt aber seit
            # STUCK_QUIET_SEC (= längstes Tool + 60 s), läuft sicher nichts
            # mehr - Zugzustand zurücksetzen, damit Selbstantrieb und
            # Rotation wieder anspringen. Läuft bewusst auch bei
            # rotate_pending.
            silent = gate.watchdog(reader)
            if silent:
                journal.log(f"[WATCHDOG] Zug galt als offen, aber {silent:.0f}s "
                            f"Stille - Zugzustand zurückgesetzt (Selbstantrieb frei).")

            # Mensch in Sichtweite -> volle Aufmerksamkeit: Idle-Backoff sofort
            # aufheben, nicht erst beim nächsten (gestreckten) Routine-Tick.
            # Roster-NPCs zählen nicht - die eigene Squad steht immer daneben.
            roster_l = {str(r).lower() for r in watcher.roster_names}
            human_near = any(str(n).lower() not in roster_l
                             for n in watcher.known_players)
            if idle_backoff != 1 and human_near:
                idle_backoff = 1
                journal.log(f"[IDLE] zurück auf {args.idle}s "
                            f"(Spieler in Sichtweite)")

            # Ein lokaler, nichtblockierender Handlungsschritt zwischen Zügen.
            # Kein zweiter LLM und keine konkurrierende Tool-Schleife: der
            # Controller respektiert Mailbox, Reise, Follow und frische Snapshots.
            # Nur im echten Leerlauf des Gehirns (TurnGate), sonst schickte er
            # Bridge-Befehle parallel zu einem laufenden Zug.
            survival_busy = False
            if (not args.no_autonomy and not args.once and not AGENT_BR
                    and not rotate_pending
                    and gate.idle(reader, IDLE_QUIET_SURVIVAL)
                    and (not buffered or time.monotonic() < backend_retry_at)
                    and time.monotonic() >= survival_deadline):
                survival_deadline = time.monotonic() + args.survival_interval
                try:
                    step = survival_tick(bridge, agent_name=AGENT_NAME,
                                         memory_path=survival_memory,
                                         allow_explore=bool(args.free and not human_near))
                    survival_busy = step.get("status") in ("issued", "started", "running", "pending", "busy")
                    if step.get("action") and step.get("status") in ("issued", "started"):
                        reason = str(step.get("reason") or step["action"])
                        journal.log(f"[SURVIVAL] {step['action']}: {reason}")
                        _set_nameplate_intent(reason)
                    if step.get("needs_llm"):
                        reason = str(step.get("reason") or "Naechsten Ueberlebensschritt planen")
                        now_step = time.monotonic()
                        if now_step >= survival_handoff_at:
                            survival_handoff = reason
                            survival_handoff_at = now_step + max(60, args.idle)
                    elif survival_busy:
                        survival_handoff = ""
                except (OSError, ValueError, TypeError) as exc:
                    journal.log(f"[SURVIVAL] Schritt ausgelassen: {exc}")
                    survival_deadline = time.monotonic() + 30

            # Auch zwischen zwei lokalen Ticks keine Routineentscheidung mitten
            # in einen soeben gestarteten Bridge-Befehl hinein senden.
            current_cmd = (bridge.read_state() or {}).get("command") or {}
            local_action_running = (current_cmd.get("status") == "running" or
                                    os.path.exists(bridge.cmd_file) or survival_busy
                                    or travel_active(bridge))

            # Zustellung: Routine nur im Leerlauf und nicht während einer
            # vorgemerkten Rotation (sie fährt dann im Handover mit, nichts
            # geht verloren); Dringendes auch mitten im Zug, gebremst auf
            # einen Weckruf pro URGENT_INJECT_MIN_SEC.
            if (buffered and bundle_ready and time.monotonic() >= backend_retry_at
                    and gate.may_deliver(reader, urgent_now, rotate_pending)):
                lines = format_buffered(buffered, buffered_at, time.monotonic(), watcher)
                buffered = []
                buffered_at = {}
                buffer_deadline = 0.0
                if lines:
                    smalltalk = watcher.drain_smalltalk()
                    prefix = ""
                    if smalltalk:
                        prefix = ("FUNKGEPLAUDER der anderen (nur zur Info, keine "
                                  "Antwort noetig): " + smalltalk + "\n\n")
                    dispatch(prefix + "EREIGNIS:\n- " + "\n- ".join(lines) +
                             "\n\nReagiere angemessen (observe zuerst, falls noetig).")

            # Routine-Tick nur im echten Leerlauf (und nie mit hängender
            # Rotation). Der Backoff-Faktor streckt AUSSCHLIESSLICH dieses
            # Gate (idle_deadline selbst wird an mehreren Stellen - Zug-Ende,
            # Dispatch - immer mit dem Basis-idle gesetzt; der Faktor gehört
            # nur in die Routine-Tick-Rechnung, nicht in die Event-Pfade).
            elif (not rotate_pending and not local_action_running
                  and gate.idle(reader, IDLE_QUIET_TICK)
                  and time.monotonic() >= backend_retry_at
                  and (survival_handoff or time.monotonic() >= idle_deadline
                      + (idle_backoff - 1) * args.idle)):
                # Kein echtes Ereignis seit dem vorigen Routine-Tick: Abstand
                # verdoppeln (1 -> 2 -> 4, Cap 4). Im BR-Modus fest 1; bei
                # Mensch in Sichtweite ebenfalls nie strecken. Und NIE
                # strecken, wenn der NPC stationär herumsteht (Analyse
                # 24.08.: 480-s-Lücken bei stehenden NPCs) - der Backoff
                # ist für beschäftigte NPCs (Reise/Marsch), nicht für
                # Rumsteher.
                stationary = watcher.stationary_secs() >= STILL_WARN_SEC
                if stationary and idle_backoff != 1:
                    idle_backoff = 1
                    journal.log(f"[IDLE] zurück auf {args.idle}s "
                                f"(NPC steht unbewegt herum)")
                if (not event_since_tick and not AGENT_BR
                        and not human_near and not stationary
                        and idle_backoff < 4):
                    idle_backoff *= 2
                    journal.log(f"[IDLE] Backoff x{idle_backoff} "
                                f"({args.idle * idle_backoff}s) - nichts los")
                event_since_tick = False
                smalltalk = watcher.drain_smalltalk()
                if args.free:
                    # Freier Modus: kein Treffpunkt-Anker - der Agent verfolgt
                    # seine EIGENEN Ziele.
                    tick = ("ROUTINE-TICK: Eine Weile ist vergangen. Prüfe die "
                            "Lage. Keine Gefahr? Dann nutze die Zeit für DEINE "
                            "eigenen Ziele: looten, erkunden, Vorräte anlegen, "
                            "Inventar ordnen, kochen. Denk daran: du hast nur "
                            "EIN Leben - geh kein unnötiges Risiko ein. Und "
                            "wenn du etwas Berichtenswertes hast (Fund, Plan, "
                            "Lage), gib einen kurzen Funkspruch ab (say).")
                else:
                    tick = ("ROUTINE-TICK: Eine Weile ist vergangen. Pruefe die "
                            "Lage. Keine Gefahr und kein Auftrag? Dann nutze die "
                            "Zeit sinnvoll: looten, kleiner Rundgang, Inventar "
                            "ordnen, kochen - bleib dabei in der Naehe des "
                            "Treffpunkts. Und wenn du etwas Berichtenswertes "
                            "hast (Fund, Plan, Lage), gib einen kurzen "
                            "Funkspruch ab (say).")
                if stationary:
                    tick = ("DU STEHST SEIT MINUTEN UNBEWEGT HERUM. " + tick
                            + " Beende den Zug NICHT, ohne dass eine Aktion "
                              "läuft (Bewegung, Looten, Kochen, Wache mit "
                              "Ansage).")
                if smalltalk:
                    tick = ("FUNKGEPLAUDER der anderen seither: " + smalltalk
                            + "\n\n" + tick)
                if survival_handoff:
                    tick = "SURVIVAL-HINWEIS: " + survival_handoff + "\n\n" + tick
                    survival_handoff = ""
                dispatch(tick, routine=True)

            time.sleep(2.0)

    except KeyboardInterrupt:
        journal.log("=== Abbruch durch Benutzer ===")
        return 0
    finally:
        # Stop-Flag (Supervisor/close_game): Gehirn SOFORT hart beenden statt
        # 15 s auf seinen Zug zu warten - close_game gibt den Runnern nur 8 s,
        # Viktor/Birgit wurden am 24.10. vor Snapshot und Despawn gekillt und
        # ihre Gehirne handelten noch nach dem Stop. Sonst wie bisher sanft.
        if os.path.exists(STOP_FLAG):
            _kill_brain(proc)
        else:
            try:
                proc.stdin.close()
                proc.wait(timeout=15)
            except Exception:
                _kill_brain(proc)
        if discord_proc:
            discord_proc.terminate()
        if mic_proc:
            mic_proc.terminate()
        # Körper aufräumen: eine Hülle ohne Gehirn steht sonst stumm in der
        # Welt und sieht aus wie "der NPC antwortet nicht". Inventar wird
        # vorher gesichert und beim nächsten Start wiederhergestellt.
        if not args.keep_body:
            try:
                save_inventory_snapshot(bridge.read_state() or {})
                r = bridge.run("despawn", timeout=10)
                journal.log(f"Koerper despawnt ({r.get('status')}) - Inventar "
                            f"gesichert, naechster Start stellt es wieder her.")
            except Exception as e:
                journal.log(f"Despawn beim Beenden fehlgeschlagen: {e}")


if __name__ == "__main__":
    sys.exit(main())
