#!/usr/bin/env python3
"""IsuSurvivor Preflight-Checks - prueft VOR dem Rundenstart, ob Keys, CLI und
Backends ueberhaupt funktionieren koennen.

Motivation: eine halbe Stunde "NPCs sind stumm/hirntot" hatte bisher fast immer
eine banale Ursache, die sich in 5 Sekunden haette pruefen lassen (Discord-Token
nicht geerbt, cli.js nach npm-Update verschwunden, Fable ohne Konto-Zugriff,
Router-Port zu). Dieser Check laeuft im Supervisor vor JEDEM Start und ist
zusaetzlich eigenstaendig aufrufbar:

  python daemon\\preflight.py                (Roster-Modelle aus arena/agents.json)
  python daemon\\preflight.py sonnet api/opus openai/gpt-5.5   (explizite Modelle)

Levels: ok / warn / fail. Voice-Keys (Discord, ElevenLabs) sind IMMER nur warn -
ohne sie laeuft die Runde stumm, aber sie laeuft. fail = die Runde wuerde sicher
Slots verbrennen (fehlender API-Key, kein CLI-Startpfad).

Tiefer 1-Token-Modell-Ping (kostet echte Tokens/Latenz) NUR mit
ISU_PREFLIGHT_PING=1. Default aus.

Nur stdlib (urllib/socket) - keine neuen Dependencies.
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
ROSTER_FILE = os.path.join(REPO_DIR, "arena", "agents.json")

# CLI-Startpfad-Autoerkennung - NACHGEBAUT aus run_agent.py (dort Z34ff).
# Bewusst NICHT importiert: run_agent hat Modul-Seiteneffekte (Pfad-Setup,
# globale Dateien) und gehoert nicht in einen reinen Check-Prozess.
NODE = r"C:\Program Files\nodejs\node.exe"
CLI = r"C:\Users\isual\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\cli.js"
CLI_EXE = r"C:\Users\isual\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"

# Ports muessen zu run_agent.resolve_backend / arena_supervisor passen
CCR_PORT = 3456      # claude-code-router: openai/ google/ xai/
LLAMA_PORT = 8080    # llama-server: local/

# Kurzname -> volle Anthropic-Modell-ID (Kopie aus run_agent.ANTHROPIC_API_ALIASES,
# nur fuer den optionalen Ping gebraucht)
ANTHROPIC_API_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
}

_COLORS = {"ok": "\x1b[32m", "warn": "\x1b[33m", "fail": "\x1b[31m"}
_RESET = "\x1b[0m"


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _http(url: str, headers: dict | None = None, data: bytes | None = None,
          timeout: float = 5.0) -> tuple[int, str]:
    """(HTTP-Status, Body-Anfang). Status 0 = Verbindungs-/Timeout-Fehler,
    Detail dann die Fehlermeldung."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# ------------------------------------------------------------------- Checks
# Jeder Check liefert (name, level, detail) mit level in ok/warn/fail.

