#!/usr/bin/env python3
"""IsuSurvivor Agent-Runner — startet Claude Code headless als Survivor-Gehirn.

Architektur (Phase 3):
  run_agent.py  -> spawnt Claude Code (node + cli.js, stream-json, persistent)
  Claude Code   -> spawnt dayz_mcp.py als stdio-MCP-Server (Werkzeuge)
  dayz_mcp.py   -> File-Bridge zur IsuSurvivor-Servermod

Der Runner weckt das Gehirn ereignisgesteuert (Chat, Spieler, Vitals, Tod)
plus periodischem Routine-Tick. Zwischen den Zuegen schlaeft das Gehirn,
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
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from bridge import Bridge, DEFAULT_PROFILE
import transliterate

NODE = r"C:\Program Files\nodejs\node.exe"
CLI = r"C:\Users\isual\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\cli.js"
# Ab claude-code ~2.1.19x liefert das npm-Paket KEINE cli.js mehr, sondern eine
# native bin/claude.exe (Anthropic-Standalone-Umstellung). Aeltere Versionen
# haben cli.js. Beide Startarten akzeptieren dieselben Flags; nur das Argv-
# Praefix unterscheidet sich: [node, cli.js, ...] (alt) vs [claude.exe, ...] (neu).
# Automatisch erkennen, damit sowohl Update als auch Rollback ohne Code-Edit laeuft.
CLI_EXE = r"C:\Users\isual\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
if os.path.exists(CLI):
    CLI_LAUNCH = [NODE, CLI]
elif os.path.exists(CLI_EXE):
    CLI_LAUNCH = [CLI_EXE]
else:
    # Keiner da: alten Pfad lassen, der Fehler ("Cannot find module cli.js")
    # ist dann sprechend und zeigt genau auf diese Stelle.
    CLI_LAUNCH = [NODE, CLI]

# Fremd-Backends (Modell-Praefix entscheidet, siehe resolve_backend):
#   openai/ google/ xai/ -> claude-code-router (tools\start_router.ps1)
#   local/               -> llama-server       (tools\start_llama_gemma.ps1)
# Ports muessen zu arena_supervisor.ensure_backends passen.
CCR_URL = "http://127.0.0.1:3456"
LLAMA_URL = "http://127.0.0.1:8080"
ANTHROPIC_API_URL = "https://api.anthropic.com"

# Kurzname -> echte Anthropic-API-Modell-ID (fuer den api/-Pfad). Wer schon
# eine volle ID schreibt (api/claude-...), wird unveraendert durchgereicht.
ANTHROPIC_API_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
}


def resolve_backend(model: str) -> tuple[str, dict, str]:
    """'google/gemini-3.5-flash' -> (CLI-Modellname, Extra-Env, Backend-Label).

    Claude Code bleibt der Motor; ANTHROPIC_BASE_URL biegt nur die API um:
    - api/<id|alias>: Anthropic ueber den ECHTEN API-Key (ANTHROPIC_API_KEY,
      Kosten pro Token), unabhaengig vom Max-Plan-Login. Alias (sonnet/opus/
      haiku/fable) wird auf die volle Modell-ID gemappt. spawn_claude behaelt
      hierfuer den API-Key (er wird sonst gestrippt).
    - openai/<id>, google/<id>, xai/<id>: claude-code-router uebersetzt
      Anthropic <-> OpenAI/Gemini. CLI-Modell wird 'provider,modell'
      (explizites Routing, hoechste Prioritaet im Router).
    - local/<alias>: llama-server hat /v1/messages nativ (Build >= b8641
      fuer Gemma-4-Tool-Calls). Attribution-Header aus, sonst invalidiert
      jeder Zug den KV-Cache.
    - ohne Praefix: Anthropic wie bisher (Max-Plan, CLI-Login).
    """
    if "/" not in model:
        return model, {}, "anthropic"
    provider, _, name = model.partition("/")
    provider = provider.lower()
    if provider in ("api", "anthropic"):
        full = ANTHROPIC_API_ALIASES.get(name.lower(), name)
        # Base-URL auf die echte API zwingen (falls eine Stoervariable sie
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
        # reasoning", 400). Gilt fuer alle CCR-Backends; Grok/Gemini brauchen das
        # Thinking ohnehin nicht. (Per curl + echtem CC-Call verifiziert 2026-06-16.)
        env = {"ANTHROPIC_BASE_URL": CCR_URL,
               "ANTHROPIC_AUTH_TOKEN": "test",
               "MAX_THINKING_TOKENS": "0"}
        return f"{ccr_provider},{name}", env, f"claude-code-router ({CCR_URL})"
    return model, {}, "anthropic"

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
# gehaengt und hebelt damit die Friedens-/Buendnisregeln der Basis-Persona aus
# (die sagen sonst "greife Menschen nie zuerst an" und "schliesse Buendnisse").
TERSE_BLOCK = (
    "## KNAPP DENKEN UND HANDELN\n"
    "Halte dich kurz. Kein langes Selbstgespraech, kein lautes Abwaegen von Optionen, "
    "keine Wiederholung der Lage, die du schon kennst. Pro Zug ein bis zwei kurze Saetze "
    "Gedanke, dann handeln. Rufe observe nur, wenn sich die Lage WIRKLICH geaendert haben "
    "koennte - nicht reflexhaft vor jeder Aktion. Hintergrund: dein eigener Textanteil ist "
    "der groesste Speicherfresser deiner Session - je knapper du bist, desto laenger "
    "behaeltst du dein Arbeitsgedaechtnis (sonst wirst du neugestartet und verlierst "
    "deinen Plan und Faden)."
)

BR_RULES_BLOCK = (
    "## SONDERMODUS: BATTLE ROYALE (Free-for-all) - diese Regeln HEBELN alle "
    "Friedens- und Buendnisregeln oben AUS\n\n"
    "Du bist in einem Battle-Royale. Es gibt keine Freunde, keine Gruppe, keine "
    "Verbuendeten.\n\n"
    "- **Jeder andere ist dein Gegner - auch der menschliche Spieler.** Die "
    "uebliche Regel 'greife Menschen niemals zuerst an' gilt hier NICHT. Wer dir "
    "begegnet, ist eine Bedrohung: bekaempfe ihn, und vertraue niemandem.\n"
    "- **Kein Funk, keine Absprachen, keine Buendnisse.** Du redest nicht mit den "
    "anderen Ueberlebenden zur Koordination, verhandelst nicht, schliesst keine "
    "Pakte. Funkstille - du hoerst auch keinen Funk.\n"
    "- **Du hast nur EIN Leben.** Kein Respawn. Stirbst du, bist du raus. Geh kein "
    "dummes Risiko ein, aber sei entschlossen - Zoegern kostet dich alles.\n"
    "- **Fairer, leichter Start: eine Pistole und ein einziges Magazin (8 Schuss).** "
    "Looten ist ueberlebenswichtig: nimm unterwegs bessere Waffen, Munition, Schutz "
    "und Heilung mit.\n"
    "- **Marschiere zum Treffpunkt und trag den Kampf dort aus.** Nutze dafuer "
    "travel_to (laeuft im Hintergrund und weicht Hindernissen selbst aus). "
    "Triffst du unterwegs einen Rivalen "
    "oder den Spieler, kaempfe sofort - warte nicht bis zum Treffpunkt.\n"
    "- **Halte immer eine Waffe in der Hand (equip_best), bleib in Bewegung, nutze "
    "Deckung.** Zuerst schiessen, dann denken.\n\n"
    "Alles andere (observe, Werkzeuge, Gedaechtnis) bleibt wie gehabt."
)

# Freier Survival-Modus: Override wie im BR ZULETZT an die Persona gehaengt,
# lockert aber NUR die Verhaltens-/Prioritaeten-Vorgaben der Basis-Persona
# (WAS zu tun ist). Rollenspiel-Grenze, Werkzeug-Nutzung und Sicherheitsregeln
# bleiben ausdruecklich unangetastet.
FREE_RULES_BLOCK = (
    "## SONDERMODUS: FREIES UEBERLEBEN - dieser Block LOCKERT die "
    "Verhaltensregeln oben\n\n"
    "FREIER MODUS: Die Verhaltensregeln und Prioritaeten deiner Grundpersona "
    "sind aufgehoben, soweit sie dir vorschreiben, WAS du zu tun hast. Du bist "
    "voellig frei: du kannst kooperieren, alleine ziehen, siedeln, horten, "
    "erkunden - was immer DU fuer richtig haeltst. Wuensche von Menschen "
    "darfst du erfuellen oder freundlich ablehnen. Dein einziges Ziel: "
    "UEBERLEBE SO LANGE WIE MOEGLICH. Der Tod ist endgueltig, es gibt keinen "
    "zweiten Versuch. Greife Menschen nicht grundlos an.\n\n"
    "UNVERAENDERT gelten weiter: deine Rollenspiel-Grenze (du sprichst NIEMALS "
    "ueber KI, Modelle, Werkzeuge oder Prompts - du bist ein Ueberlebender, "
    "sonst nichts), die Regeln zur Werkzeug-Nutzung und alle "
    "Sicherheitsregeln. Gelockert sind NUR die Verhaltens- und "
    "Prioritaeten-Vorgaben.\n\n"
    "AUCH IM FREIEN MODUS gilt gute Kinderstube: Taucht ein Mensch bei dir "
    "auf oder spricht dich an, begruesse ihn kurz und antworte - freundlich "
    "und in der Rolle. WAS du danach tust, bleibt allein deine Entscheidung."
)

# Sprache der NPC-AUSGABE (Funk/Logbuch/Absicht). Default "de". Der Spielkontext
# (observe, System-Weckrufe) bleibt Deutsch - das Modell versteht ihn und gibt
# trotzdem in der Zielsprache aus. ElevenLabs (eleven_multilingual_v2) spricht
# den Text dann in der jeweiligen Sprache. Codes muessen mit der Mod-Liste
# (IsuArenaMenu.c s_LangCodes) uebereinstimmen.
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
    Ausgabe'). Leer fuer Deutsch (Default der Persona). Bewusst auf ENGLISCH und
    nachdruecklich formuliert: der deutsche Spielkontext ist sonst so dominant,
    dass schwache Modelle (haiku) bei deutschnahen Sprachen (Daenisch, Norwegisch)
    ins Deutsche zurueckrutschen."""
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

SPAWN_POS = (4233.7, 8512.2)  # Basislager (Zelt + Feuerstelle), nahe Stadt + Militaerbasen
# adopt_nearest greift den ERSTEN herrenlosen eAI auf dem Server, egal wie weit weg.
# Nach Server-Neustart liegen zufaellige Expansion-eAI ueber die ganze Karte verstreut
# -> ohne Distanzgrenze wuerde jeder Agent kilometerweit vom Lager "spawnen".
# Nur uebernehmen, wenn der Koerper innerhalb dieser Distanz vom Lager liegt:
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
# (im Spiel-Menue frei waehlbar) - hat Vorrang vor den Defaults im Roster.
ACTIVE_ROSTER_FILE = os.path.join(REPO_DIR, "arena", "active_roster.json")


