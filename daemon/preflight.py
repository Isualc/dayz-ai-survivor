#!/usr/bin/env python3
"""IsuSurvivor Preflight-Checks - prüft VOR dem Rundenstart, ob Keys, CLI und
Backends überhaupt funktionieren können.

Motivation: eine halbe Stunde "NPCs sind stumm/hirntot" hatte bisher fast immer
eine banale Ursache, die sich in 5 Sekunden hätte prüfen lassen (Discord-Token
nicht geerbt, cli.js nach npm-Update verschwunden, Fable ohne Konto-Zugriff,
Router-Port zu). Dieser Check läuft im Supervisor vor JEDEM Start und ist
zusätzlich eigenständig aufrufbar:

  python daemon\\preflight.py                (Roster-Modelle aus arena/agents.json)
  python daemon\\preflight.py sonnet api/opus openai/gpt-5.5   (explizite Modelle)

Levels: ok / warn / fail. Voice-Keys (Discord, ElevenLabs) sind IMMER nur warn -
ohne sie läuft die Runde stumm, aber sie läuft. fail = die Runde würde sicher
Slots verbrennen (fehlender API-Key, kein CLI-Startpfad).

Cloud-Validierung für Voice NUR mit ISU_PREFLIGHT_NETWORK=1, Modell-Pings
NUR mit ISU_PREFLIGHT_PING=1. Beide standardmäßig aus. Ohne diese Flags
werden nur Installationspfade, Key-Präsenz und lokale Ports geprüft;
Antigravity wird ausschließlich lokal mit --version auf Startbarkeit geprüft.

Nur stdlib (urllib/socket) - keine neuen Dependencies.
"""

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
ROSTER_FILE = os.path.join(REPO_DIR, "arena", "agents.json")

