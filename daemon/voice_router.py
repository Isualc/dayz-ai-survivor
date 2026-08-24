#!/usr/bin/env python3
"""IsuSurvivor Voice-Router (Arena) — EIN Mikro, mehrere Agenten, kein Chaos.

Nimmt das lokale Mikrofon (Auto-Auswahl wie mic_listener), transkribiert per
ElevenLabs Scribe und stellt das Transkript gezielt zu:

  1. NAMENSANSPRACHE: Kommt ein Agentenname im Satz vor ("Igor, komm her"),
     bekommen genau die genannten Agenten die Nachricht.
  2. NAEHE: Ohne Namen bekommt sie der Agent, der dem Spieler im Spiel am
     naechsten steht (aus state_<id>.json: nearby-Spieler-Distanz).
  3. Sieht kein Agent einen Spieler in der Naehe: Hinweis im Log, verworfen.

Start (durch start_arena): python voice_router.py --selected viktor,birgit
"""

import argparse
import json
import os
import sys
import time

import sounddevice as sd

from mic_listener import (rms, stt_transcribe, pick_input_device,
                          is_noise_transcript, load_mic_mode, ptt_down,
                          GAP_SECONDS, MIN_SECONDS, MAX_SECONDS,
                          PREROLL_SECONDS)

try:
    from players_registry import resolve as resolve_player
except ImportError:                        # Registry (noch) nicht vorhanden
    resolve_player = None

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
PROFILE_DIR = os.path.join(_SERVER_DIR, "profiles", "IsuSurvivor")
ROSTER_FILE = os.path.join(REPO_DIR, "arena", "agents.json")
# Effektive (im Spiel-Menue frei gewaehlte) Namen - vom Supervisor geschrieben
ACTIVE_ROSTER_FILE = os.path.join(REPO_DIR, "arena", "active_roster.json")

ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
# MIC_NAME ist der Funk-Absendername des EINEN lokalen Mikrofons (Single-Host,
# wie bisher). Sprecher-Stempelung laeuft trotzdem ueber die players_registry:
# steht MIC_NAME (oder sein Alias) dort drin, wird der kanonische Funk-Name
# aus der Registry verwendet (z.B. Tippfehler/Gross-Kleinschreibung in der
# Env-Variable werden auf die registrierte Schreibweise normalisiert). Ist
# der Name unbekannt, bleibt MIC_NAME unveraendert - kein Verhaltensbruch.
MIC_NAME = os.environ.get("ISU_MIC_NAME", "Player")


def resolve_speaker_name(raw_name: str) -> str:
    """Normalisiert den Funk-Absendername ueber die players_registry.

    MEHRSPIELER-VORBEREITUNG (noch nicht implementiert): Dieser Router nutzt
    EIN Mikrofon fuer alle Sprecher (Single-Host-Fluss, mic_listener waehlt
    EIN Eingabegeraet in main()). Sobald mehrere echte Menschen mit eigenen
    Mikros mitspielen sollen, braucht players.json pro Eintrag ein Feld
    "mic_name" (welches Audiogeraet zu welchem Spieler gehoert) und der
    vorgesehene Weg ist EIN eigener mic_listener/voice_router-Prozess PRO
    Eintrag (je ein --selected-Aufruf mit eigenem ISU_MIC_DEVICE/ISU_MIC_NAME
    in der Prozess-Umgebung), nicht ein einzelner Prozess, der mehrere
    Geraete gleichzeitig abhoert. Diese Funktion bliebe dabei unveraendert -
    jeder Prozess stempelt weiterhin nur seinen eigenen MIC_NAME. Bis dahin
    wird hier NICHTS am mic_listener-Start geaendert (Auflage: Single-Host-
    Fluss darf sich nicht verschlechtern)."""
    if resolve_player is None:
        return raw_name
    entry = resolve_player(raw_name)
    if entry and entry.get("funk"):
        return entry["funk"]
    return raw_name
# Datei-Log: der Router laeuft in einem eigenen Konsolenfenster - stuerzt er
# ab, ist das Fenster weg und mit ihm die Spur. Hierhin schreibt er mit.
LOG_FILE = os.path.join(REPO_DIR, "agent_home", "journal", "voice_router.log")


def log(msg: str):
    line = f"[router] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def agent_home(agent_id: str) -> str:
    import agent_paths
    return agent_paths.agent_home_dir(agent_id)


def inbox_append(agent_id: str, entry: dict) -> None:
    path = os.path.join(agent_home(agent_id), "voice_inbox.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


HEAR_RANGE = 60.0  # Hoerweite wie der Spiel-Chat


def agents_in_earshot(agent_ids: list[str]) -> list[str]:
    """Alle Agenten, die einen Spieler in Hoerweite haben (State-Dateien).
    Funk ohne Namensnennung geht an ALLE davon - wie ein Zuruf im Spiel."""
    out = []
    for agent_id in agent_ids:
        state_file = os.path.join(PROFILE_DIR, f"state_{agent_id}.json")
        try:
            with open(state_file, "r", encoding="utf-8", errors="replace") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for e in state.get("nearby", []):
            if e.get("kind") == "player" and e.get("distance", 9999) <= HEAR_RANGE:
                out.append(agent_id)
                break
    return out


def load_name_aliases(agent_ids: list[str]) -> dict[str, str]:
    """Anzeigename (klein) -> Slot-Id. Namen sind im Spiel-Menue frei
    waehlbar (z.B. birgit-Slot heisst 'Angie'); der Supervisor publiziert
    sie in active_roster.json. Slot-Ids matchen immer mit."""
    aliases = {a.lower(): a for a in agent_ids}
    for path in (ROSTER_FILE, ACTIVE_ROSTER_FILE):  # active zuletzt = gewinnt
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f).get("agents", [])
        except (OSError, json.JSONDecodeError):
            continue
        for a in entries:
            aid = (a.get("id") or "").strip().lower()
            name = (a.get("name") or "").strip().lower()
            if aid in agent_ids and name:
                aliases[name] = aid
    return aliases