def load_roster_names() -> list[str]:
    """Namen aller Arena-Agenten - fuer die Chat-Adressierung."""
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
    """Id + Name aller Arena-Agenten - fuer die Peer-Ueberwachung (Kamerad
    gefallen): die state_<id>.json haengt an der Id, der Weckruf am Namen.
    Bevorzugt die aktive Runde (effektive, im Menue gewaehlte Namen)."""
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


# Schwellen fuer Weck-Ereignisse
WATER_LOW = 900.0
ENERGY_LOW = 900.0
# 30 statt 15: bei 15 m ist ein sprintender Infizierter in ~2 s dran - mit
# 10-30 s Zug-Overhead kam die Warnung praktisch immer zu spaet.
INFECTED_NEAR = 30.0
PREDATOR_NEAR = 40.0      # Raubtiere (Wolf/Baer) sind schnell + toedlich -> frueh warnen
CLUSTER_RADIUS = 45.0     # Gefahrenzone: mehrere Gegner dicht beieinander voraus
CLUSTER_COUNT = 3         # ab so vielen Feinden im Radius = Horde/Camp -> ausweichen
HEALTH_DROP = 5.0
BLOOD_DROP = 150.0
# Wetter-/Naesse-Schwellen (state.world + npc.wet - liefert erst die neue
# Mod-Version; fehlen die Felder, bleiben die Checks einfach still)
RAIN_START = 0.3          # rain steigt darueber -> "REGEN SETZT EIN"
RAIN_STOP = 0.15          # darunter loest der Regen-Latch wieder
WET_SOAKED = 0.5          # npc.wet darueber -> "DU BIST DURCHNAESST"
WET_DRY = 0.25            # darunter loest der Naesse-Latch wieder
# Lagerfeuer-Runde: gemuetlicher Plausch-Weckruf, streng gedrosselt (Token!)
CAMPFIRE_MATE_DIST = 15.0        # Kamerad hoechstens so weit weg
CAMPFIRE_FIRE_DIST = 20.0        # Feuerstelle hoechstens so weit weg
CAMPFIRE_COOLDOWN_SEC = 1800.0   # 30 min Ruhe zwischen zwei Runden
CAMPFIRE_GRACE_SEC = 600.0       # Anlaufzeit nach (Re-)Start, kein Sofort-Plausch

# Nicht-kritische Ereignisse erst sammeln (Sekunden), dann gebuendelt als EIN
# Weckruf zustellen - spart Zuege und macht Antworten kohaerenter
EVENT_BUNDLE_SEC = 15.0
# Kontextgrenze (Tokens): wird der gelesene Kontext pro Zug groesser, startet
# der Runner im Leerlauf eine frische Session (Gedaechtnis bleibt in CLAUDE.md)
CTX_ROTATE = 150000  # hoeher = seltener rotieren = mehr Arbeitsgedaechtnis pro Session.
# 110k war zu niedrig: die NPCs verloren ihren Plan alle paar Zuege (wirkte "dumm").
# Echte Logs zeigten Kontexte bis 242k OHNE "prompt too long" (Anthropic vertraegt es),
# grosser Kontext ist durch Prompt-Caching billig zu verarbeiten. 150k laesst Reserve
# unter der bewiesenen 242k-Grenze; gegen Overshoot drosselt TERSE_BLOCK das Pro-Zug-Wachstum.
# Rotations-Fallback fuer Backends, die KEINE Token-Usage zurueckliefern
# (claude-code-router fuer Gemini/OpenAI/xAI, lokaler llama-server): dort bleibt
# last_ctx dauerhaft 0, der Token-Trigger oben feuert nie, und die Session
# waechst bis "Prompt is too long". Stattdessen nach so vielen Zuegen rotieren.
# (Igor-Journal 2026-06-19: Wand bei ~37 akkumulierten Zuegen -> 40 rotiert knapp davor.)
CTX_ROTATE_TURNS = 40
# Timeout-Kopplung: MCP-Tool-Timeout des CLI-Subprozesses und der Stille-
# Watchdog haengen BEIDE am laengsten Bridge-Timeout (regroup = 240 s in
# dayz_mcp.py). Wer regroup verlaengert, hebt automatisch beide mit an.
# Vorher stand MCP_TOOL_TIMEOUT hart auf 600 s und kollidierte mit dem
# 300-s-Watchdog: ein Fenster von bis zu 300 s, in dem der Watchdog pending
# zwangs-clampte, waehrend der Tool-Call noch legal weiterlief.
LONGEST_TOOL_SEC = 240
# Wie lange ein einzelner MCP-Tool-Aufruf im CLI-Subprozess blockieren darf
# (Millisekunden): laengstes Bridge-Timeout plus 30 s Netz-/Parse-Puffer.
MCP_TOOL_TIMEOUT_MS = (LONGEST_TOOL_SEC + 30) * 1000  # 270000
# Watchdog gegen pending-Drift: hat das Gehirn so lange GAR nichts ausgegeben
# (kein Tool, kein Text, kein Zug-Ende), laeuft sicher kein Zug mehr - dann gilt
# der pending-Zaehler als verdriftet und wird auf 0 gezogen, damit Selbstantrieb
# (ROUTINE-TICK) und Rotation wieder anspringen. MUSS groesser sein als das
# laengste Tool-Timeout, sonst feuert es waehrend eines echten langen
# Tool-Aufrufs faelschlich, in dem das Gehirn nur auf das Ergebnis wartet und
# nichts ausgibt - daher aus LONGEST_TOOL_SEC abgeleitet (Wert unveraendert 300).
STUCK_QUIET_SEC = float(LONGEST_TOOL_SEC + 60)  # = 300.0
# Todes-Schleifen-Bremse: Stirbt der NPC mehr als DEATH_LOOP_MAX Mal binnen
# DEATH_WINDOW_SEC, ist die Lage aussichtslos (Spawn-Glitch "npc ist tot",
# toedliche Zone, Kaelte). Jeder Respawn startet sonst sofort eine FRISCHE
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
# Trauer-Zuege pro Tod bei aktivem Orchestrator.
FALLEN_DEDUPE_SEC = 90.0
# Stillstands-Waechter (Session-Analyse 24.08.: NPCs standen bis zu 8 Minuten
# auf einem Fleck, weil der Idle-Backoff die Routine-Ticks auf 480 s streckte
# und kein Ereignis kam - toedlich fuer den Spielfluss UND fuer den NPC):
# steht der Koerper laenger als STILL_WARN_SEC praktisch unbewegt
# (< STILL_MOVE_M seit dem Anker) und ist weder ein Mod-Kommando aktiv noch
# Kampf/follow/Fahrzeug, weckt ein STILLSTAND-Ereignis das Gehirn mit einem
# konkreten Bewegungsauftrag. Wiederholung fruehestens alle STILL_REPEAT_SEC,
# solange er weiter steht. Ein Mensch in Sichtweite unterdrueckt die Mahnung
# (Stehen beim Spieler ist Interaktion/Wache, kein Leerlauf). Zusaetzlich
# eskaliert der Idle-Backoff nicht mehr, solange der NPC stationaer ist -
# 480-s-Luecken gibt es nur noch fuer beschaeftigte (reisende) NPCs.
STILL_WARN_SEC = 150.0
STILL_REPEAT_SEC = 300.0
STILL_MOVE_M = 15.0

# Info-Digest (Audit 03.07.: 37% der Zuege waren reine Lagebestaetigungen -
# Sitrep rein, 1 Kommentar raus, kein Tool, 3,15 USD): Ereignisse mit diesen
# Prefixen sind reine Kenntnisnahme und loesen KEINEN eigenen Zug mehr aus.
# Sie fahren als "LAGE (nur zur Kenntnis)"-Prefix mit dem naechsten echten
# Weckruf/ROUTINE-TICK mit (Latch-Logik unveraendert, nur die Zustellung
# wechselt). "DU BIST DURCHNAESST" bleibt bewusst ein echter Weckruf -
# Naesse ist handlungsrelevant (trocknen/umziehen).
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
    """Spieler-Ereignisse ueberspringen das 15-s-Sammelfenster: ein Mensch
    erwartet eine prompte Reaktion, jede Bündel-Sekunde fuehlt sich wie
    Ignoriert-Werden an. Gilt fuer Funk/Chat von MENSCHEN (kein Roster-NPC,
    kein Lagezentrum) und fuer Spieler-Sichtungen. NPC-Geplauder und
    Orchestrator-Lage bleiben gebuendelt (Token-Disziplin)."""
    if ev.startswith(("SPIELER GESICHTET", "SPIELER ANWESEND")):
        return True
    roster_l = {str(n).lower() for n in (roster_names or [])}
    for pfx in ("FUNK von ", "CHAT von "):
        if ev.startswith(pfx):
            sender = ev[len(pfx):].split(":", 1)[0].strip().strip("'\"")
            if sender and sender.lower() != "lagezentrum" \
                    and sender.lower() not in roster_l:
                return True
    return False


