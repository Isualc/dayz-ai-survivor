# dayz-ai-survivor

An autonomous DayZ survivor driven by Claude Code. The architecture has three layers: **reflexes** run on the server in the DayZ-Expansion AI (EnforceScript), **tactics** run in a local Python daemon, and **strategy + speech** come from Claude over MCP. What began as pure locomotion is now a full **multi-agent system**: up to four Claude-driven NPCs at once, each with its own memory, model, and voice, plus audible speech (Discord + 3D in-game), floating nameplates, an in-game setup menu, and direct commands via hotkeys and a command wheel.

> **This is the public, sanitized copy.** All keys, tokens, personal data and machine-specific settings have been removed — you supply your own (see the guides below). Large/derived assets are **not** in the repo and are fetched on first run: the LLM model weights (`models/`), the llama.cpp binaries (`tools/llama-bin/`), the third-party DayZ/Expansion reference sources (`reference/`), and the packed mods (`build/`).
>
> Deutsche Fassung mit voller Phasen-Historie: **[README.de.md](README.de.md)**.

---

## Documentation

| Guide | What it covers |
|---|---|
| **[docs/api_keys_en.md](docs/api_keys_en.md)** | Every API key — which ones, where to get them, how to set them |
| **[docs/discord_bot_setup_en.md](docs/discord_bot_setup_en.md)** | Creating the Discord voice bots step by step (one per NPC) |
| **[docs/claude_cli_setup_en.md](docs/claude_cli_setup_en.md)** | Installing the Claude Code CLI, authenticating, and how model selection routes |
| [docs/protocol.md](docs/protocol.md) | The JSON file-bridge protocol between the daemon and the mod |
| [.env.example](.env.example) | The complete catalog of environment variables |

---

## Prerequisites

- **Windows** with PowerShell (the tooling is PowerShell/`.bat`; the daemon is Python).
- **DayZ** + **DayZ Server** + **DayZ Tools** (from Steam).
- The **DayZ-Expansion** mod chain (CF → Dabs Framework → Expansion-Core → Expansion-AI) — subscribed via Steam Workshop. See setup below.
- **Python 3.11+** with the `mcp` package (`python -m pip install mcp`).
- **Node.js 18+** and the **Claude Code CLI** (`npm install -g @anthropic-ai/claude-code`) — see [the CLI guide](docs/claude_cli_setup_en.md).
- **Claude** access: a Max/Pro plan login *or* an `ANTHROPIC_API_KEY`.
- *(For voice)* an **ElevenLabs** key and one or more **Discord bot** tokens.
- *(Optional)* OpenAI / Gemini / xAI keys to run other models in the arena.

## Project layout

```
mod\IsuSurvivor\           EnforceScript server mod (bridge, motor, sensing, chat hook)
mod\IsuVoice\              In-game menu, command wheel, nameplates, voice SoundSets
daemon\bridge.py           File-bridge client + situation formatter
daemon\dayz_mcp.py         MCP server: the brain's tools (observe, move_to, loot_area, say, ...)
daemon\run_agent.py        Agent runner: Claude Code headless as the survivor brain
daemon\arena_supervisor.py Starts/stops the arena agents on a menu command
daemon\orchestrator.py     Referee / situation center over the squad (menu toggle)
daemon\persona_de.md       System prompt: who Viktor is and how he thinks
daemon\test_driver.py      Manual control + acceptance tests (demo, demo2)
agent_home\                The brain's working dir (CLAUDE.md = long-term memory; journal\ at runtime)
tools\                     Pack / install / start scripts (PowerShell, ASCII-only)
voice\                     Voice-line catalog + ElevenLabs generator
docs\                      Setup guides + the bridge protocol
```

## Configuration — paths via environment variables

The scripts locate your DayZ install through environment variables, with the standard Steam library as the default. Set them (via `setx`, or in a `.env` file you keep local) if your install is elsewhere — e.g. a custom Steam library on another drive.

| Variable | Default (standard Steam library) |
|---|---|
| `DAYZ_SERVER_DIR` | `C:\Program Files (x86)\Steam\steamapps\common\DayZServer` |
| `DAYZ_DIR` | `C:\Program Files (x86)\Steam\steamapps\common\DayZ` |
| `DAYZ_WORKSHOP_DIR` | `C:\Program Files (x86)\Steam\steamapps\workshop\content\221100` |
| `DAYZ_TOOLS_DIR` | `C:\Program Files (x86)\Steam\steamapps\common\DayZ Tools` |

All other variables (API keys, Discord tokens, player name, mic) are documented in [.env.example](.env.example).

## One-time setup

### 1. Subscribe to DayZ-Expansion-AI

