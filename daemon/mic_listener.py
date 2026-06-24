#!/usr/bin/env python3
"""IsuSurvivor Mikrofon-Listener — Viktor hoert dich ueber das lokale Mikro.

Hintergrund: Discord erzwingt seit Maerz 2026 DAVE-E2EE auf Voice-Kanaelen,
Bot-Bibliotheken koennen Nutzer-Audio derzeit nicht entschluesseln (Senden
geht weiter). Dieser Listener nimmt stattdessen das lokale Mikrofon,
schneidet Aeusserungen an Sprechpausen, transkribiert per ElevenLabs Scribe
und schreibt in dieselbe Funk-Inbox wie der Discord-Empfang.

Das Mikrofon wird beim Start AUTOMATISCH gesucht: Default-Geraet zuerst,
dann alle echten Eingaenge, jeweils kurz angetestet. Endpunkte, die nur
digitale Stille liefern (gemutetes Headset, totes G-HUB-Routing), werden
uebersprungen, virtuelle Kabel (VB-Cable, Stereomix...) ignoriert.

Wird von run_agent.py automatisch mitgestartet (abschaltbar mit --no-mic).
Env: ISU_MIC_NAME (Absendername, Default "Player"),
     ISU_MIC_DEVICE (Index oder Namens-Teil, erzwingt ein Geraet),
     ISU_MIC_THRESHOLD (RMS-Schwelle, Default automatisch kalibriert).

Standalone-Test:  python mic_listener.py --test 10
"""

import argparse
import io
import json
import os
import sys
import time
import wave

import numpy as np
import requests
import sounddevice as sd

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
INBOX = os.path.join(REPO_DIR, "agent_home", "voice_inbox.jsonl")

ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
MIC_NAME = os.environ.get("ISU_MIC_NAME", "Player")
API = "https://api.elevenlabs.io/v1"

GAP_SECONDS = 1.0               # Sprechpause beendet die Aeusserung
MIN_SECONDS = 0.4
MAX_SECONDS = 45.0
PREROLL_SECONDS = 0.3           # Vorlauf, damit Wortanfaenge nicht fehlen

# Virtuelle/Loopback-Endpunkte, die niemals das Spieler-Mikro sind
VIRTUAL_HINTS = ("cable", "virtual", "vdvad", "stereomix", "stereo mix",
                 "soundmapper", "sound mapper", "loopback", "voicemeeter",
                 "voicemod", "primaer", "primary", "prim")


def log(msg: str):
    print(f"[mic] {msg}", flush=True)


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))


