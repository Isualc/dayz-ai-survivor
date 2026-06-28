# ===========================================================================
#  dayz-ai-survivor - guided setup wizard
# ===========================================================================
#  Run via INSTALL.bat (double-click). Automates everything the individual
#  tools\*.ps1 scripts do, in the right order, with live progress and path
#  auto-detection - so a non-developer gets to "game ready" without touching
#  a console. Only the API keys and the optional Discord bot stay manual; the
#  wizard guides those with links and prompts.
#
#  Safe to re-run: every step detects existing state and skips or repairs.
#  ASCII only (PowerShell 5.1 safe).
#
#  Switches:
#    -SkipTools     do not try to install Python/Node/Claude CLI
#    -SkipWorkshop  do not wait for the Steam workshop downloads
#    -NoLaunch      do not offer to start the game at the end
# ===========================================================================

param(
    [switch]$SkipTools,
    [switch]$SkipWorkshop,
    [switch]$NoLaunch
)

# Continue (not Stop): an installer should always reach its final summary even
# if one step hits an unexpected cmdlet error. Critical/external steps below
# check exit codes or use explicit try/catch instead.
$ErrorActionPreference = "Continue"
$RepoDir = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent

# --- DayZ Steam app ids -----------------------------------------------------
$APP_DAYZ      = "221100"   # DayZ (the game client - paid, you must own it)
$APP_SERVER    = "223350"   # DayZ Server (free tool)
$WORKSHOP_APP  = "221100"   # workshop content lives under .../workshop/content/221100

# --- The 8 workshop dependencies (same set as install_mods_to_server.ps1) ---
$WORKSHOP_MODS = @(
    @{ Name = "Community Framework (CF)";   Id = "1559212036" },
    @{ Name = "Dabs Framework (DF)";        Id = "2545327648" },
    @{ Name = "DayZ-Expansion-Bundle";      Id = "2572331007" },
    @{ Name = "DayZ-Expansion-Licensed";    Id = "2116157322" },
    @{ Name = "DayZ-Expansion-Animations";  Id = "2793893086" },
    @{ Name = "VPPAdminTools";              Id = "1828439124" },
    @{ Name = "DayZ-Expansion-Core";        Id = "2291785308" },
    @{ Name = "DayZ-Expansion-AI";          Id = "2792982069" }
)