def check_discord() -> tuple[str, str, str]:
    """DISCORD_BOT_TOKEN gesetzt + REST-Validierung. Voice = warn, nie fail."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return ("Discord-Token", "warn",
                "DISCORD_BOT_TOKEN nicht gesetzt - Funk bleibt stumm "
                "(Konsole aus frischer Shell starten?)")
    status, body = _http("https://discord.com/api/v10/users/@me",
                         headers={"Authorization": "Bot " + token,
                                  "User-Agent": "IsuSurvivorPreflight/1.0"})
    if status == 200:
        name = ""
        try:
            name = json.loads(body).get("username", "")
        except Exception:
            pass
        return ("Discord-Token", "ok", f"gueltig (Bot '{name}')" if name else "gueltig")
    if status == 401:
        return ("Discord-Token", "warn", "Token UNGUELTIG (401) - Bots loggen nicht ein")
    if status == 0:
        return ("Discord-Token", "warn", f"Discord nicht erreichbar ({body[:80]})")
    return ("Discord-Token", "warn", f"Discord antwortet HTTP {status}")


def check_elevenlabs() -> tuple[str, str, str]:
    """ELEVENLABS_API_KEY validieren. Voice = warn, nie fail."""
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        return ("ElevenLabs-Key", "warn",
                "ELEVENLABS_API_KEY nicht gesetzt - keine NPC-Stimmen, kein Mikro-Router")
    status, body = _http("https://api.elevenlabs.io/v1/user",
                         headers={"xi-api-key": key})
    if status == 200:
        return ("ElevenLabs-Key", "ok", "gueltig")
    if status == 401:
        return ("ElevenLabs-Key", "warn", "Key UNGUELTIG (401) - TTS faellt aus")
    if status == 0:
        return ("ElevenLabs-Key", "warn", f"ElevenLabs nicht erreichbar ({body[:80]})")
    return ("ElevenLabs-Key", "warn", f"ElevenLabs antwortet HTTP {status}")


def check_cli() -> tuple[str, str, str]:
    """Claude-Code-Startpfad: cli.js (alt, node) oder native claude.exe (neu).
    Fehlt beides, startet KEIN Gehirn -> fail."""
    if os.path.exists(CLI):
        if os.path.exists(NODE):
            return ("Claude-CLI", "ok", "cli.js + node.exe (alter Startpfad)")
        return ("Claude-CLI", "fail",
                f"cli.js da, aber node.exe fehlt ({NODE})")
    if os.path.exists(CLI_EXE):
        return ("Claude-CLI", "ok", "native claude.exe (Standalone ab ~2.1.19x)")
    return ("Claude-CLI", "fail",
            "weder cli.js noch bin\\claude.exe gefunden - "
            "npm install -g @anthropic-ai/claude-code")


def check_ccr(models: list[str]) -> tuple[str, str, str] | None:
    """CCR-Port 3456, wenn openai/google/xai-Modelle im Roster sind. Der
    Supervisor startet den Router bei Bedarf selbst -> zu = warn, nicht fail."""
    if not any(m.startswith(("openai/", "google/", "xai/")) for m in models):
        return None
    if _port_open(CCR_PORT):
        return ("Cloud-Router", "ok", f"Port {CCR_PORT} offen")
    return ("Cloud-Router", "warn",
            f"Port {CCR_PORT} zu - Supervisor startet ihn beim Rundenstart "
            "automatisch (start_router.ps1)")


def check_llama(models: list[str]) -> tuple[str, str, str] | None:
    """llama-server-Port 8080, wenn ein local/-Modell im Roster ist."""
    if not any(m.startswith("local/") for m in models):
        return None
    if _port_open(LLAMA_PORT):
        return ("llama-server", "ok", f"Port {LLAMA_PORT} offen")
    return ("llama-server", "warn",
            f"Port {LLAMA_PORT} zu - Supervisor startet ihn beim Rundenstart "
            "automatisch (erster Start: ~5 GB Gemma-Download!)")


def check_anthropic_key(models: list[str]) -> tuple[str, str, str] | None:
    """api/-Modelle brauchen ANTHROPIC_API_KEY (erzwingt API statt Max-Plan).
    Ohne Key bricht der Slot sofort ab -> fail."""
    if not any(m.startswith(("api/", "anthropic/")) for m in models):
        return None
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return ("Anthropic-API-Key", "ok", "gesetzt (api/-Modelle)")
    return ("Anthropic-API-Key", "fail",
            "api/-Modell gewaehlt, aber ANTHROPIC_API_KEY fehlt - "
            "setx ANTHROPIC_API_KEY sk-ant-... und Konsole neu starten")


def check_fable(models: list[str]) -> tuple[str, str, str] | None:
    """Hinweis-Check: fable-Modelle werden ohne ISU_FABLE_ENABLED=1 vom
    Supervisor auf sonnet umgebogen (Exportkontroll-Sperre 06/2026, Redeploy
    seit 01.07. - Konto-Zugriff aber nicht garantiert)."""
    if not any("fable" in m.lower() for m in models):
        return None
    if _env_true("ISU_FABLE_ENABLED"):
        return ("Fable-5", "ok",
                "ISU_FABLE_ENABLED=1 - Fable-Wahl geht unveraendert durch")
    return ("Fable-5", "warn",
            "fable gewaehlt, ISU_FABLE_ENABLED nicht gesetzt - Supervisor "
            "biegt auf sonnet um (Zugriff erst bestaetigen, dann Flag setzen)")


def _ping_messages(base_url: str, model: str, headers: dict) -> tuple[str, str]:
    """1-Token-Ping gegen einen /v1/messages-Endpunkt. -> (level, detail)."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    h = {"content-type": "application/json",
         "anthropic-version": "2023-06-01"}
    h.update(headers)
    status, body = _http(base_url.rstrip("/") + "/v1/messages",
                         headers=h, data=payload, timeout=20.0)
    if status == 200:
        return ("ok", "antwortet")
    if status == 0:
        return ("fail", f"nicht erreichbar ({body[:100]})")
    return ("fail", f"HTTP {status}: {body[:120]}")