# CLI-Startpfad-Autoerkennung - NACHGEBAUT aus run_agent.py (dort Z34ff).
# Bewusst NICHT importiert: run_agent hat Modul-Seiteneffekte (Pfad-Setup,
# globale Dateien) und gehört nicht in einen reinen Check-Prozess.
NODE = os.environ.get("ISU_NODE_BIN") or r"C:\Program Files\nodejs\node.exe"
CLI = os.path.expandvars(r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\cli.js")
CLI_EXE = os.environ.get("ISU_CLAUDE_CLI") or os.path.expandvars(r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe")
PROJECT_CLI_EXE = os.path.join(REPO_DIR, ".runtime", "claude-code", "node_modules",
                             "@anthropic-ai", "claude-code", "bin", "claude.exe")

# Ports müssen zu run_agent.resolve_backend / arena_supervisor passen
CCR_PORT = 3456      # claude-code-router: openai/ google/ xai/
LLAMA_PORT = 8080    # llama-server: local/
NATIVE_PREFIXES = ("codex/", "gemini-cli/", "antigravity/", "mistral/", "moonshot/")
NATIVE_CLI_CONFIG = {
    "codex": ("Codex-CLI", "ISU_CODEX_CLI"),
    "gemini-cli": ("Gemini-CLI", "ISU_GEMINI_CLI"),
    "antigravity": ("Antigravity-CLI", "ISU_ANTIGRAVITY_CLI"),
}

# Kurzname -> volle Anthropic-Modell-ID (Kopie aus run_agent.ANTHROPIC_API_ALIASES,
# nur für den optionalen Ping gebraucht)
ANTHROPIC_API_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
    "fable-5.1": "claude-fable-5-1",
    "fable-5-1": "claude-fable-5-1",
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
    if not _env_true("ISU_PREFLIGHT_NETWORK"):
        return ("Discord-Token", "ok", "gesetzt (keine Online-Validierung)")
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
    if not _env_true("ISU_PREFLIGHT_NETWORK"):
        return ("ElevenLabs-Key", "ok", "gesetzt (keine Online-Validierung)")
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
    if os.path.isfile(PROJECT_CLI_EXE):
        return ("Claude-CLI", "ok", "project runtime (.runtime/claude-code)")
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


def check_native_cli(models: list[str], provider: str) -> tuple[str, str, str] | None:
    """Startpfad bzw. Antigravity --version; keinen Login/Modell-Call auslösen.

    Der Worker und der Preflight teilen die Pfadauflosung; damit reicht ein
    npm-PowerShell-Shim alleine nicht für ein irrtümliches 'ok'.
    """
    if not any(m.startswith(provider + "/") for m in models):
        return None
    label, env_name = NATIVE_CLI_CONFIG[provider]
    from llm_backends import cli_launch
    try:
        command = cli_launch(provider)
    except (OSError, ValueError, RuntimeError):
        return (label, "fail", f"kein startbarer CLI-Pfad - PATH oder {env_name} pruefen")
    if not command:
        return (label, "fail", "kein startbarer CLI-Pfad")
    if provider == "antigravity":
        # No prompt, auth operation or keychain read: even explicitly enabled
        # model pings must never turn this installation check into a model call.
        try:
            result = subprocess.run(
                [*command, "--version"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            return (label, "fail", "lokales --version nach 10 Sekunden abgebrochen")
        except OSError:
            return (label, "fail", f"CLI nicht startbar - {env_name} oder agy-Installation pruefen")
        if result.returncode != 0:
            return (label, "fail", "lokales --version fehlgeschlagen - agy-Installation pruefen")
        return (label, "ok", "lokales --version erfolgreich; nutzt vorhandenen OAuth-Login, "
                "Anmeldung/Kontolimits nicht geprueft (kein API-Key noetig)")
    return (label, "ok", "Startpfad vorhanden; Login/Kontolimits nicht online geprueft")


def check_direct_api_key(models: list[str], provider: str,
                         env_name: str) -> tuple[str, str, str] | None:
    if not any(m.startswith(provider + "/") for m in models):
        return None
    label = provider.capitalize() + "-API-Key"
    if os.environ.get(env_name, "").strip():
        return (label, "ok", "gesetzt (direkte API, keine Online-Validierung)")
    return (label, "fail", f"{env_name} fehlt - tools/set_api_keys.ps1 ausfuehren "
            "und Supervisor neu starten")


def check_retired_moonshot(models: list[str]) -> tuple[str, str, str] | None:
    # Offizielle Modellliste, geprüft 2026-09-08:
    # https://platform.kimi.ai/docs/models. Custom-IDs bleiben frei wählbar.
    retired = {"kimi-k2.5", "kimi-k2-0905-preview", "kimi-k2-0711-preview",
               "kimi-k2-turbo-preview", "kimi-k2-thinking", "kimi-k2-thinking-turbo",
               "kimi-latest", "kimi-thinking-preview"}
    for model in models:
        provider, _, name = model.partition("/")
        if provider == "moonshot" and (name in retired or name.startswith("moonshot-v1")):
            return ("Moonshot-Modell", "fail", "gewaehlte alte Modellserie ist eingestellt; "
                    "kimi-k2.6, kimi-k3 oder kimi-k2.7-code waehlen")
    return None


def check_retired_gemini_cli(models: list[str]) -> tuple[str, str, str] | None:
    """Warnung: Gemini CLI ohne persönlichen Google-Zugang seit 18.06.2026
    (UNSUPPORTED_CLIENT bei jedem Zug). Der Supervisor leitet gemini-cli/*
    auf antigravity/default um, solange ISU_GEMINI_CLI_ALLOWED nicht gesetzt
    ist - darum nur warn, kein fail."""
    if not any(m.startswith("gemini-cli/") for m in models):
        return None
    if _env_true("ISU_GEMINI_CLI_ALLOWED"):
        return ("Gemini-CLI", "warn", "ISU_GEMINI_CLI_ALLOWED gesetzt - braucht eine "
                "Code-Assist-Lizenz, persoenlicher Login ist seit 18.06.2026 abgeschaltet")
    return ("Gemini-CLI", "warn", "persoenlicher Zugang seit 18.06.2026 abgeschaltet - "
            "Supervisor startet stattdessen antigravity/default (gleicher Google-Login)")


def check_fable(models: list[str]) -> tuple[str, str, str] | None:
    """Hinweis-Check: fable-Modelle werden ohne ISU_FABLE_ENABLED=1 vom
    Supervisor auf sonnet umgebogen (Exportkontroll-Sperre 06/2026, Redeploy
    seit 01.07. - Konto-Zugriff aber nicht garantiert)."""
    if not any(m.rsplit("/", 1)[-1] in ("fable", "claude-fable-5")
               and ("/" not in m or m.startswith(("api/", "anthropic/")))
               for m in models):
        return None
    if _env_true("ISU_FABLE_ENABLED"):
        return ("Fable-5", "ok",
                "ISU_FABLE_ENABLED=1 - Fable-Wahl geht unveraendert durch")
    return ("Fable-5", "warn",
            "fable gewaehlt, ISU_FABLE_ENABLED nicht gesetzt - Supervisor "
            "biegt auf sonnet um (Zugriff erst bestaetigen, dann Flag setzen)")


def cli_launch() -> list[str]:
    """Startpfad wie run_agent.CLI_LAUNCH (Projekt-Runtime > cli.js > claude.exe)."""
    if os.path.isfile(PROJECT_CLI_EXE):
        return [PROJECT_CLI_EXE]
    if os.path.exists(CLI) and os.path.exists(NODE):
        return [NODE, CLI]
    if os.path.exists(CLI_EXE):
        return [CLI_EXE]
    return []


def claude_auth_status(launch: list[str] | None = None) -> tuple[bool | None, str]:
    """Lokaler Login-Check per 'claude auth status --json' (kein Modellaufruf,
    keine Tokens). -> (eingeloggt?, Detail); None = nicht feststellbar.

    Gleiche Env wie der Gehirn-Spawn für Plan-Modelle: ein gesetzter
    ANTHROPIC_API_KEY bleibt drin, weil die CLI ihn dann statt des OAuth-Logins
    nimmt. Befund 16.10.: abgelaufener OAuth-Login kam erst im Spiel als
    Sprechblase "Failed to authenticate: OAuth session expired" an."""
    launch = launch if launch is not None else cli_launch()
    if not launch:
        return None, "kein CLI-Startpfad"
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    try:
        proc = subprocess.run(
            [*launch, "auth", "status", "--json"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return None, "'claude auth status' nach 20 s abgebrochen"
    except OSError as e:
        return None, f"'claude auth status' nicht startbar ({e})"
    out = proc.stdout or ""
    start, end = out.find("{"), out.rfind("}")
    try:
        data = json.loads(out[start:end + 1]) if start >= 0 < end else {}
    except json.JSONDecodeError:
        data = {}
    if "loggedIn" not in data:
        return None, f"unerwartete Antwort (exit {proc.returncode}): {out.strip()[:100]}"
    method = data.get("authMethod") or "?"
    if data.get("loggedIn"):
        return True, f"eingeloggt ({method})"
    return False, f"nicht eingeloggt ({method})"


LOGIN_HINT = ("Claude-Login fehlt oder ist abgelaufen - neues Terminal: "
              "'claude auth login' (oder 'claude' und dann /login)")


def check_claude_login(models: list[str]) -> tuple[str, str, str] | None:
    """Plan-Modelle (ohne Präfix) laufen über den OAuth-Login der CLI. Ist er
    weg, antwortet jedes Gehirn nur mit einem Auth-Fehler -> fail."""
    if not any(m and "/" not in m for m in models):
        return None
    logged_in, detail = claude_auth_status()
    if logged_in:
        return ("Claude-Login", "ok", detail)
    if logged_in is None:
        return ("Claude-Login", "warn", f"Status unklar: {detail}")
    return ("Claude-Login", "fail", f"{detail}. {LOGIN_HINT}")


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
    Pingt pro Backend höchstens einmal:
      api/<x>          -> echte Anthropic-API (braucht ANTHROPIC_API_KEY)
      openai/google/xai -> CCR (nur wenn Port schon offen)
      local/<x>        -> llama-server (nur wenn Port schon offen)
    Max-Plan-Modelle (ohne Präfix) laufen über den CLI-Login und werden hier
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
    """Default-Modelle aus arena/agents.json (für den Standalone-Aufruf)."""
    try:
        with open(ROSTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(a.get("model") or "").strip().lower()
                for a in data.get("agents", []) if a.get("model")]
    except (OSError, json.JSONDecodeError):
        return []


def run_checks(models: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Alle Checks ausführen. models = aktive Modell-Strings der Runde
    (Kleinschreibung egal); None = Roster-Defaults aus agents.json."""
    if models is None:
        models = roster_models()
    models = [(m or "").strip().lower() for m in models]
    results: list[tuple[str, str, str]] = []
    # Native Provider laufen ohne Claude; ein rein natives Roster darf deshalb
    # nicht an einer fehlenden Claude-Installation scheitern.
    if any(not m.startswith(NATIVE_PREFIXES) for m in models):
        cli = check_cli()
        results.append(cli)
        # Login nur prüfen, wenn die CLI überhaupt startbar ist.
        login = check_claude_login(models) if cli[1] == "ok" else None
        if login:
            results.append(login)
    results.append(check_discord())
    results.append(check_elevenlabs())
    for provider in NATIVE_CLI_CONFIG:
        r = check_native_cli(models, provider)
        if r:
            results.append(r)
    for provider, env_name in (("mistral", "MISTRAL_API_KEY"),
                               ("moonshot", "MOONSHOT_API_KEY")):
        r = check_direct_api_key(models, provider, env_name)
        if r:
            results.append(r)
    for check in (check_ccr, check_llama, check_anthropic_key, check_fable,
                  check_retired_moonshot, check_retired_gemini_cli):
        r = check(models)
        if r:
            results.append(r)
    results.extend(check_pings(models))
    return results


def summarize(results: list[tuple[str, str, str]]) -> tuple[int, int, int]:
    """(ok, warn, fail)-Zähler."""
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


def login_main(interactive: bool) -> int:
    """'preflight.py --login [--fix]': nur den Claude-Login der Roster-Modelle
    prüfen (Startskript). --fix bietet bei fehlendem Login direkt
    'claude auth login' an (Browser-Flow, Ein-/Ausgabe in dieser Konsole)."""
    result = check_claude_login(roster_models())
    if result is None:
        print("  [OK  ] Claude-Login: kein Plan-Modell im Roster, nicht nötig")
        return 0
    print_report([result])
    if result[1] != "fail":
        return 0
    if not interactive or not sys.stdin.isatty():
        return 1
    ans = input("  Claude-Login jetzt starten (Browser öffnet sich)? (J/n) ").strip().lower()
    if ans not in ("", "j", "y", "ja", "yes"):
        return 1
    launch = cli_launch()
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    try:
        subprocess.run([*launch, "auth", "login"], env=env, check=False)
    except OSError as e:
        print(f"  'claude auth login' nicht startbar: {e}")
        return 1
    result = check_claude_login(roster_models())
    print_report([result])
    return 0 if result and result[1] != "fail" else 1


def main() -> int:
    if "--login" in sys.argv[1:]:
        return login_main("--fix" in sys.argv[1:])
    models = [m for m in sys.argv[1:] if m] or None
    results = run_checks(models)
    print_report(results)
    _, _, fail = summarize(results)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
