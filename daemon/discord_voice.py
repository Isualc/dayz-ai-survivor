#!/usr/bin/env python3
"""IsuSurvivor Discord-Voice — Viktor spricht UND hoert im Discord-Sprachkanal.

Senden:    say/say_voice schreiben in voice_outbox.jsonl -> Bot spielt ab
           (Katalog-Oggs direkt, freie Texte per ElevenLabs-TTS).
Hoeren:    Bot empfaengt Audio der Nutzer (discord-ext-voice-recv), schneidet
           Aeusserungen an Sprechpausen, transkribiert sie per ElevenLabs
           Scribe und schreibt sie in voice_inbox.jsonl -> run_agent weckt
           das Gehirn mit "FUNK von <Name>: ...".

Setup: docs/discord_bot_setup.md. Braucht DISCORD_BOT_TOKEN; Hoeren und
Live-TTS brauchen zusaetzlich ELEVENLABS_API_KEY.
"""

import argparse
import asyncio
import io
import json
import os
import sys
import tempfile
import time
import wave

import discord
from discord.ext import voice_recv
import requests

try:
    from tts_normalize import normalize_for_tts
except ImportError:  # als Skript ueber Pfad gestartet -> daemon-Dir nachreichen
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tts_normalize import normalize_for_tts

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
PHRASES_FILE = os.path.join(REPO_DIR, "voice", "phrases.json")
API = "https://api.elevenlabs.io/v1"

# Multi-Bot-Betrieb: pro Agent ein eigener Discord-Bot in einem EIGENEN
# Sprachkanal (eigener Token, eigene Outbox/Inbox, eigene Stimme). Ohne
# Argumente verhaelt sich das Skript wie bisher: EIN Bot (DISCORD_BOT_TOKEN)
# liest die gemeinsame Outbox und sitzt im Kanal "DayZ".
_p = argparse.ArgumentParser()
_p.add_argument("--label", default="bot", help="Name im Log (z.B. Agenten-Id)")
_p.add_argument("--token-env", default="DISCORD_BOT_TOKEN",
                help="Umgebungsvariable mit dem Bot-Token dieses Bots")
_p.add_argument("--channel", default=os.environ.get("ISU_DISCORD_CHANNEL", "DayZ"),
                help="Discord-Sprachkanal, dem der Bot beitritt")
_p.add_argument("--voice", default="",
                help="ElevenLabs-Stimme dieses Bots (sonst phrases.json)")
_p.add_argument("--outbox", action="append", default=None,
                help="voice_outbox.jsonl (mehrfach erlaubt fuer Sammel-Bot)")
_p.add_argument("--inbox", action="append", default=None,
                help="voice_inbox.jsonl fuer transkribierten Funk (mehrfach "
                     "erlaubt - Sammel-Bot stellt an alle zu)")
_p.add_argument("--no-listen", action="store_true",
                help="Nur sprechen, nicht hoeren - setzen, wenn der lokale "
                     "Mikro-Router laeuft, sonst kommt jeder Funkspruch "
                     "DOPPELT an (Router + Bot-Ohr)")
ARGS = _p.parse_args()

OUTBOXES = ARGS.outbox or [os.path.join(REPO_DIR, "agent_home", "voice_outbox.jsonl")]
INBOXES = ARGS.inbox or [os.path.join(REPO_DIR, "agent_home", "voice_inbox.jsonl")]
TOKEN = os.environ.get(ARGS.token_env, "")
CHANNEL_NAME = ARGS.channel
BOT_VOICE = ARGS.voice
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

# Offsets SOFORT erfassen (nicht erst nach dem Discord-Login): alles, was ab
# Prozessstart in die (vom Starter frisch geleerte) Outbox faellt, wird
# gesprochen - nur Eintraege von davor gelten als Altlast.
_INIT_OFFSETS: dict = {}
for _box in OUTBOXES:
    try:
        _INIT_OFFSETS[_box] = os.path.getsize(_box)
    except OSError:
        _INIT_OFFSETS[_box] = 0


def _set_discord_flags(active: bool) -> None:
    """discord_active.flag neben jede Outbox legen/entfernen. dayz_mcp.py liest
    sie: ist sie da, sprechen die NPCs nur per Voice (kein doppelter In-Game-Chat)."""
    for _b in OUTBOXES:
        _flag = os.path.join(os.path.dirname(_b), "discord_active.flag")
        try:
            if active:
                with open(_flag, "w", encoding="utf-8") as _f:
                    _f.write("1")
            elif os.path.exists(_flag):
                os.remove(_flag)
        except OSError:
            pass


