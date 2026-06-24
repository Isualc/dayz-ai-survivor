# Discord voice bots — step by step

The NPCs talk to you over Discord voice. Each survivor (Viktor, Birgit, Igor, Konrad) speaks through **its own Discord bot**, so you hear up to four distinct voices overlapping in one channel like real radio chatter. This guide sets that up from zero.

You can also run a single shared "collective" bot if you only want one voice at first — set just `DISCORD_BOT_TOKEN` and skip the per-NPC tokens. Everything below scales from one bot to four.

> A normal invite link (`discord.gg/...`) does **not** work for bots. Bots are added only through the OAuth2 flow in Step 3, and only a server owner (or someone with "Manage Server") can add them.

---

## What you need first

- A Discord account and a Discord **server** you own (free; create one with the **+** in the Discord app if you don't have one).
- A **voice channel** on that server. The default expected name is `DayZ` — create a voice channel called `DayZ`, or pick another name and set `ISU_DISCORD_CHANNEL` later.
- An **ElevenLabs API key** for the actual speech (text-to-speech, and speech-to-text if you want the bots to hear you). Without it the bot connects to the channel but stays silent. See [api_keys_en.md](api_keys_en.md).

---

## Step 1 — Create the bot application

Do this **once per NPC** (four times for the full squad: Viktor, Birgit, Igor, Konrad).

1. Open <https://discord.com/developers/applications> and sign in.
2. Top right: **New Application**.
3. Name it after the NPC, e.g. `Viktor`. This becomes the bot's display name. Accept the terms, click **Create**.

## Step 2 — Get the bot token

1. Left sidebar: **Bot**. Discord already created the bot user with the application.
2. Next to **Token**, click **Reset Token** and confirm (a 2FA code may be required).
3. **Copy the token immediately** — it is shown only once. This is the bot's password: never paste it into code, screenshots, or chat. It goes only into an environment variable (Step 5).
4. Under **Privileged Gateway Intents**: all three switches (Presence, Server Members, Message Content) can stay **OFF**. Speaking in a voice channel needs no privileged intents.

## Step 3 — Build the invite URL (OAuth2 URL Generator)

1. Left sidebar: **OAuth2**, then scroll to **OAuth2 URL Generator**.
2. Under **Scopes**, tick exactly one: `bot`.
3. **Bot Permissions** appears below. Tick:
   - **View Channels**
   - **Connect**
   - **Speak**
4. Below the generated URL there is an **Integration Type** dropdown — set it to **Guild Install**. If it is on "User Install", Discord rejects the URL later ("invalid scopes for user install") because the `bot` scope only exists for server installs.
5. Copy the **generated URL** at the bottom. Sanity check: the URL must contain `integration_type=0` (or omit the parameter), not `integration_type=1`.

If the generator doesn't offer "Guild Install": go to **Installation** in the left sidebar, tick **Guild Install** under **Installation Contexts**, save, then return to the URL Generator.

## Step 4 — Invite the bot to your server

1. Open the copied URL in a browser where you are logged into Discord.
2. Pick your server → **Continue** → review permissions → **Authorize** (solve the captcha if asked).
3. The bot now shows up **offline** in your member list. That's correct — it only goes online when `run_agent.py` (or the in-game menu) starts it.

Repeat Steps 1–4 for each NPC you want to give a voice.

## Step 5 — Set the tokens on your machine

Each NPC slot reads its token from a specific environment variable (defined in [`arena/discord.json`](../arena/discord.json)):

| NPC    | Environment variable      |
|--------|---------------------------|
| Viktor | `DISCORD_TOKEN_VIKTOR`    |
| Birgit | `DISCORD_TOKEN_BIRGIT`    |
| Igor   | `DISCORD_TOKEN_IGOR`      |
| Konrad | `DISCORD_TOKEN_KONRAD`    |
| (fallback for any slot without its own token) | `DISCORD_BOT_TOKEN` |

Persistent (survives reboots, applies to **new** terminals):

```powershell
setx DISCORD_TOKEN_VIKTOR "the-viktor-bot-token"
setx DISCORD_TOKEN_BIRGIT "the-birgit-bot-token"
setx DISCORD_TOKEN_IGOR   "the-igor-bot-token"
setx DISCORD_TOKEN_KONRAD "the-konrad-bot-token"
setx ELEVENLABS_API_KEY   "your-elevenlabs-key"
```

Current PowerShell session only:

```powershell
$env:DISCORD_TOKEN_VIKTOR = "the-viktor-bot-token"
```

If your voice channel is not named `DayZ`:

```powershell
setx ISU_DISCORD_CHANNEL "Name-of-your-voice-channel"
```

> `setx` only affects terminals opened **after** you run it. Close the current window and open a new one (or restart the game launcher) so the new values are seen.

## Step 6 — Channel layout (optional)

By default all four bots join the same channel (`DayZ`) so the voices overlap like a shared radio net. To split squads into separate channels, edit [`arena/discord.json`](../arena/discord.json) and give each bot a different `channel` value:

```json
{
  "default_channel": "DayZ",
  "bots": {
    "viktor": { "token_env": "DISCORD_TOKEN_VIKTOR", "channel": "Squad-A" },
    "birgit": { "token_env": "DISCORD_TOKEN_BIRGIT", "channel": "Squad-A" },
    "igor":   { "token_env": "DISCORD_TOKEN_IGOR",   "channel": "Squad-B" },
    "konrad": { "token_env": "DISCORD_TOKEN_KONRAD", "channel": "Squad-B" }
  }
}
```

## Step 7 — Start and test

Start an agent (this brings its bot online and joins the channel):

```powershell
python daemon\run_agent.py
```

The runner journal should print "Discord-Voice gestartet". Details and errors go to `agent_home\journal\discord_voice.log`, where you want to see:

```
[discord-voice] Eingeloggt als Viktor#1234
[discord-voice] Im Sprachkanal: YourServer / DayZ
[discord-voice] ElevenLabs-TTS aktiv.
```

Quick test without the AI brain:

```powershell
python daemon\test_driver.py say --text "Radio check"
```

The line should be audible in the voice channel (live TTS via `say` always works as long as `ELEVENLABS_API_KEY` is set).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "The resource is being rate limited" when authorizing | Discord rate-limits the OAuth2 endpoint per IP/account (switching browsers does **not** help). Close all Authorize tabs, wait 30–60 min doing nothing (every attempt extends the lock), then open it **once**. Disable ad-blockers/shields for discord.com, turn off VPN. Quick workaround: open the URL on your phone over **mobile data** (Wi-Fi off) — the invite is device-independent. |
| "invalid scopes provided for user install" | The URL has `integration_type=1` (User Install) with scope `bot`. Set the "Integration Type" dropdown to "Guild Install" (Step 3) or change `integration_type=0` in the URL. |
| `Improper token has been passed` in the log | Token copied wrong or reset. Regenerate it (Step 2) and set the variable again. |
| `voice channel 'DayZ' not found on any server` | Bot wasn't invited (Step 4), or the channel has a different name → set `ISU_DISCORD_CHANNEL`. Case doesn't matter, but the name must match. |
| Bot is in the channel but silent | No `ELEVENLABS_API_KEY` (no live TTS), or the catalog `.ogg` files are still silent placeholders → set the key and/or regenerate voice lines (`python voice\generate_voice.py --force` and repack IsuVoice). |
| Bot connects and instantly drops | Channel permissions: the bot's role needs **Connect** + **Speak** in the channel overrides, not just server-wide. |
| `setx` variables have no effect | `setx` applies only to **new** terminals — close the current window and open a fresh one. |

## Security

Treat a bot token like an API key: never commit it to git, never put it in a screenshot or Discord message. If one leaks: Developer Portal → your app → **Bot** → **Reset Token**, which immediately invalidates the old one. This repository's `.gitignore` already excludes `.env` and key files so tokens are never tracked.

Sources: [Discord Developer Portal](https://discord.com/developers/applications), [Discord OAuth2 docs](https://discord.com/developers/docs/topics/oauth2), [discord.js OAuth2 guide](https://discordjs.guide/legacy/oauth2/oauth2)