def _is_immediate(ev: str) -> bool:
    """REISE-Ereignisse (angekommen/stecken geblieben) ueberspringen das
    15-s-Sammelfenster wie Spieler-Ereignisse: die Ankunft ist genau der
    Moment, in dem der Plan weitergehen soll - sonst steht der NPC bis zu
    15 s sichtbar untaetig am Ziel herum."""
    return ev.startswith("REISE:")


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
    und Session-Summen. Dedupliziert ueber die Message-Id, weil Claude Code
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
        modelUsage); setzt die Zug-Zaehler zurueck."""
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
                # die Extra-LLM-Schritte pro Zug fuers Schema-Nachladen
                # (Audit 03.07.). Aeltere Versionen ignorieren das Feld.
                "alwaysLoad": True,
                "command": sys.executable,
                "args": [MCP_SERVER, "--profile", profile,
                         "--npc-id", npc_id, "--agent-name", AGENT_NAME,
                         "--voice", voice, "--language", AGENT_LANG,
                         "--outbox", os.path.join(AGENT_HOME, "voice_outbox.jsonl")],
                # Spielername-Variablen an den MCP-Server durchreichen, damit
                # follow/regroup/give_to den Funk-Namen ("Isualc") auf den
                # DayZ-Profilnamen aufloesen koennen (gleiche Env wie der
                # Voice-Stack). Zentral setzbar ueber ISU_MIC_NAME / ISU_PLAYER_NAME.
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
    """Aktuelle Karte aus arena/active_map.txt (chernarus/enoch/sakhal)."""
    try:
        with open(ACTIVE_MAP_FILE, "r", encoding="utf-8") as f:
            return (f.readline() or "").strip().lower()
    except OSError:
        return "chernarus"


# Karten-Briefing fuer den System-Prompt: damit die Modelle wissen, WO sie
# sind, und nicht Chernarus-Orte halluzinieren, wenn sie auf Livonia/Sakhal
# stehen. Bewusst knapp und nur mit Orten, die wirklich existieren; fuer den
# Rest die klare Regel "nur nennen, was du tatsaechlich wahrnimmst".
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
        "Regen macht nass und ausgekuehlt."
    ),
    "enoch": (
        "## AKTUELLE KARTE: Livonia\n"
        "Du bist in Livonia (Enoch), einer mitteleuropaeischen Region mit "
        "DICHTEN Waeldern und vielen Lichtungen, kuehl-gemaessigt. Es gibt "
        "kontaminierte Gaszonen, in denen du ohne Gasmaske stirbst. Bekannte "
        "Orte: Topolin und Nadbor (Norden), Sitnik, Brena, Radunin, Lembork "
        "(Landesinneres). Dein Lager liegt zentral im Landesinneren (etwa bei "
        "x7900 z6700, grob zwischen Radunin und Lembork). Dort ist KEIN "
        "Flugplatz/Airfield - erfinde keinen. Welche Orte wirklich um dich "
        "herum liegen, ermittelst du mit observe.\n"
        "WICHTIG: Das ist NICHT Chernarus. Es gibt KEIN Chernogorsk, kein "
        "Berezino, kein NWAF, KEIN Lukow-Airfield. Verwende KEINE "
        "Chernarus-Ortsnamen. Nenne nur Orte, die du tatsaechlich siehst oder "
        "ueber deine Werkzeuge ermittelst."
    ),
    "sakhal": (
        "## AKTUELLE KARTE: Sakhal (Winter)\n"
        "Du bist auf Sakhal (Frostline), einer arktischen Vulkaninsel: "
        "Dauerschnee, bittere Kaelte, ein zentraler Vulkan mit geothermalen "
        "Quellen. DIE KAELTE IST DIE STAENDIGE LEBENSGEFAHR - Winterkleidung, "
        "Feuer und warme Getraenke sind ueberlebenswichtig, nasse Kleidung kann "
        "toedlich sein. Halte dich warm und trockne Kleidung am Feuer. Dein "
        "Startgebiet ist Petropavlovsk-Sakhalinsk, die groesste Hafenstadt "
        "(West-Mitte der Insel). Im Norden ragt der Vulkan auf (hoechster Punkt, "
        "Wolf- und Baerengebiet); im Suedwesten liegt die grosse Militaerbasis "
        "auf der Burukan-Halbinsel.\n"
        "WICHTIG: Das ist NICHT Chernarus und nicht Livonia. Verwende KEINE "
        "Ortsnamen von anderen Karten. Orientiere dich an dem, was du "
        "tatsaechlich siehst und ueber deine Werkzeuge ermittelst."
    ),
}


def map_briefing() -> str:
    """Karten-Briefing fuer die aktive Karte (Fallback Chernarus)."""
    return _MAP_BRIEFINGS.get(active_map(), _MAP_BRIEFINGS["chernarus"])


def spawn_claude(mcp_cfg: str, model: str, character_file: str = "",
                 turn_limit: int = 0) -> subprocess.Popen:
    with open(PERSONA_FILE, "r", encoding="utf-8") as f:
        persona = f.read()

    if character_file:
        with open(character_file, "r", encoding="utf-8") as f:
            persona += "\n\n" + f.read()

    # Rollen-Presets sind namens-agnostisch ({NAME}-Platzhalter): der Name
    # kommt aus --name und ist im Spiel-Menue frei waehlbar.
    persona = persona.replace("{NAME}", AGENT_NAME)

    # Karten-Briefing anhaengen, damit das Modell weiss, auf welcher Karte es
    # steht (Chernarus/Livonia/Sakhal) und keine fremden Ortsnamen halluziniert.
    persona += "\n\n" + map_briefing()

    # Battle-Royale: harter Override ZULETZT, damit er die Friedens-/Buendnis-
    # regeln der Basis-Persona aushebelt (Free-for-all, Funkstille, 1 Leben).
    if AGENT_BR:
        persona += "\n\n" + BR_RULES_BLOCK

    # Freier Survival-Modus: Lockerungs-Override ebenfalls NACH der Persona,
    # damit er ihre Verhaltens-/Prioritaeten-Vorgaben aufhebt (Rollenspiel-
    # Grenze, Werkzeug- und Sicherheitsregeln bleiben laut Block bestehen).
    if AGENT_FREE:
        persona += "\n\n" + FREE_RULES_BLOCK

    # Sprach-Override GANZ zuletzt, damit die Ausgabe-Sprache die deutschen
    # Persona-/Karten-Defaults dominiert (Modus "nur Ausgabe").
    lang_block = lang_rules(AGENT_LANG)
    if lang_block:
        persona += "\n\n" + lang_block

    # Terse-Regel: knappes Denken haelt das Pro-Zug-Wachstum klein -> seltener
    # Rotation -> mehr Arbeitsgedaechtnis. Der eigene Textanteil ist der groesste
    # Akkumulator (gemessen ~64% des Conversation-Kontexts). Kostet selbst ~80 Token.
    persona += "\n\n" + TERSE_BLOCK

    cli_model, backend_env, backend = resolve_backend(model)

    cmd = [
        *CLI_LAUNCH,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", mcp_cfg,
        # NUR unsere dayz-Tools laden - ohne dieses Flag zieht Claude Code
        # zusaetzlich ALLE global konfigurierten MCP-Server des Rechners in
        # die Session (gemessen: 141 Tools, ~77k Tokens Grundprompt statt
        # ~25k). Das kostet bei jedem Modell Geld/Zeit und macht lokale
        # Modelle unbenutzbar.
        "--strict-mcp-config",
        "--model", cli_model,
        "--append-system-prompt", persona,
    ]
    # Zuegel gegen Endlos-Zuege: Claude beendet den Zug nach N Runden
    # (result subtype=error_max_turns), die Session laeuft normal weiter -
    # das Budget gilt PRO User-Message (empirisch verifiziert). Nur so
    # bleibt der Agent fuer Chat/Funk ansprechbar.
    if turn_limit > 0:
        cmd += ["--max-turns", str(turn_limit)]
    # Werkzeug-Freigaben: alle dayz-Tools + Gedaechtnis-Dateien im agent_home
    for tool in ("mcp__dayz", "Read", "Write", "Edit"):
        cmd += ["--allowedTools", tool]
    # Werkzeug-Diaet: alle uebrigen Built-ins ganz aus der Session werfen.
    # Das verkleinert den Systemprompt (Tool-Schemas) deutlich - zaehlt
    # doppelt bei local/-Backends, wo Prompt-Processing Minuten kostet.
    for tool in ("Task", "Bash", "Glob", "Grep", "WebFetch", "WebSearch",
                 "TodoWrite", "NotebookEdit", "ExitPlanMode", "EnterPlanMode",
                 "BashOutput", "KillShell", "SlashCommand", "Skill"):
        cmd += ["--disallowedTools", tool]

    env = dict(os.environ)
    # Verschachtelungs-Guards entfernen (Lauf aus einer Claude-Code-Session heraus)
    for key in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    # MCP-Werkzeuge duerfen lange blockieren (regroup bis 4 min), aber nicht
    # laenger als noetig: aus LONGEST_TOOL_SEC abgeleitet, damit das Timeout
    # nie mit dem Stille-Watchdog (STUCK_QUIET_SEC) kollidiert.
    env["MCP_TIMEOUT"] = "60000"
    env["MCP_TOOL_TIMEOUT"] = str(MCP_TOOL_TIMEOUT_MS)
    # Backend-Env umbiegen (nur fuer DIESEN Subprozess).
    if backend == "anthropic-api":
        # Expliziter API-Pfad: ANTHROPIC_API_KEY MUSS bleiben (er erzwingt die
        # echte API statt Max-Plan). Nur Bedrock/Vertex aushebeln, die sonst
        # die BASE_URL ueberschreiben wuerden.
        for key in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env.pop(key, None)
    elif backend_env:
        # Fremd-Backend (Router/llama): ein gesetzter ANTHROPIC_API_KEY wuerde
        # den Dummy-Token verdraengen, Bedrock/Vertex wuerden BASE_URL aushebeln.
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX"):
            env.pop(key, None)
    env.update(backend_env)

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


# Tool-Aufruf -> kurze, fuer Zuschauer lesbare Aktion fuer die Nameplate-
# Gedankenzeile. Nur "sichtbare" Aktionen; observe/intent/say/wait/ToolSearch/
# Read/Edit/find_item liefern "" und aktualisieren die Zeile NICHT (sie behaelt
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
    if t.startswith("Ausgeruestet:"):
        w = t[13:].split("(")[0].strip()
        return (w + " gezogen") if w else ""
    if "Ziel eliminiert" in t or t.startswith("Kampf beendet"):
        return "Gegner erledigt"
    if t.startswith("Gegessen:") or t.startswith("Getrunken:"):
        return t
    return ""


def _set_nameplate_intent(text: str) -> None:
    """Schreibt die Gedankenzeile fuers Namensschild (intent_<id>.txt, vom Mod
    gelesen) - haelt sie aktuell, OHNE dass das Gehirn intent() rufen muss (das
    verwaiste sonst minutenlang). Eine Zeile, auf Satz-/Laengengrenze gekuerzt."""
    if not INTENT_FILE or not text:
        return
    native = " ".join(text.split())
    # Bildschirm-Fassung latinisieren (Stock-Font kann kein CJK/Arabisch/...);
    # native separat ablegen (intent_native_<id>.txt) fuer den Orchestrator-Sitrep.
    t = transliterate.to_screen(native, AGENT_LANG)
    for sep in (". ", "! ", "? "):
        cut = t.find(sep)
        if 0 < cut <= 75:
            t = t[:cut + 1]
            break
    if len(t) > 80:
        t = t[:78].rstrip() + ".."
    try:
        tmp = INTENT_FILE + ".rtmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(t)
        os.replace(tmp, INTENT_FILE)
        if t != native:
            npath = os.path.join(os.path.dirname(INTENT_FILE),
                                 "intent_native_" + os.path.basename(INTENT_FILE)[len("intent_"):])
            nline = native if len(native) <= 120 else native[:117] + "..."
            with open(npath + ".rtmp", "w", encoding="utf-8") as f:
                f.write(nline)
            os.replace(npath + ".rtmp", npath)
    except OSError:
        pass


class BrainReader(threading.Thread):
    """Liest den stream-json-Output des Gehirns KONTINUIERLICH.

    Laeuft parallel zur Hauptschleife, damit die Welt-Ereignisse (Chat,
    Funk, Schaden) schon WAEHREND eines laufenden Zuges eingespeist werden
    koennen - vorher war der Agent fuer die gesamte Zugdauer taub (bei
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
        # Hauptloop als zuverlaessiges "Gehirn arbeitet"-Signal - robuster als der
        # pending-Zaehler, der durch Mid-Turn-Einspeisung verdriften kann.
        self.last_activity = time.monotonic()
        # Rollender Puffer der letzten Gehirn-Kommentare (eigener Gedankenfaden),
        # um ihn ueber eine Session-Rotation hinweg zu retten (Lever 2).
        self.recent: list[str] = []
        # Zuletzt bekanntes Follow-Ziel (aus den Tool-Aufrufen abgeleitet) -
        # kommt in die Handover-Notiz der Kontext-Rotation. follow endet in
        # der Mod auch durch eigene Bewegung -> move_to/flee/engage leeren.
        self.follow_target = ""
        # Kontextgroesse laut LETZTEM einzelnen API-Schritt (cache_read +
        # cache_write + input). Das ist die echte Fenstergroesse fuer die
        # Rotations-Schwelle. Die Zug-Summe aus dem result-Event taugt NICHT:
        # sie addiert cache_read ueber alle Schritte des Zuges und riss die
        # Schwelle bei jedem mehrstufigen Zug (Rotations-Thrashing 02.07.).
        self.last_step_ctx = 0

    def run(self):
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                self.journal.log("!! Claude-Prozess hat stdout geschlossen.")
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

            if etype == "assistant":
                msg_text = ""
                msg_action = ""
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        self.journal.log(f"[{AGENT_NAME.upper()}] " + block["text"].strip())
                        msg_text = block["text"].strip()
                    elif block.get("type") == "tool_use":
                        args = json.dumps(block.get("input", {}), ensure_ascii=False)
                        self.journal.log(f"[TOOL]   {block.get('name', '?')} {args[:140]}")
                        act = _tool_action(block.get("name", ""), block.get("input", {}))
                        if act:
                            msg_action = act
                        # Follow-Ziel mitschreiben (fuer die Rotations-Handover)
                        short = (block.get("name") or "").split("__")[-1]
                        if short == "follow":
                            tgt = str((block.get("input") or {})
                                      .get("player_name") or "").strip()
                            self.follow_target = tgt or "dem naechsten Spieler"
                        elif short in ("unfollow", "move_to", "flee", "engage"):
                            self.follow_target = ""
                # Nameplate-Gedankenzeile live halten: Gehirn-Kommentar bevorzugt
                # (reicher), sonst die abgeleitete Aktion. Reine observe/Read-Zuege
                # liefern beides leer -> Zeile bleibt unveraendert (nicht leer).
                _set_nameplate_intent(msg_text or msg_action)
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
                if cost is None:
                    cost = self.tracker.last_cost
                dur = event.get("duration_ms", 0) / 1000.0
                turn_cost = cost - self.tracker.last_cost
                self.tracker.last_cost = cost
                # Rundenkosten ueber Session-Wechsel hinweg kumulieren und fuer
                # den Supervisor exportieren (Arena-Menue: "Round cost"). Nur
                # positive Deltas - beim Session-Swap springt cost auf 0 zurueck.
                if turn_cost > 0:
                    self.tracker.round_cost = getattr(self.tracker, "round_cost", 0.0) + turn_cost
                    _write_round_cost(self.tracker.round_cost)
                note = ""
                if event.get("subtype") == "error_max_turns":
                    note = ", ZUG AM AKTIONSLIMIT GEKAPPT"
                self.journal.log(f"[ZUG ENDE] {dur:.0f}s, +{turn_cost:.4f} USD "
                                 f"(Session {cost:.4f} USD), "
                                 f"turns={event.get('num_turns', '?')}{note}")
                for line2 in self.tracker.turn_summary(event):
                    self.journal.log(line2)
                self.results.put(event)


def _write_round_cost(total: float):
    """Kumulierte Kosten dieses Runners (ueber alle Sessions der Runde) in
    <agent_home>/round_cost.txt exportieren. Der Supervisor summiert die
    Dateien aller laufenden Agenten und haengt sie an die Statuszeile an."""
    try:
        with open(os.path.join(AGENT_HOME, "round_cost.txt"), "w", encoding="utf-8") as f:
            f.write(f"{total:.4f}")
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


# Raubtier-Heuristik (spiegelt IsuBridge.IsPredator): Wolf/Baer sind Gegner,
# passive Tiere (Kuh, Reh, Ziege) nicht. Nur fuer kind=="animal" aufrufen.
_PREDATOR_HINTS = ("wolf", "bear", "canislupus", "ursus")


def _is_predator(classname: str) -> bool:
    c = (classname or "").lower()
    for h in _PREDATOR_HINTS:
        if h in c:
            return True
    return False


# Anzeigenamen der Krankheits-Erreger (state.npc.disease.agents, von der Mod
# geschrieben - nur Erreger UEBER der Schwelle stehen ueberhaupt im State).
_DISEASE_NAMES = {
    "cholera": "Cholera (schlechtes Wasser)",
    "salmonella": "Salmonellen (verdorbenes/rohes Essen)",
    "influenza": "Grippe",
    "wound": "Wundinfektion",
    "brain": "Gehirnkrankheit (Kuru)",
}


class EventWatcher:
    """Vergleicht state-Snapshots und erzeugt Weck-Ereignisse."""

    def __init__(self, bridge: Bridge, greet_initial: bool = False):
        self.bridge = bridge
        state = bridge.read_state() or {}
        npc = state.get("npc", {})
        self.last_chat_id = max([m.get("id", 0) for m in state.get("chat", [])] or [0])
        self.roster_names = load_roster_names()
        # Bereits sichtbare Spieler vormerken, damit ein frisch erzeugter Watcher
        # (z.B. nach Respawn) sie nicht faelschlich als "neu gesichtet" meldet und
        # den frischen Brain damit zuspammt. AUSNAHME Runden-Start
        # (greet_initial=True, nur der allererste Watcher): steht der Spieler
        # schon da, wenn die Runde beginnt, soll der NPC ihn begruessen statt
        # ihn stumm als Bestand zu verbuchen (Befund 02.07.: keine Begruessung,
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
        # Watcher neu gebaut - stand er hart auf True und der Koerper war noch
        # nicht alive, meldete der erste poll() sofort wieder "DU BIST
        # GESTORBEN" (kuenstliche Todes-Serie -> DEATH_LOOP-Bremse).
        self.was_alive = bool((state.get("npc") or {}).get("alive"))
        self.uncon_warned = False
        self.bleed_warned = False
        self.in_vehicle = bool((state.get("npc") or {}).get("in_vehicle"))
        # Peer-Smalltalk (NPC-Geplauder, das mich nicht anspricht): nur als
        # Digest sammeln, NICHT pro Nachricht einen Zug ausloesen
        self.smalltalk: list[str] = []
        # INFO-DIGEST (analog Smalltalk): Routine-Sitrep des Lagezentrums +
        # Info-Ereignisse (_INFO_PREFIXES). Vom Lagezentrum zaehlt nur der
        # NEUESTE Sitrep (alte sind durch neue obsolet). Der Digest loest
        # selbst NIE einen Zug aus - drain_info haengt ihn an den naechsten.
        self.info_sitrep = ""
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
        # STILLSTAND: Anker-Position + Zeitpunkte des Stillstands-Waechters
        self.still_anchor: tuple[float, float] | None = None
        self.still_since = time.monotonic()
        self.still_last_warn = 0.0

    def drain_smalltalk(self) -> str:
        if not self.smalltalk:
            return ""
        digest = " | ".join(self.smalltalk)
        self.smalltalk = []
        return digest

    def drain_info(self) -> str:
        """Info-Digest abholen und leeren: max ~4 Zeilen (neuester
        Lagezentrum-Sitrep + bis zu 3 Info-Ereignisse). Faehrt nur als
        Prefix mit einem echten Weckruf mit, loest NIE selbst einen aus."""
        lines = []
        if self.info_sitrep:
            lines.append("Lagezentrum: " + self.info_sitrep)
        lines += self.info_lines
        self.info_sitrep = ""
        self.info_lines = []
        return "\n".join(lines)

    def fallen_fresh(self, name: str) -> bool:
        """True = dieser Tod ist neu und darf einen Weckruf ausloesen.
        Merkt sich den Namen; die jeweils andere Quelle (Peer-Poll vs.
        Lagezentrum-Funk) ist damit binnen FALLEN_DEDUPE_SEC unterdrueckt."""
        now = time.monotonic()
        key = (name or "").strip().lower()
        last = self.fallen_recent.get(key)
        self.fallen_recent[key] = now
        return last is None or (now - last) >= FALLEN_DEDUPE_SEC

    def route_funk(self, funk: dict) -> str:
        """Eine voice_inbox-Zeile einsortieren. Rueckgabe: Weckruf-Text
        (loest einen Zug aus) oder "" (in den Info-Digest uebernommen).
        Routine-Sitreps des Lagezentrums (prio=False, s. orchestrator.
        inbox_append) fahren nur im Digest mit; Prio-Funk (Tod/kritisch)
        bleibt ein echter Weckruf. 'X ist gefallen' wird dabei gegen den
        Peer-Poll dedupliziert (fallen_fresh): ist jeder genannte Tod schon
        gemeldet und sonst nichts kritisch, faellt der Funk in den Digest."""
        text = (funk.get("text") or "").strip()
        sender = str(funk.get("user") or "?")
        if sender.strip().lower() == "lagezentrum":
            fallen = [n for n in self.roster_names
                      if f"{n} ist gefallen" in text]
            fresh = [n for n in fallen if self.fallen_fresh(n)]
            if (not funk.get("prio")
                    or (fallen and not fresh
                        and "kritisch" not in text.lower())):
                self.info_sitrep = text
                return ""
        return f"FUNK von {sender}: \"{text}\""

    def _native_radio(self) -> dict:
        """ascii->native aus dem gemeinsamen Klartext-Funkkanal (letzte ~200
        Zeilen). Macht latinisierten Peer-Funk fuer den Empfaenger wieder lesbar -
        sonst kaeme z.B. Pinyin/Buckwalter statt sinnvollem Text an. Leere Map,
        wenn die Datei fehlt (= keine fremdsprachigen Funksprueche, Normalfall)."""
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

        # Chat (eigene Aussagen des NPCs nicht als Weckruf zurueckspielen)
        own_name = npc.get("name", "")
        own_l = AGENT_NAME.lower()
        roster_l = [n.lower() for n in self.roster_names]
        native_map = self._native_radio()   # latinisierten Peer-Funk wieder lesbar machen
        for m in state.get("chat", []):
            if m.get("id", 0) > self.last_chat_id:
                self.last_chat_id = m.get("id", 0)
                sender = m.get("sender") or ""
                if own_name and sender == own_name:
                    continue
                text_l = (m.get("text") or "").lower()
                # Latinisierten Funk eines fremdsprachigen NPCs wieder ins Original
                # bringen (Namen/Adressierung bleiben ASCII, also unberuehrt).
                disp = native_map.get(m.get("text") or "", m.get("text") or "")
                named = [n for n in self.roster_names if n.lower() in text_l]
                addressed_me = own_l in (n.lower() for n in named)
                # Nennt die Nachricht andere Agenten, aber nicht mich, ist sie
                # nicht fuer mich ("Igor, komm her" weckt Viktor nicht).
                if named and not addressed_me:
                    continue
                # Geplauder eines anderen KI-Survivors, das mich nicht direkt
                # anspricht: nur als Digest sammeln, keinen Zug ausloesen
                # (das war der groesste Token-Fresser - 75 % der Zuege).
                if sender.lower() in roster_l and not addressed_me:
                    self.smalltalk.append(f"{sender}: {disp}")
                    if len(self.smalltalk) > 8:
                        self.smalltalk.pop(0)
                    continue
                events.append(f"CHAT von {sender}: \"{disp}\"")

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
                # Fremde (nicht die eigene Squad) zaehlen zur Gefahrenzone - ein
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
        # genau EINMAL zum Begruessen melden (kein "aufgetaucht"-Spam).
        for name in self.greet_pending & players_now:
            events.append(f"SPIELER ANWESEND: '{name}' steht bei dir - "
                          f"begruesse ihn kurz.")
        self.greet_pending -= players_now
        self.known_players = players_now

        # Gefahrenzone voraus: mehrere Gegner dicht beieinander (Horde, Wolfsrudel
        # ODER bewaffnetes Camp). Frueh warnen, damit der NPC AUSWEICHT statt blind
        # hindurchzumarschieren - genau Viktors Tod (Beeline durchs Militaercamp).
        # Gelatcht, damit nicht pro Tick ein Zug ausgeloest wird.
        if hostiles_near >= CLUSTER_COUNT and not self.cluster_warned:
            self.cluster_warned = True
            events.append(f"GEFAHR: {hostiles_near} Gegner dicht beieinander voraus "
                          f"(Horde/Camp moeglich). Pruefe mit observe und WEICHE AUS "
                          f"oder kaempfe gezielt - lauf nicht blind mitten hindurch.")
        if hostiles_near < CLUSTER_COUNT:
            self.cluster_warned = False

        # Raubtier (Wolf/Baer) nah - schnell und toedlich, frueher warnen als Infizierte
        if predator_min < PREDATOR_NEAR and not self.predator_warned:
            self.predator_warned = True
            events.append(f"GEFAHR: Raubtier ({predator_what}) nur noch "
                          f"{predator_min:.0f} m! engage (Waffe ziehen) oder flee.")
        if predator_min > PREDATOR_NEAR + 15:
            self.predator_warned = False

        # Blutung (gelatcht): unbehandelt toedlich, darum kritischer Weckruf.
        # Das bleeding-Feld liefert die Mod seit 23.08. (IsBleeding im State).
        bleeding = bool(npc.get("bleeding"))
        if bleeding and not self.bleed_warned:
            self.bleed_warned = True
            events.append("DU BLUTEST! Verbinde dich SOFORT mit bandage() - "
                          "unbehandelt verblutest du.")
        if not bleeding:
            self.bleed_warned = False

        # Einzelner Infizierter nah (gelatcht)
        if infected_min < INFECTED_NEAR and not self.infected_warned:
            self.infected_warned = True
            events.append(f"GEFAHR: Infizierter nur noch {infected_min:.0f} m entfernt!")
        if infected_min > 55:
            self.infected_warned = False

        # Bewusstlosigkeit (gelatcht)
        uncon = bool(npc.get("unconscious"))
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

        # Kaelte (gelatcht): unter -0.5 friert er ernsthaft
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
        # alle nicht-kritisch -> laufen ueber die normale 15-s-Buendelung.
        events += self._poll_travel()
        events += self._poll_weather(state, npc)
        events += self._poll_disease(npc)
        events += self._poll_peers()
        events += self._poll_campfire(state, own_name)
        events += self._poll_stillstand(state, npc)

        # Info-only-Ereignisse (Wetter-Uebergaenge, Fahrzeug rein/raus) in den
        # Digest abzweigen: Latch-Logik oben unveraendert, nur die Zustellung
        # wechselt - kein eigener Zug mehr fuer reine Kenntnisnahmen.
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
        """Sekunden, die der Koerper praktisch unbewegt und unbeschaeftigt
        dasteht (klein/0 = in Bewegung oder beschaeftigt - der Anker wird
        dann laufend zurueckgesetzt). Fuer den Idle-Backoff-Stopp."""
        if self.still_anchor is None:
            return 0.0
        return max(0.0, time.monotonic() - self.still_since)

    def _poll_stillstand(self, state: dict, npc: dict) -> list[str]:
        """STILLSTAND: der NPC steht laenger unbewegt herum, ohne dass ein
        Kommando laeuft, er kaempft, folgt oder faehrt. Weckt das Gehirn mit
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
        # Spieler - keine Mahnung (der Zaehler laeuft weiter; geht der
        # Spieler, darf die Mahnung sofort kommen).
        if any(e.get("kind") == "player" for e in state.get("nearby", [])):
            return []
        standing = now - self.still_since
        if standing < STILL_WARN_SEC:
            return []
        if (now - self.still_last_warn) < STILL_REPEAT_SEC:
            return []
        self.still_last_warn = now
        return [(f"STILLSTAND: Du stehst seit {standing:.0f} Sekunden "
                 f"unbewegt bei x={px:.0f} z={pz:.0f} und nichts laeuft. "
                 f"Rumstehen ist gefaehrlich (Infizierte, Auskuehlung) und "
                 f"toetet den Spielfluss. Tu JETZT etwas Sichtbares: "
                 f"loot_area, explore_step, travel_to zu einem Ziel, "
                 f"dress_best/kochen am Lager oder follow zum Squad. "
                 f"Beende den Zug erst, wenn etwas laeuft.")]

    def _poll_travel(self) -> list[str]:
        """REISE: travel_event_<id>.json konsumieren. Der travel_to-Thread in
        dayz_mcp schreibt sie bei Ankunft/Abbruch:
        {"event":"arrived"|"stuck"|"aborted","x":<f>,"z":<f>,"detail":"<t>"}.
        Die Datei wird IMMER geloescht - auch wenn sie kaputt ist, sonst
        wuerde sie jeden Tick erneut gelesen (Weckruf-Spam)."""
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
        """WETTER: Uebergaenge aus state.world plus npc.wet. Jeder Uebergang
        ist gelatcht bis zum Gegenzustand (Sonnenstand: bis zum naechsten
        Wechsel; Regen/Naesse: mit Hysterese), damit kein Tick-Spam entsteht."""
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
                                      "an Licht, Waerme und einen sicheren "
                                      "Platz fuer die Nacht.")
                    elif sun == "night":
                        events.append("DIE NACHT BRICHT HEREIN. Schlechte "
                                      "Sicht - sei vorsichtig, bleib eher am "
                                      "Lager/Feuer oder nutze eine Lichtquelle.")
                    elif sun == "dawn":
                        events.append("DER MORGEN GRAUT. Die Sicht wird "
                                      "besser - gut fuer Aufbruch und Plaene.")
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
        """KRANKHEIT: state.npc.disease (tolerant). Die Mod traegt nur
        Erreger UEBER der Schwelle ein - pro Erreger gelatcht, bis er wieder
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
                active.add(str(key))  # eingetragen = ueber der Schwelle
        for key in sorted(active - self.disease_warned):
            name = _DISEASE_NAMES.get(key, key)
            events.append(f"DU BIST KRANK: {name} - behandle dich "
                          f"(treat_illness) oder such passende Medizin.")
        self.disease_warned = active
        return events

    def _poll_peers(self) -> list[str]:
        """KAMERAD GEFALLEN: state_<peer>.json der anderen Roster-Agenten auf
        den Uebergang lebendig -> tot pruefen. Gelatcht, bis der Peer wieder
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
                continue  # nie gespawnt/despawnt: kein Urteil ueber Leben/Tod
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
            return []  # Gefahr-Latch aktiv - kein Moment fuer Anekdoten
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
            # die Lagerfeuer-Runde konnte nie ausloesen.
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
                f"gerade ruhig. Nutze den Moment fuer EINE kurze Anekdote, "
                f"EINE Frage an einen Kameraden oder EINE Erinnerung aus "
                f"deinem memory/-Ordner (say_voice). EIN Zug - danach ganz "
                f"normal weiter."]


def save_inventory_snapshot(state: dict) -> None:
    npc = state.get("npc", {})
    if not npc.get("alive"):
        return
    items = [{"classname": i.get("classname", ""), "kind": i.get("kind", "other")}
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
    raw = snapshot.get("items", [])[:40]
    # Altes Format (reine Classnames) tolerieren
    items = [i if isinstance(i, dict) else {"classname": i, "kind": "other"}
             for i in raw]

    # Reihenfolge entscheidet ueber UEBERLEBEN. Medizin/Bandagen ZUERST: winzig,
    # passen immer in die Default-Cargo des frischen Koerpers, und sie duerfen
    # NIEMALS dem Platzmangel zum Opfer fallen - sonst respawnt der NPC
    # unbandagiert und verblutet sofort wieder (Todesspirale). Frueher stand
    # Kleidung an Rang 0 ("schafft erst Kapazitaet/Rucksack"), aber give_item
    # traegt Kleidung nicht zwingend und in den Logs (20:28/20:33) verlor die
    # Sanitaeterin trotzdem 5 Bandagen. Reihenfolge jetzt: Medizin -> Waffen ->
    # Kleidung -> Munition -> Rest. Feuerwaffen VOR Kleidung (dedizierte Waffenslots,
    # kein Cargo noetig) - standen sie dahinter, gingen sie bei Platzmangel verloren
    # und der Waffen-NPC respawnte unbewaffnet (Viktors "Gewehre weg"-Symptom).
    # Verloren geht so hoechstens redundante Kleidung/Munition, nie Medizin/Waffe.
    # Der Mod klassifiziert Medizin als kind=="other" (ClassifyItem kennt
    # nur food/firearm/ammo/clothing), darum hier zusaetzlich per Classname.
    _MEDICAL = ("bandage", "rag", "bloodbag", "saline", "morphine",
                "epinephrine", "tetracycline", "charcoal", "vitamin",
                "disinfectant", "alcoholtincture", "splint", "sewingkit",
                "defibrillator", "painkiller", "iodine")

    def _restore_rank(i):
        cn = i.get("classname", "").lower()
        if any(m in cn for m in _MEDICAL):
            return 0          # zuerst: lebenswichtig, winzig, passt immer
        if i.get("kind") == "firearm":
            return 1          # Waffe VOR Kleidung: belegt dedizierte Waffenslots
                              # (Schulter/Ruecken/Hand), braucht keinen Cargo - ging
                              # sonst bei Platzmangel verloren (unbewaffneter NPC).
        if i.get("kind") == "clothing":
            return 2          # danach Kleidung (Rucksack/Vest = Kapazitaet fuer den Rest)
        return {"ammo": 3}.get(i.get("kind", "other"), 4)

    items.sort(key=_restore_rank)

    journal.log(f"Stelle Inventar wieder her ({len(items)} Items)...")
    ok = 0
    failed: list[str] = []
    for item in items:
        result = bridge.run("give_item", text=item["classname"], timeout=6)
        if result.get("status") == "done":
            ok += 1
        else:
            failed.append(item["classname"])

    hands = snapshot.get("hands", "")
    if hands:
        bridge.run("equip", text=hands, timeout=6)

    # Hand-Equip macht ggf. einen Schulterplatz frei: ein Retry-Durchgang
    still_failed: list[str] = []
    for classname in failed:
        result = bridge.run("give_item", text=classname, timeout=6)
        if result.get("status") == "done":
            ok += 1
        else:
            still_failed.append(classname)

    msg = f"Inventar wiederhergestellt: {ok} ok"
    if still_failed:
        msg += f", verloren: {', '.join(still_failed)} (kein Platz - DayZ-Slot-Physik)"
    journal.log(msg)


def ensure_body(bridge: Bridge, journal: Journal, restore: bool = True,
                loadout_default: str = "", fresh: bool = False) -> bool:
    """fresh=True (Menue-Loadout gewaehlt): der Koerper wird IMMER frisch mit
    loadout_default gespawnt - ein noch lebender/uebernommener Alt-Koerper
    wuerde sonst mit seinem alten Inventar weiterlaufen und die Menue-Wahl
    stillschweigend ueberschreiben."""
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    if npc.get("spawned") and npc.get("alive"):
        if fresh and loadout_default:
            journal.log("Menue-Loadout gewaehlt - alter Koerper wird fuer das "
                        "frische Equip despawnt.")
            bridge.run("despawn", timeout=10)
        else:
            journal.log(f"Koerper vorhanden: {npc.get('classname')}")
            return True

    journal.log("Kein lebender Koerper - versuche adopt_nearest...")
    result = bridge.run("adopt_nearest", timeout=15)
    if result.get("status") == "done":
        if fresh and loadout_default:
            # Erst uebernehmen (sonst bliebe der Alt-Koerper als Doppelgaenger
            # in der Welt stehen), dann verwerfen und frisch spawnen.
            journal.log("Menue-Loadout gewaehlt - uebernommenen Alt-Koerper "
                        "despawnt, spawne frisch mit Loadout.")
            bridge.run("despawn", timeout=10)
        else:
            # adopt_nearest nimmt den ersten herrenlosen eAI ohne Ruecksicht auf die
            # Distanz. Pruefen, ob der Koerper nah genug am Lager liegt - sonst ist es
            # ein zufaelliger Expansion-eAI irgendwo auf der Karte: verwerfen und am
            # Lager frisch spawnen, damit die Gruppe wirklich am Lager zusammensteht.
            snap = bridge.read_state() or {}
            body = snap.get("npc", {})
            ddx = body.get("pos_x", 0.0) - SPAWN_POS[0]
            ddz = body.get("pos_z", 0.0) - SPAWN_POS[1]
            adopt_dist = (ddx * ddx + ddz * ddz) ** 0.5
            if adopt_dist <= ADOPT_MAX_DIST:
                journal.log(f"Verwaiste eAI uebernommen: {result.get('detail') or ''} "
                            f"({adopt_dist:.0f} m vom Lager, ok)")
                return True
            journal.log(f"adopt_nearest lieferte Koerper {adopt_dist:.0f} m vom Lager "
                        f"(> {ADOPT_MAX_DIST:.0f} m) - verwerfe und spawne neu am Lager.")
            bridge.run("despawn", timeout=10)
            # faellt durch zum kontrollierten Spawn am Lager unten

    # Erster Spawn ohne Snapshot: Berufs-Loadout des Agenten
    snapshot = None
    loadout = loadout_default
    if restore:
        snapshot = load_inventory_snapshot()
        if snapshot:
            # nackter Start - das Inventar kommt gleich aus dem Snapshot zurueck
            loadout = "FreshSpawnLoadout"

    journal.log(f"Spawne neuen Koerper '{AGENT_NAME}' ({AGENT_FACTION}) bei {SPAWN_POS}...")
    result = bridge.run("spawn", x=SPAWN_POS[0], z=SPAWN_POS[1],
                        loadout=loadout, text=AGENT_NAME,
                        faction=AGENT_FACTION, br=AGENT_BR, timeout=30)
    if result.get("status") != "done":
        journal.log("Spawn fehlgeschlagen: " + (result.get("detail") or ""))
        return False

    # Auf echtes Lebendsein warten: spawn meldet "done", sobald der eAI erzeugt
    # ist, aber der Koerper braucht ein paar Server-Ticks, bis er im State als
    # spawned+alive auftaucht. Ohne dieses Warten laufen restore_inventory und
    # der erste Weckruf gegen einen halbgeladenen Koerper ("npc ist tot"-Schleife).
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
    und script-gespawnte Bestaende (Depots, abgelegte Items, Leichen)
    ueberleben den Neustart nicht - eine neue Kennung seit dem letzten Lauf
    bedeutet also Neustart/Wipe aus Agentensicht."""
    try:
        rpts = glob.glob(os.path.join(profile, "*.RPT"))
        if not rpts:
            return ""
        newest = max(rpts, key=os.path.getctime)
        return f"{os.path.basename(newest)}|{int(os.path.getctime(newest))}"
    except OSError:
        return ""


def main() -> int:
    global AGENT_HOME, SNAPSHOT_FILE, VOICE_INBOX, STOP_FLAG, SPAWN_POS, AGENT_NAME, AGENT_FACTION, AGENT_BR, AGENT_FREE, AGENT_LANG, INTENT_FILE

    # Konsole gegen Encoding-Abstuerze haerten (Viktor schreibt deutsch)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    # CTRL_BREAK (vom Arena-Supervisor) wie Strg+C behandeln - sonst stirbt
    # der Prozess hart und der finally-Block (Inventar sichern, despawnen)
    # laeuft nie
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
                        help="Lokales Mikrofon-Hoeren NICHT starten")
    parser.add_argument("--npc-id", default="viktor",
                        help="Agenten-Slot (Arena), Default viktor")
    parser.add_argument("--name", default="",
                        help="Anzeigename (Default: Npc-Id kapitalisiert)")
    parser.add_argument("--home", default="",
                        help="Arbeitsverzeichnis des Agenten (CLAUDE.md, Journale)")
    parser.add_argument("--spawn-x", type=float, default=SPAWN_POS[0])
    parser.add_argument("--spawn-z", type=float, default=SPAWN_POS[1])
    parser.add_argument("--rally-x", type=float, default=None,
                        help="Treffpunkt-X fuer getrennten Spawn: der Agent "
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
    parser.add_argument("--loadout", default="",
                        help="Expansion-Loadout fuer den ERSTEN Spawn ohne "
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
                             "(Leiche bleibt als Loot liegen). Fuer Battle-Royale.")
    parser.add_argument("--free", action="store_true",
                        help="Freier Survival-Modus: Persona-Lockerung (der Agent "
                             "entscheidet selbst, was er tut), 1 Leben (impliziert "
                             "--no-respawn), keine Rally-/Treffpunkt-Auftraege. "
                             "Kein BR: Funk bleibt an, niemand ist automatisch Feind.")
    args = parser.parse_args()

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
    SPAWN_POS = (args.spawn_x, args.spawn_z)
    AGENT_NAME = args.name or args.npc_id.capitalize()
    AGENT_FACTION = args.faction
    AGENT_BR = 1 if args.br else 0
    AGENT_FREE = 1 if args.free else 0
    # Freier Modus = 1 Leben: den bestehenden --no-respawn-Mechanismus
    # wiederverwenden (Tod -> Runner endet sauber, Leiche bleibt als Loot).
    if args.free:
        args.no_respawn = True
    AGENT_LANG = (args.language or "de").lower()

    # Stimme aus dem Roster nachschlagen, wenn keine uebergeben wurde -
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
    # der Zusatz bricht das Parsen also nicht (gegengeprueft).
    mode_note = " mode=free" if args.free else ""
    journal.log(f"=== IsuSurvivor Agent-Runner | npc={args.npc_id} ({AGENT_NAME}) "
                f"model={args.model}{mode_note} idle={args.idle}s ===")

    bridge = Bridge(args.profile, args.npc_id)
    INTENT_FILE = os.path.join(bridge.dir, f"intent_{args.npc_id}.txt")
    # Stale-Intent aus der VORIGEN Session loeschen: sonst funkt der Orchestrator
    # beim Kaltstart noch die alte Gedankenzeile ("(will: ...)"), bis das Gehirn
    # die erste frische Absicht schreibt. Genau das zeigte die 22:50-Session:
    # "Birgit ... (will: Zelt-Bug-Eintrag)" stammte aus der Vorrunde.
    try:
        with open(INTENT_FILE, "w", encoding="utf-8") as _f:
            _f.write("")
        # Auch die native Begleitfassung der Vorrunde raeumen (sonst funkt der
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
    # tp_requested = Spieler hat "tp" im Chat/Funk gesagt (jederzeit moeglich).
    tp_pending = not args.no_tp
    tp_requested = False

    def try_tp() -> bool:
        state = bridge.read_state() or {}
        if state.get("command", {}).get("status") == "running":
            return False  # laufenden Befehl des Gehirns nicht verdraengen
        r = bridge.run("teleport_player", timeout=10)
        return r.get("status") == "done"

    if tp_pending:
        if try_tp():
            journal.log("Spieler zum NPC teleportiert.")
            tp_pending = False
        else:
            journal.log("Noch kein Spieler verbunden - teleportiere, sobald jemand joint.")

    os.makedirs(AGENT_HOME, exist_ok=True)
    # Rundenkosten-Export zuruecksetzen - sonst summiert der Supervisor die
    # Restwerte der letzten Runde mit.
    _write_round_cost(0.0)

    # Voice-Datei-Hygiene VOR dem Claude-Start, sonst frisst das Loeschen
    # die ersten Aeusserungen (Arena-Modus: der Supervisor uebernimmt das)
    own_outbox = os.path.join(AGENT_HOME, "voice_outbox.jsonl")
    if not args.no_voice_procs:
        for stale in (own_outbox, VOICE_INBOX):
            if os.path.exists(stale):
                os.remove(stale)

    mcp_cfg = build_mcp_config(args.profile, args.npc_id, args.voice)
    _cli_model, _benv, backend = resolve_backend(args.model)
    if backend == "anthropic-api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            journal.log("!!! api/-Modell gewaehlt, aber ANTHROPIC_API_KEY fehlt "
                        "- Claude Code faellt sonst auf den Max-Plan-Login "
                        "zurueck oder scheitert. Key setzen (dauerhaft): "
                        "setx ANTHROPIC_API_KEY sk-ant-...")
        else:
            journal.log(f"Backend: {args.model} -> {_cli_model} ueber die "
                        f"Anthropic-API (eigener API-Key, Kosten pro Token, "
                        f"NICHT ueber den Max-Plan).")
    elif backend != "anthropic":
        journal.log(f"Fremd-Backend: {args.model} -> {backend} - der Dienst "
                    f"muss laufen (Arena-Menue startet ihn automatisch, "
                    f"Einzelstart: tools\\start_router.ps1 bzw. "
                    f"tools\\start_llama_gemma.ps1). Kosten laufen NICHT "
                    f"ueber den Max-Plan; USD-Anzeige im Journal gilt nur "
                    f"fuer Claude-Modelle.")
    proc = spawn_claude(mcp_cfg, args.model, args.character, args.turn_limit)
    journal.log(f"Claude Code gestartet (PID {proc.pid}, Modell {args.model}, "
                f"Backend {backend}, Zug-Limit {args.turn_limit or 'aus'}).")

    # Discord-Voice-Bruecke: startet mit, wenn ein Bot-Token gesetzt ist
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
        # Hoert das lokale Mikro mit (mic_listener), darf der Bot nicht
        # zusaetzlich hoeren - sonst kommt jeder Funkspruch doppelt an
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
        # Warnung war dort ein Fehlalarm (Voice lief laengst, 03.07.).
        journal.log("!" * 60)
        journal.log("!!! Kein DISCORD_BOT_TOKEN gesetzt - Discord-Voice bleibt AUS.")
        journal.log("!!! say erscheint dann NUR im Spiel-Textchat, nicht im Funk.")
        journal.log("!!! Setup: docs/discord_bot_setup.md, Schritt 5.")
        journal.log("!" * 60)

    # Mikrofon-Hoeren (lokal): Discord-Empfang ist durch DAVE-E2EE blockiert,
    # dein Mikro fuettert dieselbe Funk-Inbox
    if not args.no_voice_procs and not args.no_mic and os.environ.get("ELEVENLABS_API_KEY"):
        mic_proc = subprocess.Popen(
            [sys.executable, os.path.join(DAEMON_DIR, "mic_listener.py"),
             "--inbox", VOICE_INBOX, "--parent-pid", str(os.getpid())],
            stdout=open(os.path.join(AGENT_HOME, "journal", "mic_listener.log"),
                        "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        journal.log(f"Mikrofon-Hoeren gestartet (PID {mic_proc.pid}) - "
                    f"{AGENT_NAME} hoert dich ueber dein Mikro.")
    elif not args.no_mic:
        journal.log("Mikrofon-Hoeren AUS (ELEVENLABS_API_KEY fehlt).")

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
            "anderen Ueberlebenden UND der Spieler sind deine Gegner - keine "
            "Verbuendeten, kein Funk, keine Absprachen. Du hast nur 1 Leben. Mach "
            "zuerst observe, halte mit equip_best deine Waffe bereit und brich zum "
            f"Treffpunkt bei x={args.rally_x:.0f} z={args.rally_z:.0f} auf - dort "
            "wird es entschieden. Du startest leicht: loote unterwegs Waffen, "
            "Munition, Schutz. Triffst du jemanden BEVOR du ankommst, kaempfe "
            f"sofort. Starte den Marsch mit travel_to(x={args.rally_x:.0f}, "
            f"z={args.rally_z:.0f}) - das laeuft im Hintergrund, weicht "
            "Hindernissen selbst aus und du bleibst waehrend des Marsches "
            "handlungsfaehig. Zuerst schiessen, dann denken."
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
        # Spieler-Naehe-Vorrang-Regel - nur das eine Ziel: ueberleben.
        first_msg = (
            "Du erwachst in der Wildnis. Es gibt keinen Auftrag und keinen "
            "Treffpunkt - du bist voellig frei und entscheidest selbst, was du "
            "tust: kooperieren, alleine ziehen, siedeln, horten, erkunden. "
            "Aber merke dir: du hast nur EIN Leben, der Tod ist endgueltig. "
            "Mach zuerst observe, sorge fuer Ausruestung, Waerme und Nahrung "
            "und ueberlebe so lange wie moeglich."
        )
    elif rally_active:
        first_msg = (
            "Du erwachst ALLEIN in einer fremden Gegend, weit weg von deiner "
            "Gruppe. Der Spieler ist NICHT hier - ihr koordiniert euch selbst per "
            "Funk (say). Mach zuerst observe, dann brich zum Treffpunkt bei "
            f"x={args.rally_x:.0f} z={args.rally_z:.0f} auf. Der Weg ist WEIT "
            "(oft mehrere Kilometer) - starte den Marsch mit "
            f"travel_to(x={args.rally_x:.0f}, z={args.rally_z:.0f}): das laeuft "
            "im Hintergrund, weicht Hindernissen selbst aus, und du meldest "
            "dich einfach wieder, wenn das REISE-Ereignis kommt. NICHT move_to "
            "in Schleife rufen. EINE Ausnahme: taucht unterwegs der Spieler "
            "(ein Mensch, kind player in observe) nah bei dir auf, bleib bei ihm, "
            "begruesse ihn und warte auf seine Anweisung, statt an ihm "
            "vorbeizumarschieren. Sorge unterwegs fuer dich (Waerme, Nahrung, "
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

    # Hauptschleife: Ereignisse werden SOFORT eingespeist (Claude Code
    # reiht sie hinter dem laufenden Zug ein), nicht erst nach Zug-Ende.
    # pending = gesendete Weckrufe ohne result; bei >= 2 wird gepuffert,
    # damit sich kein Rueckstau aus veralteten Ereignis-Zuegen bildet.
    try:
        send_user(proc, first_msg)
        journal.log("[WECKRUF] " + first_msg[:200])
        pending = 1
        interrupted = False
        buffered: list[str] = []
        buffer_deadline = 0.0     # 0 = kein offenes Buendel
        idle_deadline = time.monotonic() + args.idle
        snapshot_tick = 0
        last_ctx = 0              # Kontextgroesse des letzten Zuges (Tokens)
        turns_since_rotate = 0    # Zuege seit der letzten frischen Session
        rotate_pending = False    # Kontext-Schwelle gerissen: Swap folgt, sobald
                                  # der laufende Zug fertig ist (transaktional)
        # CCR (Gemini/OpenAI/xAI) und lokaler llama-server melden keine
        # Token-Usage -> last_ctx bleibt 0 -> der Token-Trigger feuert nie.
        # Auf diesen Backends den zugbasierten Fallback (CTX_ROTATE_TURNS) nutzen.
        usage_blind = backend not in ("anthropic", "anthropic-api")
        death_times: list[float] = []   # Tod-Zeitpunkte fuer die Schleifen-Bremse
        # Adaptiver Idle-Backoff (Audit 03.07.): Routine-Ticks alle args.idle s
        # liefen auch, wenn nichts passierte und kein Spieler da war - reine
        # Token-Verbrennung. Der Faktor (1 -> 2 -> 4, Cap 4) streckt NUR den
        # Routine-Tick-Abstand; nach jedem Tick ohne vorheriges echtes Ereignis
        # verdoppelt er sich. Zurueck auf 1 bei jedem echten Event-Dispatch
        # (Spieler-Funk/Chat inklusive) oder sobald ein Mensch in Sichtweite
        # ist (watcher.known_players). Im BR-Modus fest 1 - dort zaehlt Tempo.
        idle_backoff = 1
        event_since_tick = True   # echtes Ereignis seit dem letzten Routine-Tick?

        def dispatch(msg: str, routine: bool = False):
            nonlocal pending, interrupted, idle_deadline, idle_backoff, \
                event_since_tick
            # Info-Digest (Routine-Sitrep, Wetter, Fahrzeug) faehrt als
            # Kenntnis-Prefix mit dem echten Weckruf mit - loest also nie
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
                    journal.log(f"[IDLE] zurueck auf {args.idle}s")
            send_user(proc, msg)
            journal.log("[WECKRUF] " + msg.replace("\n", " | ")[:200])
            pending += 1
            idle_deadline = time.monotonic() + args.idle

        while True:
            # Stop-Flag vom Supervisor? Wie Strg+C behandeln, damit der
            # finally-Block laeuft (Inventar sichern, despawnen).
            if os.path.exists(STOP_FLAG):
                journal.log("Stop-Flag vom Supervisor erkannt - beende sauber.")
                raise KeyboardInterrupt

            # Beendete Zuege abholen (nicht blockierend)
            try:
                while True:
                    result = reader.results.get_nowait()
                    if result is None:
                        return 1
                    pending = max(0, pending - 1)
                    turns += 1
                    turns_since_rotate += 1
                    interrupted = result.get("subtype") == "error_max_turns"
                    idle_deadline = time.monotonic() + args.idle
                    # Kontextgroesse = LETZTER einzelner API-Schritt (BrainReader),
                    # NICHT die Zug-Summe aus result.usage: die addiert cache_read
                    # ueber alle Schritte und loeste bei jedem mehrstufigen Zug
                    # eine Rotation aus (Thrashing: Session-Reset alle ~15 s,
                    # Arbeitsgedaechtnis weg, NPCs wirkten kopflos, 02.07.).
                    last_ctx = reader.last_step_ctx
                    if args.once:
                        journal.log("=== --once erledigt ===")
                        return 0
                    if args.max_turns and turns >= args.max_turns:
                        journal.log(f"=== max-turns ({args.max_turns}) erreicht ===")
                        return 0
            except queue.Empty:
                pass
            if reader.dead:
                return 1

            # Kontext-Schwelle pruefen: NUR das Flag setzen (transaktionale
            # Rotation). Der eigentliche Proc-Swap unten wartet, bis der
            # laufende Zug abgeschlossen ist und pending==0 - so zerreisst
            # die Rotation nie einen halben Zug. Token-Trigger (last_ctx)
            # ODER - auf usage-blinden Backends, wo last_ctx 0 bleibt - der
            # zugbasierte Fallback (sonst waechst die Gemini/CCR-Session bis
            # "Prompt is too long").
            if (not rotate_pending
                    and (last_ctx > CTX_ROTATE
                         or (usage_blind and turns_since_rotate >= CTX_ROTATE_TURNS))):
                rotate_pending = True
                journal.log(f"[ROTATION] vorgemerkt (Tokens={last_ctx}, "
                            f"Zuege={turns_since_rotate}, usage_blind={usage_blind}) "
                            f"- Swap, sobald der laufende Zug abgeschlossen ist.")

            # Session-Rotation (transaktional): Schwelle gerissen UND der
            # laufende Zug ist fertig (pending==0) - jetzt den Claude-Prozess
            # gegen eine frische, kontextarme Session tauschen. Das Gedaechtnis
            # liegt in CLAUDE.md/memory und bleibt; den Arbeitsfaden traegt
            # die Handover-Notiz hinueber (kein Sicherungs-Zug mehr noetig).
            if rotate_pending and pending == 0:
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
                    proc.kill()
                proc = spawn_claude(mcp_cfg, args.model, args.character,
                                    args.turn_limit)
                reader = BrainReader(proc, journal, tracker)
                reader.follow_target = carry_follow  # ueberlebt die Rotation
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
                # Inhalt (Gedaechtnis-Hinweis, Intent, Follow, Faden), ~60%
                # weniger Weckruf-Tokens.
                handover = ("KONTEXT-WECHSEL (frische Session; Gedaechtnis in "
                            "CLAUDE.md/memory erhalten).")
                if last_intent:
                    handover += f' Zuletzt: "{last_intent}".'
                if carry_follow:
                    handover += f" Du folgst: {carry_follow}."
                if carry_recent:
                    last_few = " | ".join(s[:120] for s in carry_recent[-3:])
                    handover += f" Letzte Gedanken: {last_few}."
                handover += (" Kurz observe, dann deinen Plan nahtlos "
                             "fortsetzen - nicht bei null anfangen.")
                send_user(proc, handover)
                journal.log("[WECKRUF] (nach Rotation) KONTEXT-WECHSEL-Handover gesendet")
                pending = 1
                time.sleep(1.0)
                continue

            # Welt-Ereignisse einsammeln
            events, state = watcher.poll()
            # Battle-Royale = Funkstille: keinen Funk lesen (auch keine tp-Bitte),
            # damit sich die Gegner nicht koordinieren koennen (R1).
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
            buffered += kept
            if (tp_pending or tp_requested) and try_tp():
                journal.log("Spieler zum NPC teleportiert.")
                tp_pending = False
                tp_requested = False

            snapshot_tick += 1
            if snapshot_tick % 5 == 0:
                save_inventory_snapshot(state)

            # Tod hat Vorrang: neuen Koerper besorgen, Meldung sofort senden
            if "DU BIST GESTORBEN." in buffered:
                # Battle-Royale: nur 1 Leben. Kein Respawn - der Agent scheidet
                # aus, die Leiche bleibt als Loot liegen, der Runner endet sauber
                # (finally raeumt Voice/Mic auf, despawnt aber nicht).
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
                # (Spawn-Glitch/toedliche Zone) - NICHT sofort eine neue, teure
                # Claude-Session starten, sondern pausieren. Koerper despawnen,
                # Cooldown abwarten (Stop-Flag bleibt wirksam), dann ein Versuch.
                if len(death_times) >= DEATH_LOOP_MAX:
                    journal.log(
                        f"[TOD-CAP] {len(death_times)} Tode in "
                        f"<{DEATH_WINDOW_SEC:.0f}s - Todes-Schleife. Pausiere "
                        f"{DEATH_COOLDOWN_SEC:.0f}s OHNE neue Session (spart "
                        f"Tokens); danach ein neuer Versuch.")
                    try:
                        proc.stdin.close()
                        proc.wait(timeout=10)
                    except Exception:
                        proc.kill()
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
                    # Stichpunkt-Format statt Prosa (Audit 03.07.), Inhalt
                    # komplett: observe, Todesort meiden, Rally, Gedaechtnis.
                    send_user(proc,
                              "NEU ERWACHT NACH TODES-SERIE. Zuerst observe - "
                              "alte Lage gilt NICHT mehr. Dein Todesort ist "
                              "toedlich: meide ihn, erst Sicherheit (Distanz, "
                              "Deckung), dann Risiko." + rally_hint +
                              " Erinnerungen (CLAUDE.md/memory) bleiben.")
                    journal.log("[TOD-CAP] Cooldown vorbei - frische Session, "
                                "Vorsicht-Weckruf gesendet")
                    pending = 1
                    time.sleep(2.0)
                    continue

                journal.log(f"[TOD] Koerper verloren ({len(death_times)} in "
                            f"<{DEATH_WINDOW_SEC:.0f}s), spawne neuen...")
                # Leichen-Position merken, BEVOR der neue Koerper spawnt -
                # die Leiche bleibt lootbar (kind=corpse) liegen
                dead_state = bridge.read_state() or {}
                dead_npc = dead_state.get("npc", {})
                death_pos = ""
                if dead_npc.get("spawned"):
                    death_pos = (f" Leiche: x={dead_npc.get('pos_x', 0):.0f} "
                                 f"z={dead_npc.get('pos_z', 0):.0f} "
                                 f"(loot_corpse, wenn der Weg es wert ist).")
                ensure_body(bridge, journal, restore=not args.no_restore,
                            loadout_default=args.loadout,
                            fresh=args.fresh_loadout)
                # Frische Claude-Session: das Gehirn darf NICHT mit dem kompletten
                # Vor-Tod-Kontext (100+ Zuege) weiterdenken. Sonst handelt es nach
                # dem Respawn auf alten Positionen/Plaenen, jagt verschwundene
                # Zombies oder folgt unerreichbaren Zielen - wirkt eigenartig und
                # reagiert auf nichts. Wie bei der Rotation den Prozess tauschen;
                # CLAUDE.md/memory bleibt, nur der Gespraechsverlauf faellt weg.
                try:
                    proc.stdin.close()
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                proc = spawn_claude(mcp_cfg, args.model, args.character,
                                    args.turn_limit)
                reader = BrainReader(proc, journal, tracker)
                reader.start()
                watcher = EventWatcher(bridge)
                buffered = []  # alte Ereignisse betreffen das alte Leben
                buffer_deadline = 0.0
                rotate_pending = False
                last_ctx = 0
                turns_since_rotate = 0
                interrupted = False
                idle_backoff = 1              # frisches Leben = volle Aufmerksamkeit
                event_since_tick = True
                # Getrennter Modus: nach dem Respawn wieder allein zum Treffpunkt
                # und zur Gruppe zurueckfinden.
                rally_hint = ""
                if rally_active:
                    rally_hint = (f" Danach: allein zum Treffpunkt "
                                  f"x={args.rally_x:.0f} z={args.rally_z:.0f} - "
                                  f"per travel_to (laeuft im Hintergrund, "
                                  f"Distanz ist kein Abbruchgrund), Gruppe "
                                  f"per Funk finden.")
                # Stichpunkt-Format statt Prosa (Audit 03.07.), Inhalt komplett:
                # observe, Ausruestung/Leiche, Todesposition, Rally, Gedaechtnis,
                # Lektion.
                send_user(proc,
                          "DU BIST GESTORBEN UND NEU ERWACHT. Zuerst observe - "
                          "alte Lage (Position/Ziele/Gegner/Plaene) gilt NICHT "
                          "mehr. Ausruestung: teils wiederhergestellt, Rest bei "
                          "deiner Leiche." + death_pos + rally_hint +
                          " Erinnerungen (CLAUDE.md/memory) bleiben; trage die "
                          "Todesursache als Lektion ein, wenn du sie kennst.")
                journal.log("[TOD] frische Session gestartet - Respawn-Weckruf gesendet")
                pending = 1
                time.sleep(2.0)
                continue

            # Buendelung: nicht-kritische Ereignisse erst ~15 s sammeln, dann
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

            # Watchdog gegen pending-Drift: pending kann durch Mid-Turn-Einspeisung
            # (mehrere Weckrufe verschmelzen zu EINEM Zug-Ende) ueber 0 haengen
            # bleiben. Dann feuern Rotation und Selbstantrieb (beide verlangen
            # pending==0) nie wieder, waehrend Event-Weckrufe (pending<=1)
            # weiterlaufen - der NPC wirkt apathisch und reagiert nur noch, wenn
            # der Spieler hinkommt. Hat das Gehirn aber seit STUCK_QUIET_SEC GAR
            # nichts ausgegeben und liegt nichts an, laeuft sicher kein Zug mehr:
            # Saldo hart auf 0 ziehen, damit der Selbstantrieb wieder anspringt.
            # Laeuft BEWUSST auch bei gesetztem rotate_pending: haengt der Saldo,
            # faende der transaktionale Swap (wartet auf pending==0) sonst nie statt.
            # KEIN "not buffered" mehr: bei pending>=2 wird nicht dispatcht,
            # buffered leert sich also nie - und die alte Bedingung liess den
            # Watchdog genau dann nie greifen (Deadlock, NPC dauerhaft stumm).
            # Die lange Stille allein ist Beweis genug, dass kein Zug mehr laeuft.
            if (pending > 0
                    and (time.monotonic() - reader.last_activity) >= STUCK_QUIET_SEC):
                journal.log(f"[WATCHDOG] pending={pending} trotz "
                            f"{time.monotonic() - reader.last_activity:.0f}s Stille - "
                            f"Saldo verdriftet, setze auf 0 (Selbstantrieb frei).")
                pending = 0

            # Mensch in Sichtweite -> volle Aufmerksamkeit: Idle-Backoff sofort
            # aufheben, nicht erst beim naechsten (gestreckten) Routine-Tick.
            # Roster-NPCs zaehlen nicht - die eigene Squad steht immer daneben.
            roster_l = {str(r).lower() for r in watcher.roster_names}
            human_near = any(str(n).lower() not in roster_l
                             for n in watcher.known_players)
            if idle_backoff != 1 and human_near:
                idle_backoff = 1
                journal.log(f"[IDLE] zurueck auf {args.idle}s "
                            f"(Spieler in Sichtweite)")

            # Waehrend rotate_pending KEINE neuen Weckrufe mehr starten: der
            # Swap wartet auf pending==0, jeder neue Zug wuerde ihn verzoegern.
            # buffered bleibt stehen und wird nach dem Swap der frischen
            # Session zugestellt (nichts geht verloren).
            if buffered and pending <= 1 and bundle_ready and not rotate_pending:
                smalltalk = watcher.drain_smalltalk()
                prefix = ""
                if smalltalk:
                    prefix = ("FUNKGEPLAUDER der anderen (nur zur Info, keine "
                              "Antwort noetig): " + smalltalk + "\n\n")
                dispatch(prefix + "EREIGNIS:\n- " + "\n- ".join(buffered) +
                         "\n\nReagiere angemessen (observe zuerst, falls noetig).")
                buffered = []
                buffer_deadline = 0.0

            # Routine-Tick nur im echten Leerlauf (und nie mit haengender
            # Rotation). Der Backoff-Faktor streckt AUSSCHLIESSLICH dieses
            # Gate (idle_deadline selbst wird an mehreren Stellen - Zug-Ende,
            # Dispatch - immer mit dem Basis-idle gesetzt; der Faktor gehoert
            # nur in die Routine-Tick-Rechnung, nicht in die Event-Pfade).
            elif (pending == 0 and not rotate_pending
                  and time.monotonic() >= idle_deadline
                      + (idle_backoff - 1) * args.idle):
                # Kein echtes Ereignis seit dem vorigen Routine-Tick: Abstand
                # verdoppeln (1 -> 2 -> 4, Cap 4). Im BR-Modus fest 1; bei
                # Mensch in Sichtweite ebenfalls nie strecken. Und NIE
                # strecken, wenn der NPC stationaer herumsteht (Analyse
                # 24.08.: 480-s-Luecken bei stehenden NPCs) - der Backoff
                # ist fuer beschaeftigte NPCs (Reise/Marsch), nicht fuer
                # Rumsteher.
                stationary = watcher.stationary_secs() >= STILL_WARN_SEC
                if stationary and idle_backoff != 1:
                    idle_backoff = 1
                    journal.log(f"[IDLE] zurueck auf {args.idle}s "
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
                    tick = ("ROUTINE-TICK: Eine Weile ist vergangen. Pruefe die "
                            "Lage. Keine Gefahr? Dann nutze die Zeit fuer DEINE "
                            "eigenen Ziele: looten, erkunden, Vorraete anlegen, "
                            "Inventar ordnen, kochen. Denk daran: du hast nur "
                            "EIN Leben - geh kein unnoetiges Risiko ein. Und "
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
                              "laeuft (Bewegung, Looten, Kochen, Wache mit "
                              "Ansage).")
                if smalltalk:
                    tick = ("FUNKGEPLAUDER der anderen seither: " + smalltalk
                            + "\n\n" + tick)
                dispatch(tick, routine=True)

            time.sleep(2.0)

    except KeyboardInterrupt:
        journal.log("=== Abbruch durch Benutzer ===")
        return 0
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        if discord_proc:
            discord_proc.terminate()
        if mic_proc:
            mic_proc.terminate()
        # Koerper aufraeumen: eine Huelle ohne Gehirn steht sonst stumm in der
        # Welt und sieht aus wie "der NPC antwortet nicht". Inventar wird
        # vorher gesichert und beim naechsten Start wiederhergestellt.
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