# Aeusserungs-Erkennung
GAP_SECONDS = 1.1        # Sprechpause, die eine Aeusserung beendet
MIN_SECONDS = 0.5        # kuerzere Schnipsel verwerfen (Atmer, Klicks)
MAX_SECONDS = 45.0       # Sicherheitsdeckel
PCM_BYTES_PER_SECOND = 48000 * 2 * 2  # 48 kHz, stereo, 16 bit


def log(msg: str):
    print(f"[discord-voice:{ARGS.label}] {msg}", flush=True)


def resolve_voice() -> tuple[str, str]:
    with open(PHRASES_FILE, "r", encoding="utf-8") as f:
        catalog = json.load(f)
    name = BOT_VOICE or catalog.get("voice_name", "Daniel")
    model = catalog.get("model_id", "eleven_multilingual_v2")
    r = requests.get(f"{API}/voices", headers={"xi-api-key": ELEVEN_KEY}, timeout=30)
    r.raise_for_status()
    voices = r.json().get("voices", [])
    if not voices:
        raise RuntimeError("Keine Stimmen im ElevenLabs-Konto (Voice Library leer)")

    log("Verfuegbare Stimmen: " + ", ".join(v["name"] for v in voices[:12]))

    # Teilstring-Match: ElevenLabs-Premades heissen "Callum - Husky Trickster",
    # in phrases.json reicht "Callum"
    for v in voices:
        if name.lower() in v["name"].lower():
            return v["voice_id"], model

    # Fallback: erste Stimme des Kontos nutzen statt stumm zu bleiben
    log(f"Stimme '{name}' nicht im Konto - nutze Fallback '{voices[0]['name']}'. "
        f"(voice_name in voice/phrases.json anpassen!)")
    return voices[0]["voice_id"], model


def tts_to_file(voice_id: str, model_id: str, text: str) -> str:
    # Zahlen sprechbar machen (Koordinaten/Kaliber), sonst Kauderwelsch
    text = normalize_for_tts(text)
    r = requests.post(
        f"{API}/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": model_id,
              "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}},
        timeout=120,
    )
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="isu_tts_")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return path