def route(agent_ids: list[str], aliases: dict[str, str],
          text: str) -> tuple[list[str], str]:
    """(Zielagenten, Begruendung) - Begruendung landet im Debug-Log.

    Beginnt der Satz mit einer Anrede ("Igor, ..."), bekommt ihn NUR der
    Genannte (privater Befehl). OHNE direkte Anrede hoeren ALLE deine NPCs
    zu - mapweit, egal wie weit weg (Spieler-Wunsch). Hoerweite ist KEINE
    Bedingung; so wird kein Funk verworfen, nur weil niemand nahe steht."""
    text_l = text.lower()
    lead = text_l.strip()[:24]
    direct = sorted({aid for alias, aid in aliases.items() if lead.startswith(alias)})
    if direct:
        return direct, "direkte Anrede (privat)"
    # MAPWEIT: ohne direkte Anrede an ALLE aktiven NPCs, ueber die ganze Karte.
    return sorted(agent_ids), "mapweit an alle NPCs"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected", required=True,
                        help="Kommagetrennte Agenten-Ids, z.B. viktor,birgit")
    args = parser.parse_args()
    agent_ids = [a.strip() for a in args.selected.split(",") if a.strip()]

    if not ELEVEN_KEY:
        log("ELEVENLABS_API_KEY fehlt - beende.")
        return 1

    aliases = load_name_aliases(agent_ids)
    shown = sorted({n.capitalize() for n in aliases})
    log(f"Aktive Agenten/Rufnamen: {', '.join(shown)}")
    log("ANSPRACHE: Namen im Satz nennen ('Igor, komm her'). OHNE Namen")
    log("antwortet der Agent, der dir am naechsten steht.")

    # Sprecher-Stempelung ueber die players_registry (Schnittstelle 5): der
    # rohe MIC_NAME (ISU_MIC_NAME) wird einmalig auf den registrierten
    # Funk-Namen normalisiert (z.B. andere Gross-/Kleinschreibung oder ein
    # in players.json hinterlegter Alias). Unbekannter Name -> unveraendert.
    speaker_name = resolve_speaker_name(MIC_NAME)
    if speaker_name != MIC_NAME:
        log(f"Sprecher '{MIC_NAME}' -> Registry-Funkname '{speaker_name}'.")

    device, rate, floor = pick_input_device(logger=log)
    block = int(rate * 0.03)
    preroll_max = max(1, int(PREROLL_SECONDS / 0.03))

    env_thr = os.environ.get("ISU_MIC_THRESHOLD", "")
    threshold = float(env_thr) if env_thr else max(250.0, floor * 6.0)

    mode, ptt_vk, ptt_name = load_mic_mode()
    if mode == "ptt":
        log(f"PUSH-TO-TALK aktiv: [{ptt_name}] gedrueckt halten zum Sprechen.")
    else:
        log(f"IMMER-OFFEN-Modus: Schwelle {threshold:.0f} (Teppich {floor:.0f}).")

    preroll: list[bytes] = []
    chunks: list[bytes] = []
    voiced = False
    last_voice = 0.0

    def flush():
        nonlocal chunks, voiced
        pcm = b"".join(chunks)
        chunks = []
        voiced = False
        seconds = len(pcm) / (rate * 2)
        if seconds < MIN_SECONDS:
            return
        log(f"Aeusserung ({seconds:.1f}s) -> STT...")
        try:
            text = stt_transcribe(pcm, rate)
        except Exception as e:
            log(f"STT-Fehler: {e}")
            return
        if not text:
            return
        if is_noise_transcript(text):
            log(f"(Geraeusch ignoriert: {text[:50]})")
            return
        targets, reason = route(agent_ids, aliases, text)
        if not targets:
            log(f"VERWORFEN (kein Agent in Spielernaehe, kein Name genannt): {text}")
            return
        log(f"FUNK an {', '.join(targets).upper()} ({reason}): {text}")
        for target in targets:
            inbox_append(target, {"user": speaker_name, "text": text, "t": time.time()})

    with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                        blocksize=block, device=device) as stream:
        while True:
            data, _ = stream.read(block)
            data = data.reshape(-1)
            level = rms(data)
            now = time.monotonic()

            if mode == "ptt":
                hot = ptt_down(ptt_vk)
                tail = 0.35
            else:
                hot = level >= threshold
                tail = GAP_SECONDS

            if hot:
                if not voiced:
                    voiced = True
                    chunks = list(preroll)
                chunks.append(data.tobytes())
                last_voice = now
                if len(chunks) * 0.03 > MAX_SECONDS:
                    flush()
            else:
                if voiced:
                    chunks.append(data.tobytes())
                    if now - last_voice > tail:
                        flush()
                else:
                    preroll.append(data.tobytes())
                    if len(preroll) > preroll_max:
                        preroll.pop(0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        log(f"Router abgestuerzt: {exc!r}")
        log(traceback.format_exc())
        sys.exit(1)
