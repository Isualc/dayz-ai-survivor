#!/usr/bin/env python3
"""IsuSurvivor MCP-Server — stellt die DayZ-Bridge als Werkzeuge fuer Claude bereit.

Wird von Claude Code als stdio-MCP-Server gestartet (siehe run_agent.py).
Alle Werkzeuge blockieren bis zum Ergebnis (mit Zeitdeckel) und geben
deutschen Klartext zurueck.
"""

import argparse
import json
import os
import time

from mcp.server.fastmcp import FastMCP

from bridge import Bridge, format_observation, inventory_signature, DEFAULT_PROFILE
import tactics

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDSETS_FILE = os.path.join(REPO_DIR, "voice", "soundsets.json")
# VOICE_OUTBOX wird nach dem Argument-Parsing pro Agent gesetzt (jeder Bot
# hat seine eigene Sprech-Warteschlange in seinem agent_home)
VOICE_OUTBOX = os.path.join(REPO_DIR, "agent_home", "voice_outbox.jsonl")
_VOICE_CATALOG: dict | None = None


def _agent_home(npc_id: str) -> str:
    if npc_id == "viktor":
        return os.path.join(REPO_DIR, "agent_home")
    return os.path.join(REPO_DIR, "agent_homes", npc_id)


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

# equip_best-Circuit-Breaker: aufeinanderfolgende Fehlschlaege, damit der NPC
# nicht stur dieselbe scheiternde Aktion wiederholt (Konrad-Muster, Logs 20:28).
_EQUIP_FAILS = 0
_EQUIP_LAST_FAIL = 0.0


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
#   falls er vom Funk-Namen abweicht. Leer = live aus der Welt holen.
# Die Bots hoeren Befehle unter dem Funk-Namen, follow/regroup/give_to der Mod
# brauchen aber den Profilnamen - _resolve_player_name uebersetzt zwischen beiden.
PLAYER_RADIO_NAME = os.environ.get("ISU_MIC_NAME", "Player")
PLAYER_PROFILE_NAME = os.environ.get("ISU_PLAYER_NAME", "")

mcp = FastMCP("dayz")


def _observe_text(full: bool = False) -> str:
    global LAST_CHAT_ID, LAST_INV_SIG
    state = BRIDGE.read_state()
    sig = inventory_signature(state)
    # Inventar nur ausschreiben, wenn es sich seit dem letzten observe
    # geaendert hat (oder full angefordert ist) - das spart spuerbar Tokens
    inv_unchanged = (not full) and LAST_INV_SIG is not None and sig == LAST_INV_SIG
    LAST_INV_SIG = sig
    text, LAST_CHAT_ID = format_observation(
        state, LAST_CHAT_ID, inv_unchanged=inv_unchanged, compact=not full)
    return text