def stt_transcribe(pcm: bytes) -> str:
    """PCM (48 kHz stereo s16) -> Text via ElevenLabs Scribe."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm)
    buf.seek(0)

    r = requests.post(
        f"{API}/speech-to-text",
        headers={"xi-api-key": ELEVEN_KEY},
        files={"file": ("audio.wav", buf, "audio/wav")},
        data={"model_id": "scribe_v1"},
        timeout=120,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def inbox_append(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    for box in INBOXES:
        try:
            os.makedirs(os.path.dirname(box), exist_ok=True)
            with open(box, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


class ViktorVoice(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.vc: voice_recv.VoiceRecvClient | None = None
        self.offsets: dict[str, int] = dict(_INIT_OFFSETS)
        self.voice_id = ""
        self.model_id = ""
        # Arena: pro Agent eine eigene Stimme (Name -> voice_id, lazy aufgeloest)
        self.voice_cache: dict[str, str] = {}
        # user_id -> {"name", "chunks": [bytes], "last": float}
        self.buffers: dict[int, dict] = {}

    # ------------------------------------------------------------- Empfang

    def on_voice_packet(self, user, data):
        """Callback aus dem Voice-Empfangs-Thread (BasicSink)."""
        if user is None or ELEVEN_KEY == "":
            return
        # NIE Bot-Audio transkribieren: sonst hoeren sich die Agenten-Bots
        # gegenseitig (TTS-Echo-Schleife) und jede Aussage kommt doppelt an
        if getattr(user, "bot", False):
            return
        buf = self.buffers.get(user.id)
        if buf is None:
            buf = {"name": user.display_name, "chunks": [], "last": 0.0}
            self.buffers[user.id] = buf
            log(f"Empfange erstmals Audio von {user.display_name}.")
        buf["chunks"].append(data.pcm)
        buf["last"] = time.monotonic()

    async def utterance_flusher(self):
        while not self.is_closed():
            await asyncio.sleep(0.4)
            now = time.monotonic()
            for user_id, buf in list(self.buffers.items()):
                if not buf["chunks"] or now - buf["last"] < GAP_SECONDS:
                    continue
                pcm = b"".join(buf["chunks"])
                buf["chunks"] = []

                seconds = len(pcm) / PCM_BYTES_PER_SECOND
                if seconds < MIN_SECONDS:
                    continue
                if seconds > MAX_SECONDS:
                    pcm = pcm[: int(MAX_SECONDS * PCM_BYTES_PER_SECOND)]

                name = buf["name"]
                log(f"Aeusserung von {name} ({seconds:.1f}s) -> STT...")
                self.loop.create_task(self.transcribe_and_deliver(name, pcm))

    async def transcribe_and_deliver(self, name: str, pcm: bytes):
        try:
            text = await asyncio.to_thread(stt_transcribe, pcm)
        except Exception as e:
            log(f"STT-Fehler: {e}")
            return
        if not text:
            return
        log(f"FUNK {name}: {text}")
        inbox_append({"user": name, "text": text, "t": time.time()})

    # -------------------------------------------------------------- Senden

    async def on_ready(self):
        log(f"Eingeloggt als {self.user}")

        channel = self.find_voice_channel()

        if not channel:
            log(f"FEHLER: Sprachkanal '{CHANNEL_NAME}' auf keinem Server gefunden.")
            await self.close()
            return

        # Voice-Connect mit Wiederholung: Wenn mehrere Bots gleichzeitig
        # starten, scheitert der Beitritt gern transient - frueher blieb
        # der Bot dann still am Leben und sprach nie wieder
        for attempt in range(4):
            try:
                self.vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
                break
            except Exception as e:
                log(f"Voice-Connect fehlgeschlagen (Versuch {attempt + 1}/4): {e}")
                await asyncio.sleep(5)
        if not self.vc:
            log("FEHLER: Voice-Verbindung dauerhaft fehlgeschlagen - beende.")
            await self.close()
            return
        log(f"Im Sprachkanal: {channel.guild.name} / {channel.name}")
        # Ab jetzt sprechen die NPCs per Voice -> dayz_mcp soll den In-Game-Chat
        # nicht mehr zusaetzlich befuellen (sonst erscheint jede Zeile doppelt).
        _set_discord_flags(True)

        if ELEVEN_KEY:
            await self.ensure_voice()
            if ARGS.no_listen:
                log("Hoeren AUS (--no-listen): der Mikro-Router uebernimmt.")
            else:
                self.vc.listen(voice_recv.BasicSink(self.on_voice_packet))
                log("Hoere zu (STT aktiv).")
        else:
            log("Kein ELEVENLABS_API_KEY - weder Live-TTS noch Hoeren.")

        self.loop.create_task(self.poll_outbox())
        self.loop.create_task(self.utterance_flusher())

    async def poll_outbox(self):
        while not self.is_closed():
            await asyncio.sleep(1.0)
            for box in OUTBOXES:
                try:
                    entries = self.read_new(box)
                except FileNotFoundError:
                    continue
                for entry in entries:
                    # Ein kaputter Eintrag (ffmpeg-Probe, Disconnect mitten
                    # im Sprechen) darf die Schleife nicht dauerhaft killen
                    try:
                        await self.speak(entry)
                    except Exception as e:
                        log(f"speak-Fehler ({e}) - Eintrag uebersprungen.")

    def read_new(self, box: str) -> list[dict]:
        size = os.path.getsize(box)
        offset = self.offsets.get(box, 0)
        if size < offset:
            offset = 0
        if size == offset:
            return []
        with open(box, "r", encoding="utf-8") as f:
            f.seek(offset)
            chunk = f.read()
            self.offsets[box] = f.tell()
        entries = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries

    async def ensure_voice(self) -> bool:
        """ElevenLabs-Stimme aufloesen, mit Wiederholung: vier Bots fragen
        beim Arena-Start gleichzeitig /voices ab, das kann 429 geben -
        frueher war TTS dann fuer die ganze Session stumm."""
        if self.voice_id:
            return True
        if not ELEVEN_KEY:
            return False
        for attempt in range(3):
            try:
                self.voice_id, self.model_id = await asyncio.to_thread(resolve_voice)
                log("ElevenLabs-TTS aktiv.")
                return True
            except Exception as e:
                log(f"Stimmen-Aufloesung fehlgeschlagen (Versuch {attempt + 1}/3): {e}")
                await asyncio.sleep(4 + attempt * 4)
        log("TTS vorerst aus - naechster Sprech-Eintrag versucht es erneut.")
        return False

    async def resolve_entry_voice(self, voice_name: str) -> str:
        """Stimme fuer einen Outbox-Eintrag (Arena: pro Agent verschieden)."""
        if not voice_name:
            return self.voice_id
        if voice_name in self.voice_cache:
            return self.voice_cache[voice_name]
        try:
            def lookup():
                r = requests.get(f"{API}/voices",
                                 headers={"xi-api-key": ELEVEN_KEY}, timeout=30)
                r.raise_for_status()
                for v in r.json().get("voices", []):
                    if voice_name.lower() in v["name"].lower():
                        return v["voice_id"]
                return self.voice_id
            vid = await asyncio.to_thread(lookup)
        except Exception as e:
            # Transienter Fehler (Timeout, 429): NICHT cachen, naechster
            # Eintrag versucht den Lookup erneut
            log(f"Stimmen-Lookup '{voice_name}' fehlgeschlagen: {e}")
            return self.voice_id
        self.voice_cache[voice_name] = vid
        return vid

    def find_voice_channel(self):
        for guild in self.guilds:
            for ch in guild.voice_channels:
                if ch.name.lower() == CHANNEL_NAME.lower():
                    return ch
        return None

    async def ensure_connected(self) -> bool:
        """Voice-Verbindung sicherstellen. Trennt Discord den Bot still (passiert
        bei mehreren Bots im selben Kanal), kehrte speak() sonst fuer den REST
        der Session still zurueck - der NPC sprach 'nur am Anfang'. Hier bei
        Verlust automatisch neu beitreten."""
        if self.vc and self.vc.is_connected():
            return True
        channel = self.find_voice_channel()
        if not channel:
            return False
        if self.vc:
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
            self.vc = None
        try:
            self.vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
            log("Voice neu verbunden (war getrennt).")
            return True
        except Exception as e:
            log(f"Voice-Reconnect fehlgeschlagen: {e}")
            return False

    async def speak(self, entry: dict):
        if not await self.ensure_connected():
            return

        path = ""
        cleanup = False
        if entry.get("type") == "ogg":
            path = os.path.join(REPO_DIR, entry.get("path", ""))
            if not os.path.exists(path):
                log(f"Ogg fehlt: {path}")
                return
        elif entry.get("type") == "tts":
            if not self.voice_id and not await self.ensure_voice():
                log(f"(kein TTS) {entry.get('text', '')[:60]}")
                return
            voice_id = await self.resolve_entry_voice(entry.get("voice", ""))
            try:
                path = await asyncio.to_thread(
                    tts_to_file, voice_id, self.model_id, entry.get("text", ""))
                self._tts_fails = 0
                cleanup = True
            except requests.exceptions.HTTPError as e:
                # 401/403 = Key ungueltig, 429 = Kontingent/Rate erschoepft. Genau
                # das liess in der letzten Session ALLE Stimmen verstummen ("am
                # Anfang da, dann nichts"). Nicht bei jeder Nachricht dieselbe
                # Zeile spammen: EINE klare Warnung, danach still bis Neustart.
                code = getattr(getattr(e, "response", None), "status_code", 0)
                self._tts_fails = getattr(self, "_tts_fails", 0) + 1
                if code in (401, 403, 429):
                    if self._tts_fails == 1:
                        log(f"!!! ElevenLabs lehnt TTS ab (HTTP {code}): API-Key "
                            f"ungueltig oder Kontingent/Guthaben erschoepft. Die "
                            f"Stimmen bleiben stumm, bis Key/Kontingent erneuert "
                            f"ist - pruefe das ElevenLabs-Konto (ELEVENLABS_API_KEY).")
                else:
                    log(f"TTS-Fehler: {e}")
                return
            except Exception as e:
                log(f"TTS-Fehler: {e}")
                return
        else:
            return

        waited = 0.0
        while self.vc.is_playing() and waited < 30.0:
            await asyncio.sleep(0.3)
            waited += 0.3
        if self.vc.is_playing():
            self.vc.stop()   # haengende Vorgaenger-Wiedergabe abbrechen, nicht ewig warten

        source = await discord.FFmpegOpusAudio.from_probe(path)
        done = asyncio.Event()
        self.vc.play(source, after=lambda err: done.set())
        try:
            # NICHT ewig warten: feuert der after-Callback nicht (stiller Voice-
            # Disconnect mitten im Abspielen), wuerde die Outbox-Schleife sonst
            # fuer den Rest der Session einfrieren -> "sprach nur am Anfang".
            await asyncio.wait_for(done.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            log("Wiedergabe haengt (>30 s) - abgebrochen, weiter.")
            try:
                self.vc.stop()
            except Exception:
                pass

        if cleanup:
            try:
                os.remove(path)
            except OSError:
                pass


def main() -> int:
    if not TOKEN:
        log(f"{ARGS.token_env} nicht gesetzt - beende.")
        return 1
    log(f"Starte: Kanal '{CHANNEL_NAME}', {len(OUTBOXES)} Outbox(en), "
        f"Stimme '{BOT_VOICE or 'Katalog-Default'}'.")
    client = ViktorVoice()
    try:
        client.run(TOKEN, log_handler=None)
    finally:
        _set_discord_flags(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