def check_pings(models: list[str]) -> list[tuple[str, str, str]]:
    """Tiefer Modell-Ping (kostet Tokens/Latenz) - NUR mit ISU_PREFLIGHT_PING=1.
    Pingt pro Backend hoechstens einmal:
      api/<x>          -> echte Anthropic-API (braucht ANTHROPIC_API_KEY)
      openai/google/xai -> CCR (nur wenn Port schon offen)
      local/<x>        -> llama-server (nur wenn Port schon offen)
    Max-Plan-Modelle (ohne Praefix) laufen ueber den CLI-Login und werden hier
    nicht gepingt (kein API-Endpunkt ohne Key)."""
    if not _env_true("ISU_PREFLIGHT_PING"):
        return []
    results: list[tuple[str, str, str]] = []
    pinged: set[str] = set()
    for m in models:
        ml = m.strip()
        if ml.startswith(("api/", "anthropic/")):
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not key or "anthropic-api" in pinged:
                continue
            pinged.add("anthropic-api")
            name = ml.partition("/")[2]
            full = ANTHROPIC_API_ALIASES.get(name.lower(), name)
            level, detail = _ping_messages("https://api.anthropic.com", full,
                                           {"x-api-key": key})
            results.append((f"Ping api/{name}", level, detail))
        elif ml.startswith(("openai/", "google/", "xai/")):
            if f"ccr:{ml}" in pinged or not _port_open(CCR_PORT):
                continue
            pinged.add(f"ccr:{ml}")
            provider, _, name = ml.partition("/")
            ccr_provider = {"openai": "openai", "google": "gemini",
                            "xai": "xai"}[provider]
            level, detail = _ping_messages(f"http://127.0.0.1:{CCR_PORT}",
                                           f"{ccr_provider},{name}",
                                           {"authorization": "Bearer test"})
            results.append((f"Ping {ml}", level, detail))
        elif ml.startswith("local/"):
            if "llama" in pinged or not _port_open(LLAMA_PORT):
                continue
            pinged.add("llama")
            level, detail = _ping_messages(f"http://127.0.0.1:{LLAMA_PORT}",
                                           ml.partition("/")[2],
                                           {"authorization": "Bearer local"})
            results.append((f"Ping {ml}", level, detail))
    return results


# --------------------------------------------------------------- Aggregation

def roster_models() -> list[str]:
    """Default-Modelle aus arena/agents.json (fuer den Standalone-Aufruf)."""
    try:
        with open(ROSTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(a.get("model") or "").strip().lower()
                for a in data.get("agents", []) if a.get("model")]
    except (OSError, json.JSONDecodeError):
        return []


def run_checks(models: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Alle Checks ausfuehren. models = aktive Modell-Strings der Runde
    (Kleinschreibung egal); None = Roster-Defaults aus agents.json."""
    if models is None:
        models = roster_models()
    models = [(m or "").strip().lower() for m in models]
    results: list[tuple[str, str, str]] = []
    results.append(check_cli())
    results.append(check_discord())
    results.append(check_elevenlabs())
    for check in (check_ccr, check_llama, check_anthropic_key, check_fable):
        r = check(models)
        if r:
            results.append(r)
    results.extend(check_pings(models))
    return results


def summarize(results: list[tuple[str, str, str]]) -> tuple[int, int, int]:
    """(ok, warn, fail)-Zaehler."""
    ok = sum(1 for _, lv, _ in results if lv == "ok")
    warn = sum(1 for _, lv, _ in results if lv == "warn")
    fail = sum(1 for _, lv, _ in results if lv == "fail")
    return ok, warn, fail


def print_report(results: list[tuple[str, str, str]]) -> None:
    """Farbige Konsolen-Zusammenfassung (ANSI; os.system('') schaltet auf
    Windows-Konsolen die VT-Sequenzen frei)."""
    if os.name == "nt":
        os.system("")
    print("--- PREFLIGHT " + "-" * 46)
    for name, level, detail in results:
        color = _COLORS.get(level, "")
        print(f"  {color}[{level.upper():4}]{_RESET} {name}: {detail}")
    ok, warn, fail = summarize(results)
    print(f"--- {ok} ok, {warn} warn, {fail} fail " + "-" * 30)


def main() -> int:
    models = [m for m in sys.argv[1:] if m] or None
    results = run_checks(models)
    print_report(results)
    _, _, fail = summarize(results)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
