# Claude CLI integration

The survivor's brain is **Claude Code** running headless. `daemon/run_agent.py` launches Claude Code as a persistent process, feeds it the situation through a file bridge, and exposes the in-game actions (`observe`, `move_to`, `say`, `loot_area`, …) as an MCP tool server (`daemon/dayz_mcp.py`). This page covers installing the CLI, authenticating it, and how model selection routes to different backends.

---

## 1. Install Node.js and the Claude Code CLI

Claude Code is distributed as an npm package and runs on Node.

1. Install **Node.js 18+** from <https://nodejs.org> (the LTS build is fine).
2. Install the CLI globally:

   ```powershell
   npm install -g @anthropic-ai/claude-code
   ```

3. Verify:

   ```powershell
   claude --version
   ```

`run_agent.py` does **not** call the `claude` wrapper directly — it invokes `node cli.js` and feeds the prompt over stdin (more reliable for long prompts on Windows). It finds both automatically:

- **Node**: from `PATH`, or set `ISU_NODE_BIN` to the full path of `node.exe`.
- **cli.js**: from the global npm install (`%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\cli.js`), or set `ISU_CLAUDE_CLI` to the full path.

Override these only if auto-detection fails (non-standard install location).

## 2. Authenticate

Pick one (see also [api_keys_en.md](api_keys_en.md)):

- **Max / Pro plan** — run `claude` once and `/login`. The bare model names (`sonnet`, `opus`, `haiku`) then run on your plan.
- **API key** — `setx ANTHROPIC_API_KEY "sk-ant-..."`. The `api/...` model names force the billed Anthropic API.

A quick check that the CLI works at all:

```powershell
claude -p "say hello"
```

## 3. How model selection routes (the prefix system)

You pick a model per NPC in `arena/agents.json` or live in the in-game setup menu (Insert key). The **prefix** decides which backend handles the turn — `run_agent.resolve_backend` maps it:

| Model string | Backend | Requires |
|---|---|---|
| `sonnet`, `opus`, `haiku` (no prefix) | Claude via your **Max/Pro plan login** | `claude /login` |
| `api/sonnet`, `api/opus`, `api/haiku`, `api/<full-id>` | Claude via the **real Anthropic API** (billed) | `ANTHROPIC_API_KEY` |
| `openai/gpt-5.5`, `google/gemini-3.5-flash`, `xai/grok-4.3` | **claude-code-router** translating Anthropic ⇄ OpenAI/Gemini/xAI | router on port 3456 + provider key |
| `local/gemma-4-E4B-it` | **llama-server** (local GGUF, free, offline) | llama-server on port 8080 |

In all cases Claude Code stays the engine; `ANTHROPIC_BASE_URL` is just redirected to the right endpoint. The short aliases map to full IDs in `ANTHROPIC_API_ALIASES` inside `run_agent.py` (`sonnet → claude-sonnet-4-6`, `opus → claude-opus-4-8`, `haiku → claude-haiku-4-5-...`). Bumping a model is a one-line edit there (and in the router config / menu list).

The arena supervisor (`daemon/arena_supervisor.py`) starts the router or llama-server automatically when a turn needs one, so you normally just pick the model and play.

## 4. Other cloud models — claude-code-router (optional)

To run OpenAI, Gemini, or xAI models as survivors:

```powershell
powershell -ExecutionPolicy Bypass -File tools\start_router.ps1
```

This installs `@musistudio/claude-code-router` (command `ccr`) if missing, generates `%USERPROFILE%\.claude-code-router\config.json` from your `OPENAI_API_KEY` / `GEMINI_API_KEY` / `XAI_API_KEY` (only if no config exists yet), and starts the router on port 3456. Useful commands: `ccr status`, `ccr stop`, `ccr ui`.

The provider/model lists in that config must match the entries the in-game menu offers. If a provider returns a 400 like "Unknown parameter 'reasoning'", that is the router passing an unsupported parameter to a specific model — adjust the model entry, it is not an auth error (which would be 401).

## 5. Local model — llama-server (optional, free/offline)

To run a survivor entirely offline on your own GPU:

```powershell
powershell -ExecutionPolicy Bypass -File tools\start_llama_gemma.ps1
```

This runs **Gemma 4 E4B** (`gemma-4-E4B-it-Q4_K_M.gguf`) through `llama-server` on port 8080, which speaks the Anthropic `/v1/messages` format natively. The script locates `llama-server.exe` via `ISU_LLAMA_SERVER`, then `PATH`, then `tools\llama-bin\`, and otherwise downloads a current `win-vulkan-x64` build. The GGUF (~4.7 GB) is downloaded to `models\` from the `unsloth/gemma-4-E4B-it-GGUF` repo on first run.

Notes:
- Tool calls need a llama.cpp build **≥ b8641** (Gemma-4 template fixes) plus `--jinja`.
- Context is capped at ~48k by default; pushing it to 128k crashed the GPU's KV build in testing. Leave it unless you have a large GPU.
- `models\`, `tools\llama-bin\`, and the GGUF are git-ignored — they are downloaded, not committed.

## 6. Quick end-to-end check

Once the CLI is installed and authenticated, this drives an NPC without the game's menu:

```powershell
python daemon\run_agent.py --once "Do a situation assessment and eat something if you are hungry."
```

The motor-only smoke test (no Claude, just the bridge to the mod) is:

```powershell
python daemon\test_driver.py demo
```

If it ends with `ERFOLG`, the bridge to the server mod is healthy.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Claude "needs more context" / only sees the first line | A wrapper truncated a long prompt. `run_agent.py` avoids this by piping over stdin; if you call the CLI yourself, pass long prompts via stdin or a file, not as one big argument. |
| `cli.js not found` | Global install missing or in a non-standard place → reinstall with `npm install -g @anthropic-ai/claude-code`, or set `ISU_CLAUDE_CLI`. |
| `node is not recognized` | Node not on `PATH` → reinstall Node, or set `ISU_NODE_BIN`. |
| Provider returns 401 | Missing/invalid key for that provider. 400 (e.g. unknown parameter) is a router/model-config issue, not the key. |
| `local/...` model never responds | llama-server not on port 8080, or a build older than b8641 (no Gemma-4 tool calls). |