def stt_transcribe(pcm16: bytes, rate: int) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    buf.seek(0)
    r = requests.post(
        f"{API}/speech-to-text",
        headers={"xi-api-key": ELEVEN_KEY},
        files={"file": ("mic.wav", buf, "audio/wav")},
        data={"model_id": "scribe_v1"},
        timeout=120,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def inbox_append(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(INBOX), exist_ok=True)
        with open(INBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _probe_device(idx: int) -> tuple[float, int]:
    """(Rauschteppich, nutzbare Rate) eines Endpunkts; (-1, 0) wenn unbrauchbar.

    Testet BEIDE Raten und nimmt die mit echtem Signal. Manche Headsets (z.B.
    PRO X 2 LIGHTSPEED) lassen sich bei 16 kHz oeffnen, liefern dort aber nur
    Stille (Teppich 0) und funktionieren erst bei 48 kHz - frueher wurde 16 kHz
    zuerst genommen und der Endpunkt galt faelschlich als stumm."""
    best_floor, best_rate = -1.0, 0
    for rate in (48000, 16000):
        try:
            rec = sd.rec(int(0.6 * rate), samplerate=rate, channels=1,
                         dtype="int16", device=idx)
            sd.wait()
            floor = rms(rec.reshape(-1))
        except Exception:
            continue
        if floor >= 3.0:          # echtes Signal -> diese Rate nehmen
            return floor, rate
        if best_rate == 0 or floor > best_floor:
            best_floor, best_rate = floor, rate
    return best_floor, best_rate


def pick_input_device(logger=log) -> tuple[int, int, float]:
    """Lebendes Mikrofon finden: (device_index, capture_rate, rauschteppich).

    Reihenfolge: ISU_MIC_DEVICE (wenn gesetzt) > Windows-Default > weitere
    echte Eingaenge. Der erste Endpunkt mit echtem Signal (Teppich >= 3)
    gewinnt - ein gemutetes Headset liefert ~0 und wird uebersprungen.
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    # Hoechste Prioritaet: per Mikrofon-Check gespeicherte Wahl (arena/mic.json)
    mic_cfg_path = os.path.join(REPO_DIR, "arena", "mic.json")
    try:
        with open(mic_cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = {}
    if cfg and not cfg.get("auto"):
        idx = -1
        # Geraete-Indizes koennen sich verschieben: erst per Name suchen
        saved_name = cfg.get("device_name", "")
        if saved_name:
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and dev["name"] == saved_name:
                    idx = i
                    break
        if idx < 0:
            idx = int(cfg.get("device_index", -1))
        if 0 <= idx < len(devices):
            floor, rate = _probe_device(idx)
            if rate:
                logger(f"Mikrofon (per Mikrofon-Check gewaehlt): [{idx}] "
                       f"{devices[idx]['name']} @{rate} Hz, Teppich {floor:.0f}")
                if floor < 1.0:
                    logger("!!! Das gewaehlte Mikro liefert gerade STILLE - "
                           "Mikrofon-Check erneut ausfuehren (start_all -> j).")
                return idx, rate, floor
        logger("Gespeicherte Mikrofon-Wahl nicht nutzbar - suche automatisch.")

    env_dev = os.environ.get("ISU_MIC_DEVICE", "")
    if env_dev:
        idx = -1
        try:
            idx = int(env_dev)
        except ValueError:
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and env_dev.lower() in dev["name"].lower():
                    idx = i
                    break
        if idx >= 0:
            floor, rate = _probe_device(idx)
            if rate:
                logger(f"Mikrofon (per ISU_MIC_DEVICE erzwungen): [{idx}] "
                       f"{devices[idx]['name']} @{rate} Hz, Teppich {floor:.0f}")
                return idx, rate, floor
        logger(f"ISU_MIC_DEVICE={env_dev} nicht nutzbar - suche automatisch.")

    candidates = []
    default_idx = sd.default.device[0]
    if default_idx is not None and default_idx >= 0:
        candidates.append(default_idx)
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] < 1 or i == default_idx:
            continue
        if any(h in dev["name"].lower() for h in VIRTUAL_HINTS):
            continue
        api = hostapis[dev["hostapi"]]["name"]
        # pro physischem Geraet reicht der robuste MME-Endpunkt
        if "MME" not in api:
            continue
        candidates.append(i)

    best = (-1, 0, -1.0)
    for idx in candidates:
        floor, rate = _probe_device(idx)
        mark = "DEFAULT, " if idx == default_idx else ""
        if rate == 0:
            logger(f"  Probe [{idx:2}] {devices[idx]['name'][:42]:42} ({mark}unbrauchbar)")
            continue
        logger(f"  Probe [{idx:2}] {devices[idx]['name'][:42]:42} ({mark}Teppich {floor:.0f})")
        if floor >= 3.0:
            logger(f"GEWAEHLT: [{idx}] {devices[idx]['name']} @{rate} Hz - echtes Signal.")
            if idx != default_idx:
                logger("Hinweis: Das ist NICHT das Default-Mikro. Wenn dein Headset "
                       "gemeint war: Mute-Taste am Headset / G HUB pruefen.")
            return idx, rate, floor
        if floor > best[2]:
            best = (idx, rate, floor)

    if best[0] >= 0:
        logger("!!! ALLE Endpunkte liefern Stille (Headset-Mute? G HUB?). "
               "Nutze den besten trotzdem.")
        return best
    raise RuntimeError("Kein nutzbares Eingabegeraet gefunden")


def is_noise_transcript(text: str) -> bool:
    """Scribe beschreibt Nicht-Sprache in Klammern: '(rock music)',
    '(Vogelgezwitscher)' - solche Junk-Transkripte nicht zustellen."""
    stripped = text.strip()
    if not stripped:
        return True
    # alle Klammergruppen entfernen; bleibt nichts Sinnvolles -> Geraeusch
    import re
    rest = re.sub(r"\([^)]*\)", "", stripped)
    rest = re.sub(r"[\s.,!?…-]+", "", rest)
    return rest == ""


def watch_parent(parent_pid: int):
    """Beendet den Prozess, wenn der Eltern-Runner stirbt (kein Zombie-
    Listener, der eine Inbox fuettert, die niemand liest)."""
    import ctypes
    import threading

    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        return

    def waiter():
        ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        log(f"Eltern-Prozess {parent_pid} beendet - Listener stoppt.")
        os._exit(0)

    threading.Thread(target=waiter, daemon=True).start()


# Push-to-Talk: global abgefragte Taste (funktioniert auch, wenn DayZ den
# Fokus hat). Default ist PTT mit CAPSLOCK - dieselbe Taste wie DayZs
# eingebauter Funk (VON), ein Druck bedient also Spieler UND Agenten.
# Ein dauerhaft offenes Raummikro hoert sonst Familie, Fernseher und
# Selbstgespraeche mit.
VK_KEYS = {"F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
           "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
           "F11": 0x7A, "F12": 0x7B, "CAPSLOCK": 0x14,
           "MAUS4": 0x05, "MAUS5": 0x06}


def load_mic_mode() -> tuple[str, int, str]:
    """(mode, vk_code, key_name) aus arena/mic.json bzw. Env.
    mode: 'ptt' (Default) oder 'open'."""
    cfg = {}
    try:
        with open(os.path.join(REPO_DIR, "arena", "mic.json"), "r",
                  encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    mode = os.environ.get("ISU_MIC_MODE", "") or cfg.get("mode", "ptt")
    key = (os.environ.get("ISU_MIC_PTT_KEY", "") or
           cfg.get("ptt_key", "CAPSLOCK")).upper()
    vk = VK_KEYS.get(key, VK_KEYS["CAPSLOCK"])
    if mode not in ("ptt", "open"):
        mode = "ptt"
    return mode, vk, key


def ptt_down(vk: int) -> bool:
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", type=int, default=0,
                        help="nach N Sekunden beenden (Diagnose)")
    parser.add_argument("--inbox", default="",
                        help="Ziel-Inbox (Default: agent_home/voice_inbox.jsonl)")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="beendet sich, wenn dieser Prozess stirbt")
    args = parser.parse_args()

    global INBOX
    if args.inbox:
        INBOX = os.path.abspath(args.inbox)
    if args.parent_pid:
        watch_parent(args.parent_pid)

    if not ELEVEN_KEY:
        log("ELEVENLABS_API_KEY fehlt - beende.")
        return 1

    device, rate, floor = pick_input_device()
    block = int(rate * 0.03)
    preroll_max = max(1, int(PREROLL_SECONDS / 0.03))

    env_thr = os.environ.get("ISU_MIC_THRESHOLD", "")
    threshold = float(env_thr) if env_thr else max(250.0, floor * 6.0)

    mode, ptt_vk, ptt_name = load_mic_mode()
    if mode == "ptt":
        log(f"PUSH-TO-TALK aktiv: [{ptt_name}] gedrueckt halten zum Sprechen "
            f"(funktioniert auch im Spiel).")
    else:
        log(f"IMMER-OFFEN-Modus: Schwelle {threshold:.0f} (Teppich {floor:.0f}). "
            f"Achtung: hoert den ganzen Raum!")

    preroll: list[bytes] = []
    chunks: list[bytes] = []
    voiced = False
    last_voice = 0.0
    started = time.monotonic()

    # Pegel-Anzeige fuer die ersten 30 s: zeigt, ob Sprache ueberhaupt ankommt
    level_window_max = 0.0
    next_level_report = started + 5.0

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
            log("(leeres Transkript)")
            return
        if is_noise_transcript(text):
            log(f"(Geraeusch ignoriert: {text[:50]})")
            return
        log(f"FUNK {MIC_NAME}: {text}")
        inbox_append({"user": MIC_NAME, "text": text, "t": time.time()})

    with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                        blocksize=block, device=device) as stream:
        while True:
            if args.test and time.monotonic() - started > args.test:
                log("Testzeit abgelaufen - beende.")
                return 0

            data, _ = stream.read(block)
            data = data.reshape(-1)
            level = rms(data)
            now = time.monotonic()

            if mode == "open" and now - started < 30.0:
                level_window_max = max(level_window_max, level)
                if now >= next_level_report:
                    log(f"Pegel (max. der letzten 5 s): {level_window_max:.0f} "
                        f"(Schwelle: {threshold:.0f})")
                    level_window_max = 0.0
                    next_level_report = now + 5.0

            if mode == "ptt":
                hot = ptt_down(ptt_vk)
                tail = 0.35  # kurzer Nachlauf nach Loslassen
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
    sys.exit(main())
