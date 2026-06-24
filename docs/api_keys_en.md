# API keys — what you need and where to get them

The project reads every key from an **environment variable**. Nothing is hardcoded, and `.env` / key files are git-ignored. This page lists each key, whether it is required, where to obtain it, and how to set it on Windows.

See [`.env.example`](../.env.example) for the complete variable catalog in one place.

---

## At a glance

| Key | Required? | What it unlocks | Get it from |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | One auth method required (see below) | Claude as the survivor brain, billed per token (`api/...` models) | <https://console.anthropic.com> |
| *(or)* Claude Max/Pro login | One auth method required | Claude brain via your plan (bare model names) | run `claude` → `/login` |
| `ELEVENLABS_API_KEY` | Required for voice | Discord text-to-speech + microphone speech-to-text | <https://elevenlabs.io> |
| `DISCORD_TOKEN_VIKTOR` … `_KONRAD`, or `DISCORD_BOT_TOKEN` | Required for Discord voice | The bots that speak in your voice channel | <https://discord.com/developers/applications> |
| `OPENAI_API_KEY` | Optional | `openai/...` models in the in-game menu | <https://platform.openai.com/api-keys> |
| `GEMINI_API_KEY` | Optional | `google/...` models | <https://aistudio.google.com/apikey> (free tier) |
| `XAI_API_KEY` | Optional | `xai/...` models | <https://console.x.ai> |

You do **not** need every key. The minimum to see an NPC think and act is one Claude auth method. Add ElevenLabs + Discord for voice. Add OpenAI/Gemini/xAI only if you want to pit other models against Claude in the arena.

---

## 1. Claude (the brain) — two ways to authenticate

Claude Code is the engine that drives each NPC. Pick **one**:

- **Max / Pro plan login (no key).** Run `claude` once in a terminal and `/login`. The bare model names in the in-game menu (`sonnet`, `opus`, `haiku`) then run through your plan. Best if you already pay for Claude.
- **Pay-per-token API key.** Get a key at <https://console.anthropic.com> → **API Keys**, then set `ANTHROPIC_API_KEY`. Selecting an `api/...` model in the menu (e.g. `api/sonnet`) forces the real Anthropic API and bills per token, independent of any plan.

Details on installing the CLI and how model prefixes route are in [claude_cli_setup_en.md](claude_cli_setup_en.md).

## 2. ElevenLabs (voice) — required for sound

Voice output (and the bots hearing you) runs through ElevenLabs. Without `ELEVENLABS_API_KEY` the bots still join the Discord channel but stay silent, and the microphone listener is disabled.

1. Sign up at <https://elevenlabs.io>.
2. Profile → **API Keys** → create a key.
3. Set `ELEVENLABS_API_KEY` (Step 4 below).

Per-NPC voices are chosen by name in `arena/agents.json` (e.g. "Helmut - German Epic", "Sarah"). Any voice from your ElevenLabs account works; partial-name matching is supported.

## 3. Discord bot tokens — required for Discord voice

Each NPC speaks through its own Discord bot. Creating the bots and obtaining the tokens is its own walkthrough: [discord_bot_setup_en.md](discord_bot_setup_en.md). The variables are `DISCORD_TOKEN_VIKTOR`, `DISCORD_TOKEN_BIRGIT`, `DISCORD_TOKEN_IGOR`, `DISCORD_TOKEN_KONRAD`, with `DISCORD_BOT_TOKEN` as a shared fallback.

## 4. Optional cloud models (arena) — OpenAI / Gemini / xAI

These let you run other models as survivors alongside Claude. They route through the **claude-code-router** (`ccr`, port 3456), which the supervisor starts automatically when you pick an `openai/`, `google/`, or `xai/` model. Keys:

- **OpenAI** — <https://platform.openai.com/api-keys> → `OPENAI_API_KEY`
- **Google Gemini** — <https://aistudio.google.com/apikey> (free tier available) → `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)
- **xAI Grok** — <https://console.x.ai> → `XAI_API_KEY`

The router config (`%USERPROFILE%\.claude-code-router\config.json`) is generated from these variables on first run. Note that the model IDs in that config (e.g. `gpt-5.4-mini`, `gemini-3.5-flash`, `grok-4.3`) must match the entries the in-game menu offers — adjust both if a provider renames a model.

---

## 5. How to set the variables (Windows)

**Interactive helper (cloud model keys):**

```powershell
powershell -ExecutionPolicy Bypass -File tools\set_api_keys.ps1
```

It prompts for `OPENAI_API_KEY`, `GEMINI_API_KEY`, and `XAI_API_KEY`, stores them as **persistent user environment variables**, and updates an existing router config if present.

**Persistent, for any key (recommended):**

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
setx ELEVENLABS_API_KEY "..."
setx DISCORD_TOKEN_VIKTOR "..."
```

> `setx` writes to your user profile and only affects terminals opened **afterwards**. Open a new window — and restart the game launcher/supervisor — so the new values are picked up.

**Current shell only (temporary):**

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

**Check what is set:**

```powershell
[Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY","User")
```

---

## Security

- Keys and tokens go **only** into environment variables, never into source files. This repo's `.gitignore` already excludes `.env`, `*.key`, and `*.token`.
- If a key leaks, rotate it at the provider (Anthropic/OpenAI/Google/xAI console, ElevenLabs profile, or Discord Developer Portal → Reset Token). Rotation immediately invalidates the old value.
- Never paste a key into a screenshot, a chat, or a commit message.