# ===========================================================================
#  UI helpers
# ===========================================================================
$script:StepNo = 0
function Write-Phase($text) {
    $script:StepNo++
    Write-Host ""
    Write-Host ("=" * 74) -ForegroundColor DarkCyan
    Write-Host (" STEP {0}: {1}" -f $script:StepNo, $text) -ForegroundColor Cyan
    Write-Host ("=" * 74) -ForegroundColor DarkCyan
}
function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Info($text) { Write-Host "  [..]   $text" -ForegroundColor Gray }
function Write-Warn($text) { Write-Host "  [WARN] $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "  [FAIL] $text" -ForegroundColor Red }
function Ask-YesNo($question, $defaultYes = $true) {
    $hint = if ($defaultYes) { "(Y/n)" } else { "(y/N)" }
    $a = Read-Host "  $question $hint"
    if (-not $a) { return $defaultYes }
    return ($a -match "^[yY]")
}

# Track what is set / missing for the final summary.
$script:Summary = [ordered]@{}
function Set-Status($key, $value) { $script:Summary[$key] = $value }

# ===========================================================================
#  Environment helpers
# ===========================================================================
function Set-UserEnv($name, $value) {
    # Persist (User scope, no admin needed) AND apply to this process so later
    # steps and the scripts we call see it immediately.
    [Environment]::SetEnvironmentVariable($name, $value, "User")
    Set-Item -Path "Env:$name" -Value $value
}

function Update-SessionPath {
    # winget-installed tools land in PATH, but only for NEW processes. Rebuild
    # this process's PATH from the registry so we can use them right away.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user | Where-Object { $_ }) -join ";"
}

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# ===========================================================================
#  Steam / DayZ detection (verified live against libraryfolders.vdf)
# ===========================================================================
function Get-SteamPath {
    foreach ($k in @("HKCU:\Software\Valve\Steam", "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam", "HKLM:\SOFTWARE\Valve\Steam")) {
        $p = (Get-ItemProperty $k -ErrorAction SilentlyContinue)
        if ($p.SteamPath)   { return ($p.SteamPath   -replace '/', '\') }
        if ($p.InstallPath) { return ($p.InstallPath  -replace '/', '\') }
    }
    return $null
}

function Get-SteamLibraries($steamPath) {
    $libs = @()
    $vdf = Join-Path $steamPath "steamapps\libraryfolders.vdf"
    if (Test-Path $vdf) {
        $raw = Get-Content $vdf -Raw
        foreach ($m in [regex]::Matches($raw, '"path"\s+"([^"]+)"')) {
            $libs += ($m.Groups[1].Value -replace '\\\\', '\')
        }
    }
    if ($libs.Count -eq 0) { $libs = @($steamPath) }
    return $libs
}

function Find-SteamApp($libs, $appid) {
    foreach ($lib in $libs) {
        if (Test-Path (Join-Path $lib "steamapps\appmanifest_$appid.acf")) { return $lib }
    }
    return $null
}

function Open-SteamInstall($appid) {
    Start-Process "steam://install/$appid" -ErrorAction SilentlyContinue
}

# ===========================================================================
#  BANNER
# ===========================================================================
Clear-Host
Write-Host ""
Write-Host "  ###########################################################" -ForegroundColor Cyan
Write-Host "  #          dayz-ai-survivor  -  Setup wizard              #" -ForegroundColor Cyan
Write-Host "  #              isualc AI  -  game ready in 7 steps        #" -ForegroundColor Cyan
Write-Host "  ###########################################################" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This wizard sets up everything you need to host the AI" -ForegroundColor Gray
Write-Host "  survivors: tools, DayZ paths, workshop mods, server mods" -ForegroundColor Gray
Write-Host "  and the Python packages. Only the API keys and the optional" -ForegroundColor Gray
Write-Host "  Discord bot stay your job - and I walk you through those" -ForegroundColor Gray
Write-Host "  step by step." -ForegroundColor Gray
Write-Host ""
Write-Host "  You can abort any time with Ctrl+C and run it again later -" -ForegroundColor DarkGray
Write-Host "  steps that are already done get skipped." -ForegroundColor DarkGray
Write-Host ""
Write-Host ("  Project folder: {0}" -f $RepoDir) -ForegroundColor DarkGray
if (-not (Ask-YesNo "Start now?")) { Write-Host "Aborted." ; exit 0 }

# ===========================================================================
#  STEP 1: Tools (Python, Node.js, Claude Code CLI)
# ===========================================================================
Write-Phase "Check tools (Python, Node.js, Claude Code CLI)"

if ($SkipTools) {
    Write-Warn "-SkipTools set: skipping the tool installation."
} else {
    $haveWinget = Test-Cmd winget
    if (-not $haveWinget) {
        Write-Warn "winget (App Installer) not found. You will have to install any"
        Write-Warn "missing tools manually - I will tell you which ones."
    }

    # --- Python ---
    if (Test-Cmd python) {
        $pv = (& python --version) 2>&1
        Write-Ok "Python found: $pv"
    } else {
        Write-Info "Python is missing."
        if ($haveWinget -and (Ask-YesNo "Install Python 3.12 now via winget?")) {
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            Update-SessionPath
        }
        if (-not (Test-Cmd python)) {
            Write-Err "Python still not on PATH. Please install it manually:"
            Write-Err "  https://www.python.org/downloads/  (tick 'Add to PATH'!)"
            Write-Err "Then restart this wizard."
        } else { Write-Ok "Python installed." }
    }

    # --- Node.js ---
    if (Test-Cmd node) {
        Write-Ok "Node.js found: $((& node --version) 2>&1)"
    } else {
        Write-Info "Node.js is missing (needed for the Claude Code CLI)."
        if ($haveWinget -and (Ask-YesNo "Install Node.js LTS now via winget?")) {
            winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
            Update-SessionPath
        }
        if (-not (Test-Cmd node)) {
            Write-Err "Node.js still not on PATH. Please install it manually:"
            Write-Err "  https://nodejs.org/  (LTS version), then restart the wizard."
        } else { Write-Ok "Node.js installed." }
    }

    # --- Claude Code CLI ---
    $claudeCli = $null
    if (Test-Cmd npm) {
        try {
            $npmRoot = (& npm root -g) 2>$null
            $cand = Join-Path $npmRoot "@anthropic-ai\claude-code\cli.js"
            if (Test-Path $cand) { $claudeCli = $cand }
        } catch {}
        if (-not $claudeCli) {
            Write-Info "Claude Code CLI is missing."
            if (Ask-YesNo "Install the Claude Code CLI now via npm (global)?") {
                & npm install -g "@anthropic-ai/claude-code"
                try {
                    $npmRoot = (& npm root -g) 2>$null
                    $cand = Join-Path $npmRoot "@anthropic-ai\claude-code\cli.js"
                    if (Test-Path $cand) { $claudeCli = $cand }
                } catch {}
            }
        }
        if ($claudeCli) { Write-Ok "Claude Code CLI: $claudeCli" }
    } else {
        Write-Warn "npm not available (Node.js missing) - Claude Code CLI skipped."
    }

    # Pin the resolved tool paths so the daemons do not have to guess.
    if (Test-Cmd node)   { Set-UserEnv "ISU_NODE_BIN"   (Get-Command node).Source }
    if ($claudeCli)      { Set-UserEnv "ISU_CLAUDE_CLI" $claudeCli }
}
Set-Status "Python"       $(if (Test-Cmd python) { "ok" } else { "MISSING" })
Set-Status "Node.js"      $(if (Test-Cmd node)   { "ok" } else { "MISSING" })
Set-Status "Claude CLI"   $(if ($env:ISU_CLAUDE_CLI) { "ok" } else { "check" })

# ===========================================================================
#  STEP 2: Detect DayZ paths
# ===========================================================================
Write-Phase "Find the DayZ install (Steam, DayZ, DayZ Server)"

$steam = Get-SteamPath
if (-not $steam) {
    Write-Err "Steam not found. Please install Steam first:"
    Write-Err "  https://store.steampowered.com/about/"
    Write-Err "Then restart this wizard."
    Set-Status "DayZ paths" "MISSING (no Steam)"
} else {
    Write-Ok "Steam: $steam"
    $libs = Get-SteamLibraries $steam
    Write-Info ("Steam libraries: {0}" -f ($libs -join ", "))

    # DayZ client (paid - must already be owned/installed)
    $dayzLib = Find-SteamApp $libs $APP_DAYZ
    if ($dayzLib) {
        $dayzDir = Join-Path $dayzLib "steamapps\common\DayZ"
        Set-UserEnv "DAYZ_DIR" $dayzDir
        Set-UserEnv "DAYZ_WORKSHOP_DIR" (Join-Path $dayzLib "steamapps\workshop\content\$WORKSHOP_APP")
        Write-Ok "DayZ (client): $dayzDir"
    } else {
        Write-Warn "DayZ (client) not installed. DayZ is a paid game - you must"
        Write-Warn "own and install it to watch the survivors yourself in-game."
        if (Ask-YesNo "Open the Steam install page for DayZ now?" $false) { Open-SteamInstall $APP_DAYZ }
    }

    # DayZ Server (free)
    $srvLib = Find-SteamApp $libs $APP_SERVER
    if ($srvLib) {
        $serverDir = Join-Path $srvLib "steamapps\common\DayZServer"
        Set-UserEnv "DAYZ_SERVER_DIR" $serverDir
        Write-Ok "DayZ Server: $serverDir"
    } else {
        Write-Warn "DayZ Server (free) not installed - it is required for hosting."
        if (Ask-YesNo "Install DayZ Server now via Steam (opens Steam)?") {
            Open-SteamInstall $APP_SERVER
            Write-Info "Waiting for the server download... (press Enter once Steam is done; 's' = skip)"
            do {
                $a = Read-Host "  Server installed? Enter=re-check, s=skip"
                if ($a -match "^[sS]") { break }
                $libs = Get-SteamLibraries $steam
                $srvLib = Find-SteamApp $libs $APP_SERVER
            } while (-not $srvLib)
            if ($srvLib) {
                $serverDir = Join-Path $srvLib "steamapps\common\DayZServer"
                Set-UserEnv "DAYZ_SERVER_DIR" $serverDir
                Write-Ok "DayZ Server: $serverDir"
            }
        }
    }
    # Fallback workshop dir from server lib if DayZ client lib was not found
    if (-not $env:DAYZ_WORKSHOP_DIR -and $srvLib) {
        Set-UserEnv "DAYZ_WORKSHOP_DIR" (Join-Path $srvLib "steamapps\workshop\content\$WORKSHOP_APP")
    }
    Set-Status "DayZ paths" $(if ($env:DAYZ_SERVER_DIR) { "ok" } else { "incomplete" })
}

# ===========================================================================
#  STEP 3: Subscribe to the workshop mods
# ===========================================================================
Write-Phase "Subscribe to the workshop mods (8 dependencies)"

if ($SkipWorkshop) {
    Write-Warn "-SkipWorkshop set: skipping the workshop subscriptions."
} elseif (-not $env:DAYZ_WORKSHOP_DIR) {
    Write-Warn "Workshop folder unknown (DayZ paths incomplete) - skipped."
} else {
    Write-Info "DayZ cannot subscribe itself - the Steam client does that."
    Write-Info "I will open a helper page with all 8 mods. On each Steam page"
    Write-Info "click 'Subscribe'; Steam then downloads them."
    Write-Host ""
    foreach ($m in $WORKSHOP_MODS) {
        Write-Host ("        - {0,-32} id {1}" -f $m.Name, $m.Id) -ForegroundColor Gray
    }
    Write-Host ""

    # Build a tiny local HTML page with clickable links (hands off to Steam).
    $html = New-Object System.Text.StringBuilder
    [void]$html.AppendLine('<!doctype html><html><head><meta charset="utf-8">')
    [void]$html.AppendLine('<title>dayz-ai-survivor - subscribe to workshop mods</title>')
    [void]$html.AppendLine('<style>body{font-family:Segoe UI,Arial;background:#1b2838;color:#c7d5e0;max-width:760px;margin:30px auto;padding:0 16px}')
    [void]$html.AppendLine('h1{color:#66c0f4}a{display:inline-block;background:#2a475e;color:#fff;text-decoration:none;padding:8px 14px;border-radius:4px;margin:4px 0}a:hover{background:#66c0f4;color:#1b2838}li{margin:10px 0}</style></head><body>')
    [void]$html.AppendLine('<h1>Subscribe to the workshop mods</h1>')
    [void]$html.AppendLine('<p>Click each button and press <b>Subscribe</b> in the Steam window. Steam downloads the mods automatically. Then press Enter back in the setup wizard.</p><ol>')
    foreach ($m in $WORKSHOP_MODS) {
        $url = "https://steamcommunity.com/sharedfiles/filedetails/?id=$($m.Id)"
        [void]$html.AppendLine(("<li><a href=""{0}"" target=""_blank"">{1}</a></li>" -f $url, $m.Name))
    }
    [void]$html.AppendLine('</ol></body></html>')
    $htmlPath = Join-Path $PSScriptRoot "_subscribe.html"
    [System.IO.File]::WriteAllText($htmlPath, $html.ToString(), (New-Object System.Text.UTF8Encoding($true)))
    Start-Process $htmlPath -ErrorAction SilentlyContinue
    Write-Ok "Helper page opened: $htmlPath"
    Write-Host ""
    Write-Info "Now checking which mods have already downloaded."

    # Poll the workshop content folder until all 8 ids are present.
    do {
        $present = @(); $missing = @()
        foreach ($m in $WORKSHOP_MODS) {
            if (Test-Path (Join-Path $env:DAYZ_WORKSHOP_DIR $m.Id)) { $present += $m } else { $missing += $m }
        }
        Write-Host ""
        foreach ($m in $WORKSHOP_MODS) {
            $mark = if (Test-Path (Join-Path $env:DAYZ_WORKSHOP_DIR $m.Id)) { "[OK]  " } else { "[..]  " }
            $col  = if ($mark -eq "[OK]  ") { "Green" } else { "DarkGray" }
            Write-Host ("        {0}{1}" -f $mark, $m.Name) -ForegroundColor $col
        }
        Write-Host ("  {0} of {1} ready." -f $present.Count, $WORKSHOP_MODS.Count) -ForegroundColor Cyan
        if ($missing.Count -eq 0) { Write-Ok "All workshop mods downloaded."; break }
        $a = Read-Host "  Enter=re-check, s=continue anyway"
        if ($a -match "^[sS]") { Write-Warn "Continuing despite missing mods - the server may not start."; break }
    } while ($true)
    Set-Status "Workshop mods" ("{0}/{1}" -f $present.Count, $WORKSHOP_MODS.Count)
}

# ===========================================================================
#  STEP 4: Link the mods + deploy the server mods
# ===========================================================================
Write-Phase "Set up the server (link mods, deploy server mods)"

if (-not $env:DAYZ_SERVER_DIR -or -not (Test-Path $env:DAYZ_SERVER_DIR)) {
    Write-Err "DayZ Server folder missing - this step is skipped."
    Set-Status "Server setup" "MISSING"
} else {
    # 4a) Junctions + bikeys + dev config (the existing one-time setup script).
    #     Run it in a CHILD powershell process: that script calls `exit 1` when
    #     workshop mods are missing, and an `exit` from an &-invoked script would
    #     otherwise terminate THIS wizard too. The child inherits our process env
    #     (DAYZ_SERVER_DIR/DAYZ_WORKSHOP_DIR are already set), so it finds the paths.
    $linkScript = Join-Path $RepoDir "tools\install_mods_to_server.ps1"
    Write-Info "Linking the workshop mods into the server (junctions, bikeys, dev config)..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $linkScript
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Workshop mods linked + dev config deployed."
    } else {
        Write-Warn "install_mods_to_server reported an error (code $LASTEXITCODE)."
        Write-Warn "(Usually just missing workshop mods - redo step 3 and re-run.)"
    }

    # 4b) Deploy the PRE-PACKED server mods. This is the big simplification:
    #     the release ships build\@IsuSurvivor + build\@IsuVoice PBOs, so the
    #     user never needs DayZ Tools or the AddonBuilder.
    $deployed = 0
    foreach ($modName in @("IsuSurvivor", "IsuVoice")) {
        $srcPbo = Join-Path $RepoDir "build\@$modName\addons\$modName.pbo"
        if (-not (Test-Path $srcPbo)) {
            Write-Warn "Bundled PBO missing: $srcPbo"
            Write-Warn "(Shipped inside the release ZIP; from a git clone run tools\pack_mod.ps1 first.)"
            continue
        }
        $dstDir = Join-Path $env:DAYZ_SERVER_DIR "@$modName\addons"
        try {
            New-Item -ItemType Directory -Force $dstDir | Out-Null
            Copy-Item $srcPbo (Join-Path $dstDir "$modName.pbo") -Force -ErrorAction Stop
            # Take the bikey too if present (signature check is off in the dev
            # config, but it is cleaner to have it anyway).
            $keySrc = Join-Path $RepoDir "build\@$modName\keys"
            if (Test-Path $keySrc) {
                $keyDst = Join-Path $env:DAYZ_SERVER_DIR "keys"
                New-Item -ItemType Directory -Force $keyDst | Out-Null
                Copy-Item (Join-Path $keySrc "*.bikey") $keyDst -Force -ErrorAction SilentlyContinue
            }
            Write-Ok "Deployed: @$modName"
            $deployed++
        } catch {
            Write-Warn "@$modName could not be deployed (is the server still running?): $_"
        }
    }
    Set-Status "Server setup" $(if ($deployed -eq 2) { "ok" } else { "$deployed/2 mods" })
}

# ===========================================================================
#  STEP 5: Python packages
# ===========================================================================
Write-Phase "Install the Python packages"

if (-not (Test-Cmd python)) {
    Write-Err "Python missing - this step is skipped (redo step 1)."
    Set-Status "Python packages" "MISSING"
} else {
    $req = Join-Path $RepoDir "requirements.txt"
    if (Test-Path $req) {
        Write-Info "Installing the packages from requirements.txt (may take 1-2 minutes)..."
        & python -m pip install --upgrade pip 2>&1 | Out-Null
        & python -m pip install -r $req
        if ($LASTEXITCODE -eq 0) { Write-Ok "Python packages installed."; Set-Status "Python packages" "ok" }
        else { Write-Warn "pip reported an error - check the output above."; Set-Status "Python packages" "error" }
    } else {
        Write-Warn "requirements.txt not found - skipped."
        Set-Status "Python packages" "?"
    }
}

# ===========================================================================
#  STEP 6: Claude login (the brain)
# ===========================================================================
Write-Phase "Sign in to Claude (the survivors' brain)"

Write-Info "Claude Code is the engine behind the AI survivors. Two ways:"
Write-Host "        A) Max/Pro plan: in a terminal, type  claude  then  /login  -" -ForegroundColor Gray
Write-Host "           no per-turn cost (for the models sonnet/opus/haiku)." -ForegroundColor Gray
Write-Host "        B) API key (pay per token): pick an 'api/' model in-game." -ForegroundColor Gray
Write-Host ""
if ($env:ANTHROPIC_API_KEY) {
    Write-Ok "ANTHROPIC_API_KEY is already set."
} else {
    if (Ask-YesNo "Do you have a Max/Pro plan and want to sign in via /login (way A)?" $true) {
        Write-Info "After the wizard, open a NEW terminal and type:  claude"
        Write-Info "Then  /login  and follow the browser flow. No key needed."
    } else {
        $k = Read-Host "  Enter ANTHROPIC_API_KEY (empty = skip)"
        if ($k) { Set-UserEnv "ANTHROPIC_API_KEY" $k.Trim(); Write-Ok "API key saved." }
    }
}
Set-Status "Claude login" $(if ($env:ANTHROPIC_API_KEY) { "API key" } else { "plan /login (manual)" })

# ===========================================================================
#  STEP 7: Optional extras (cloud models, voice, Discord)
# ===========================================================================
Write-Phase "Optional extras (guided) - cloud models, voice, Discord"

# 7a) Cloud LLM provider keys
if (Ask-YesNo "Use other models (OpenAI/Gemini/Grok) in the arena menu?" $false) {
    $sk = Join-Path $RepoDir "tools\set_api_keys.ps1"
    if (Test-Path $sk) { & $sk } else { Write-Warn "set_api_keys.ps1 not found." }
}

# 7b) ElevenLabs voice (TTS + microphone STT)
if ($env:ELEVENLABS_API_KEY) {
    Write-Ok "ELEVENLABS_API_KEY is already set (voice + mic active)."
} elseif (Ask-YesNo "Enable voices + microphone listening (ElevenLabs key)?" $false) {
    Write-Info "Get a key: https://elevenlabs.io  ->  Profile  ->  API Keys"
    Start-Process "https://elevenlabs.io/app/settings/api-keys" -ErrorAction SilentlyContinue
    $k = Read-Host "  Enter ELEVENLABS_API_KEY (empty = skip)"
    if ($k) { Set-UserEnv "ELEVENLABS_API_KEY" $k.Trim(); Write-Ok "ElevenLabs key saved." }
}
Set-Status "Voice (ElevenLabs)" $(if ($env:ELEVENLABS_API_KEY) { "ok" } else { "off" })

# 7c) Discord voice bot
if ($env:DISCORD_BOT_TOKEN) {
    Write-Ok "DISCORD_BOT_TOKEN is already set."
} elseif (Ask-YesNo "Set up Discord voice output (your own bot)?" $false) {
    $doc = Join-Path $RepoDir "docs\discord_bot_setup_en.md"
    if (-not (Test-Path $doc)) { $doc = Join-Path $RepoDir "docs\discord_bot_setup.md" }
    Write-Info "Guide (create the bot + get the token):"
    Write-Info "  $doc"
    if (Test-Path $doc) { Start-Process $doc -ErrorAction SilentlyContinue }
    Start-Process "https://discord.com/developers/applications" -ErrorAction SilentlyContinue
    Write-Info "In short: New Application -> Bot -> copy token -> OAuth2 URL Generator"
    Write-Info "(scope 'bot', permissions 'Connect'+'Speak') -> invite the bot to your server."
    $k = Read-Host "  Enter DISCORD_BOT_TOKEN (empty = skip)"
    if ($k) { Set-UserEnv "DISCORD_BOT_TOKEN" $k.Trim(); Write-Ok "Discord token saved." }
}
Set-Status "Discord" $(if ($env:DISCORD_BOT_TOKEN) { "ok" } else { "off" })

# ===========================================================================
#  DONE
# ===========================================================================
Write-Host ""
Write-Host ("=" * 74) -ForegroundColor DarkGreen
Write-Host "  DONE - summary" -ForegroundColor Green
Write-Host ("=" * 74) -ForegroundColor DarkGreen
foreach ($k in $script:Summary.Keys) {
    $v = $script:Summary[$k]
    $col = if ($v -match "MISSING|error") { "Red" } elseif ($v -match "off|manual|check|\?|incomplete") { "Yellow" } else { "Green" }
    Write-Host ("  {0,-22} {1}" -f ($k + ":"), $v) -ForegroundColor $col
}
Write-Host ""
Write-Host "  IMPORTANT: already-open windows do NOT see the new variables." -ForegroundColor Yellow
Write-Host "  If you still need to sign in via plan: new terminal -> 'claude' -> /login" -ForegroundColor Yellow
Write-Host ""
Write-Host "  How to play:" -ForegroundColor Cyan
Write-Host "    1. Run start_game.bat (server + supervisor + client, pick a map)" -ForegroundColor Gray
Write-Host "    2. Join in-game (127.0.0.1:2302) and press Insert for the arena menu" -ForegroundColor Gray
Write-Host "    3. Pick agents/models/alignment -> START" -ForegroundColor Gray
Write-Host ""

if (-not $NoLaunch) {
    if (Ask-YesNo "Start start_game.bat now?" $false) {
        $bat = Join-Path $RepoDir "start_game.bat"
        if (Test-Path $bat) { Start-Process $bat -WorkingDirectory $RepoDir }
        else { Write-Warn "start_game.bat not found." }
    }
}
Write-Host "Have fun. - isualc AI" -ForegroundColor Cyan
exit 0