def _outcome(result: dict, success_text: str) -> str:
    status = result.get("status", "unbekannt")
    detail = result.get("detail") or ""
    if status == "done":
        return f"{success_text} {detail}".rstrip()
    if status == "interrupted":
        return ("ABGEBROCHEN: Der Spieler funkt dich gerade an. Hoer SOFORT zu "
                "und reagiere auf seinen Funk, bevor du weitermachst.")
    if status == "running":
        dist = result.get("dist_to_target", -1.0)
        return (f"Laeuft noch (Distanz {dist:.0f} m). Mit observe() pruefen "
                f"oder wait() nutzen.")
    return f"Fehlgeschlagen: {detail}"


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
    nichts. Unerreichbare Ziele schlagen nach 45 s ohne Fortschritt fehl."""
    # Schon am Ziel? Dann KEINEN neuen Marsch starten - sonst feuert das Gehirn
    # minutenlang identische move_to auf einen bereits erreichten Punkt (jeder
    # Lauf meldet sofort "Angekommen", das LLM wiederholt endlos). Schwelle 4 m
    # liegt knapp ueber der Mod-Ankunftsschwelle (3 m, BR 2 m).
    snap = BRIDGE.read_state() or {}
    npc = snap.get("npc", {})
    if npc.get("alive") and npc.get("spawned"):
        dx = npc.get("pos_x", 0.0) - float(x)
        dz = npc.get("pos_z", 0.0) - float(z)
        if dx * dx + dz * dz <= 16.0:   # <= 4 m
            d = (dx * dx + dz * dz) ** 0.5
            return (f"Du bist bereits am Ziel (Distanz {d:.0f} m) - kein erneuter "
                    f"Marsch noetig. Mach etwas anderes oder beende den Zug.")
    result = BRIDGE.run("move_to", x=x, z=z, timeout=35, interruptible=True)
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
    needle = classname or item_name or item or name
    result = BRIDGE.run("pickup", text=needle, timeout=60, interruptible=True)
    out = _outcome(result, "Aufgehoben:")
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
    needle = classname or item_name or item or name
    result = BRIDGE.run("eat", text=needle, timeout=30)
    return _outcome(result, "Gegessen:")


@mcp.tool()
def drink(classname: str = "", item_name: str = "", item: str = "",
          name: str = "") -> str:
    """Aus einem Getraenk im Inventar trinken. OHNE Argument das erste; MIT
    classname gezielt dieses (Teilstring reicht). Verschlossene Dosen werden
    vorher geoeffnet. Schlaegt fehl, wenn nichts Passendes da ist."""
    needle = classname or item_name or item or name
    result = BRIDGE.run("drink", text=needle, timeout=30)
    return _outcome(result, "Getrunken:")


@mcp.tool()
def harvest(animal: str = "") -> str:
    """Tierkadaver verwerten (Jagd): laeuft zum naechsten toten Tier im
    Umkreis von 50 m (kind=animal_corpse in der Umgebung), zerlegt es und
    nimmt das Fleisch ins Inventar (rohes Fleisch vor dem Essen am Feuer
    braten!). Braucht ein Schneidwerkzeug (Messer/Machete/Axt) im Inventar.
    Optional animal=Teil des Klassennamens (z.B. "Cervus" fuer Hirsch)."""
    result = BRIDGE.run("harvest", text=animal, timeout=90, interruptible=True)
    return _outcome(result, "Zerlegt:")


@mcp.tool()
def wear(classname: str = "", item_name: str = "", item: str = "",
         name: str = "") -> str:
    """Kleidungsstueck anziehen (gegen Kaelte - die VITALS zeigen, ob du
    frierst). Sucht im Inventar UND am Boden im Umkreis von 10 m, ein
    vorheriges pickup ist nicht noetig. Ist der Koerper-Slot belegt, wird
    automatisch getauscht: das alte Stueck landet am Boden."""
    needle = classname or item_name or item or name
    if not needle:
        return "Bitte classname angeben, z.B. wear(classname=\"BeanieHat\")."
    result = BRIDGE.run("wear", text=needle, timeout=20)
    return _outcome(result, "Angezogen:")


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
    result = tactics.equip_best(BRIDGE, log=lambda m: None)
    if "fehlgeschlagen" in result.lower():
        _EQUIP_FAILS += 1
        _EQUIP_LAST_FAIL = now
    else:
        _EQUIP_FAILS = 0   # Erfolg ODER "keine Waffe im Inventar" -> Zaehler zuruecksetzen
    return result


@mcp.tool()
def equip_melee() -> str:
    """Die beste NAHKAMPFWAFFE (Machete, Axt, Messer ...) in die Hand nehmen -
    auch wenn du eine geladene Schusswaffe haettest. SO kaempfst du, wenn du
    ALLEIN bist und nur ein, zwei Infizierte da sind: Nahkampf ist leise, spart
    Munition und lockt KEINE weitere Horde an (ein Schuss zieht schnell 5-10
    weitere an, die dich allein toeten). Danach engage. Bei Uebermacht (mehrere
    gleichzeitig), gegen Raubtiere (Wolf/Baer) oder wenn dein Squad bei dir ist
    (macht ohnehin Laerm), nimm lieber equip_best (Schusswaffe)."""
    return tactics.equip_melee(BRIDGE, log=lambda m: None)


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
def loot_area(max_items: int = 6) -> str:
    """Sichtbare lohnende Bodenitems in der Umgebung einsammeln (bis zu
    max_items, bewertet nach Nutzen: Waffen-Upgrades, Munition, Medizin,
    Nahrung, Werkzeug; Schrott bleibt liegen). Laeuft selbststaendig hin,
    sammelt ein und ruestet danach die beste Waffe aus. Kann mehrere Minuten
    dauern. Nur in sicherer Lage nutzen."""
    max_items = max(1, min(12, int(max_items)))
    result = tactics.loot_area(BRIDGE, max_items=max_items, log=lambda m: None)
    haul = result["haul"]
    parts = []
    if haul:
        parts.append("Eingesammelt: " + ", ".join(haul))
    else:
        parts.append("Nichts Lohnendes in Sichtweite gefunden.")
    if result["failed"]:
        parts.append("Nicht erreichbar: " + ", ".join(result["failed"]))
    # Aufgehobene Munitionskisten gleich aufmachen, sonst bleibt die Mun drin.
    boxes = [cn for cn in haul if cn.startswith("AmmoBox")]
    for cn in boxes:
        BRIDGE.run("unpack_ammo", text=cn, timeout=10)
    if boxes:
        parts.append(f"Munitionskisten geoeffnet: {len(boxes)}")
    parts.append(tactics.equip_best(BRIDGE, log=lambda m: None))
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
    result = BRIDGE.run("engage", timeout=90)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "kein Gegner" in detail:
        return ("Kein Gegner (Infizierter/Raubtier) in Sicht. NICHT blind engage "
                "wiederholen - erst mit observe pruefen, was in der Naehe ist. "
                "Ist nichts da, geh weiter (move_to/explore_step) oder mach etwas "
                "anderes.")
    return _outcome(result, "Kampf beendet:")


@mcp.tool()
def flee() -> str:
    """150 m vom naechsten Infizierten wegsprinten (oder in Blickrichtung,
    wenn keiner in der Naehe ist)."""
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
# ISU_PLAYER_NAMES: optionale Komma-Liste aller Namen, unter denen der Spieler
# angesprochen werden kann (Funk-Name UND DayZ-Profilname, falls verschieden).
# Default = der Funk-Name (ISU_MIC_NAME). Beispiel: "Player,MeinProfilName".
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
    also seinen Namen nennst (z.B. "<Spielername>, pass auf!"). Funk untereinander
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

    Der menschliche Spieler heisst im Funk oft anders (PLAYER_RADIO_NAME, der
    im Spiel-Menue gesetzte Voice-Name) als im Spiel selbst (sein DayZ-Profilname
    aus GetIdentity). Die Mod vergleicht beim follow EXAKT
    (pb.GetIdentity().GetName() != cmd.text), darum scheitert follow mit dem
    Funk-Namen ("weder Spieler noch Kamerad in 180 m"), waehrend follow mit dem
    Profilnamen klappt. Aufloesungsreihenfolge:
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
    # Meint der Befehl nachweislich den Menschen (sein Funk- oder konfigurierter
    # Profilname)? Dann ueber die zentrale Variable aufloesen statt zu raten.
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
    target = _resolve_player_name(player_name, allow_empty=True)
    result = BRIDGE.run("follow", text=target, timeout=15)
    detail = result.get("detail") or ""
    if result.get("status") != "done" and "weder Spieler noch Kamerad" in detail:
        if not (player_name or "").strip():
            return ("Fehlgeschlagen: kein Spieler/Kamerad in Reichweite und du "
                    "hast KEINEN Namen genannt. Gib einen Namen an, z.B. "
                    "follow(player_name=\"<Profilname>\"), oder geh mit move_to naeher "
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
    result = BRIDGE.run("stop", timeout=15)
    return _outcome(result, "Stehe still.")


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
    liegende Rucksaecke, Kleidung mit Sachen drin, Kisten. In observe
    erkennst du sie am Marker [enthaelt N]. Beispiel:
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
    return tactics.cook_meal(BRIDGE, log=lambda m: None)


@mcp.tool()
def drink_at_well() -> str:
    """Zum naechsten sichtbaren Brunnen laufen (kind=water in observe), dort
    trinken und einen Fluessigkeitsbehaelter auffuellen. Brunnen stehen in
    Doerfern - wenn keiner sichtbar ist, erst in eine Ortschaft gehen."""
    return tactics.water_run(BRIDGE, log=lambda m: None)


@mcp.tool()
def find_item(pattern: str = "", item_name: str = "", item_type: str = "",
              item: str = "", name: str = "") -> str:
    """Pruefen, ob du etwas Bestimmtes hast oder in Sichtweite liegt.
    Beispiel: find_item(pattern="Canister") - Teilstring im Classname.
    Fuer die Suche in der Welt: explore_step wiederholen."""
    needle = pattern or item_name or item_type or item or name
    if not needle:
        return "Bitte pattern angeben, z.B. find_item(pattern=\"Nail\")."
    return tactics.find_item(BRIDGE, needle)


@mcp.tool()
def explore_step() -> str:
    """Ein Such-/Erkundungsschritt: laeuft ~100 m in eine neue Richtung und
    sammelt dort Lohnendes ein. Fuer gezielte Item-Suche mehrfach wiederholen
    und zwischendurch mit find_item pruefen."""
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
    needle = classname or item_name or item or name
    result = BRIDGE.run("drop", text=needle, timeout=30)
    return _outcome(result, "Abgelegt:")


@mcp.tool()
def give_to(player_name: str = "", classname: str = "", item_name: str = "",
            item: str = "", name: str = "", player: str = "") -> str:
    """Einem anderen Survivor (KI-Kollegen) einen Gegenstand DIREKT ins
    Inventar geben - zuverlaessiger als drop+pickup, das Item geht nicht am
    Boden verloren. Beide muessen nah beieinander sein (max 12 m).
    Beispiel: give_to(player_name="Konrad", classname="AmmoBox_545x39_20Rnd")."""
    target = _resolve_player_name(player_name or player)
    needle = classname or item_name or item or name
    if not target or not needle:
        return "Bitte player_name UND classname angeben."
    result = BRIDGE.run("hand_over", text=f"{target}|{needle}", timeout=15)
    return _outcome(result, "Uebergeben:")


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
    result = BRIDGE.run("unstick", timeout=15)
    return _outcome(result, "Befreit:")


@mcp.tool()
def wait(seconds: int = 15) -> str:
    """Abwarten und beobachten (1-60 Sekunden). Danach kommt automatisch eine
    frische Lagebeschreibung."""
    seconds = max(1, min(60, int(seconds)))
    time.sleep(seconds)
    return f"{seconds}s gewartet.\n\n" + _observe_text()


if __name__ == "__main__":
    mcp.run()