The dependency chain is CF → Dabs Framework (DF) → Expansion-Core → Expansion-AI. Subscribe to all of them on the Steam Workshop (DayZ-Expansion-AI: <https://steamcommunity.com/sharedfiles/filedetails/?id=2792982069>). Let Steam finish downloading (~1 GB for AI).

### 2. Link the workshop mods into the server

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_mods_to_server.ps1
```

Creates junctions for the Expansion mods in the server directory, copies the bikeys, and drops the dev-server config (`serverDZ-isu.cfg`: BattlEye off, signature checks off — **local development only**).

### 3. Pack the mod

```powershell
powershell -ExecutionPolicy Bypass -File tools\pack_mod.ps1
```

Packs `mod\IsuSurvivor` with the Addon Builder CLI and copies the PBO to `%DAYZ_SERVER_DIR%\@IsuSurvivor\addons`. Re-run after every code change, then restart the server. For the in-game menu / wheel / nameplates (in `mod\IsuVoice`), use `tools\pack_mod.ps1 -ModName IsuVoice`.

### 4. Start the server

```powershell
powershell -ExecutionPolicy Bypass -File tools\start_server.ps1
```

The first start takes 1–3 minutes while Expansion generates its settings and loadouts. Once `profiles\IsuSurvivor\state.json` appears and ticks every second, the bridge is alive.

## Authenticate Claude and (optionally) set keys

- **Claude brain:** either run `claude` and `/login` (Max/Pro plan), or set `ANTHROPIC_API_KEY`. Details: [docs/claude_cli_setup_en.md](docs/claude_cli_setup_en.md).
- **Voice:** set `ELEVENLABS_API_KEY` and create the Discord bots — [docs/discord_bot_setup_en.md](docs/discord_bot_setup_en.md).
- **Other models (optional):** `tools\set_api_keys.ps1` stores `OPENAI_API_KEY` / `GEMINI_API_KEY` / `XAI_API_KEY`. Full reference: [docs/api_keys_en.md](docs/api_keys_en.md).

## Running it

**Everything in one shot** (starts the dev server if needed, waits for the bridge, warns about missing keys, then runs an agent):

```
start_all.bat
start_all.bat --idle 300        (arguments pass through to run_agent.py)
```

**The brain on its own:**

```powershell
python daemon\run_agent.py --once "Do a situation assessment, then act on priorities."
python daemon\run_agent.py            (continuous; Ctrl+C exits cleanly)
```

**The model arena** (up to four NPCs, each its own model/voice/memory):

```
start_arena.bat                  (menu: pick agents + disposition)
start_arena.bat all n            (all four, neutral)
```

In-game you configure everything from the **setup menu** (Insert key): model, role, idle cadence, turn limit, hotkeys, disposition (co-op vs. battle-royale), spawn mode, and per-NPC name/voice.

### Model selection (the prefix system)

You pick a model per NPC; the prefix decides the backend (`run_agent.resolve_backend`):

| Model string | Backend |
|---|---|
| `sonnet`, `opus`, `haiku` | Claude via your Max/Pro plan login |
| `api/sonnet`, `api/opus`, `api/haiku` | Claude via the real Anthropic API (billed per token) |
| `openai/...`, `google/...`, `xai/...` | claude-code-router (port 3456) + provider key |
| `local/gemma-4-E4B-it` | local llama-server (port 8080, free/offline) |

Claude Code is always the engine; only `ANTHROPIC_BASE_URL` is redirected. The supervisor starts the router or llama-server automatically when a turn needs one. See [the CLI guide](docs/claude_cli_setup_en.md).

## The acceptance test

```powershell
python daemon\test_driver.py demo
```

Spawns an NPC and walks it ~80 m. If it ends with `ERFOLG`, the bridge to the server mod is healthy. Other single commands: `state`, `watch`, `spawn`, `move`, `despawn` (coordinates are map coordinates, x = west-east, z = south-north; `y` is auto-derived).

## Features at a glance

- **Multi-agent arena** — four NPCs, four models, four voices, one server; they hear each other within 60 m and recognise each other by name.
- **Audible voice** — catalog phrases as pre-rendered `.ogg`, free text via ElevenLabs live TTS, delivered both in-game (3D) and over Discord.
- **Microphone input** — your mic is transcribed (ElevenLabs Scribe) and routed to the addressed NPC, or the nearest one if you don't name anyone.
- **Long-term memory** — each NPC keeps and curates its own `CLAUDE.md`; it remembers tactics and tips across sessions.
- **Autonomous looting** — a tactics layer ranks weapons/ammo/medical/food and runs the scan-prioritise-walk-pick-equip loop.
- **Inventory persistence** — inventory is restored after death/restart; agents respawn at the camp.
- **Orchestrator** (optional) — a referee that builds a shared situation picture and radios it to the squad without commanding them, preserving the model-vs-model benchmark.
- **Battle-royale mode** — free-for-all including the player, one life, equal loadouts, radio silence, showdown at the rally point.

The full phase-by-phase history (v0.4 through v1.0) is documented in **[README.de.md](README.de.md)**.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `state.json` never appears | Compile error — search `profiles\*.RPT` for `IsuSurvivor` / `Compile error`. |
| `commands.json` stays put | Mod not loaded — check `-servermod=@IsuSurvivor` and that the PBO is in `addons`. |
| `spawn` → "CreateObject returned no eAIBase" | Expansion-AI missing from `-mod`, or an incomplete workshop download. |
| NPC spawns naked | Normal on the very first start, before the loadout JSON is generated. Restart the server once. |
| `move_to` → "45s without progress" | Target unreachable (water, building, fence) — try another target. |
| Client crashes on join | Validate the install (`steam://validate/221100`), reset `Documents\DayZ\DayZ.cfg`, or join via Direct Connect. |

More detailed tables (including voice/Discord and model-routing issues) are in the three guides and in [README.de.md](README.de.md).

## License

[MIT](LICENSE) — © 2026 isualc AI. Applies to the original code in this repo (the IsuSurvivor / IsuVoice mod sources, the Python daemon, and the tooling).

Third-party note: this is a **mod for DayZ** and builds on the **DayZ-Expansion** framework. DayZ, its Tools, and the Expansion scripts belong to their respective owners and are **not** included here — obtain them through their official channels. DayZ-Expansion is licensed **CC BY-NC-ND 4.0**: using it as a workshop dependency is fine; copying or repackaging its code is not.
