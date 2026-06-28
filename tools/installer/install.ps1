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
#  ASCII only (PowerShell 5.1 safe), German UI with ae/oe/ue substitutes to
#  match the other tools\*.ps1 scripts.
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
    Write-Host (" SCHRITT {0}: {1}" -f $script:StepNo, $text) -ForegroundColor Cyan
    Write-Host ("=" * 74) -ForegroundColor DarkCyan
}
function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Info($text) { Write-Host "  [..]   $text" -ForegroundColor Gray }
function Write-Warn($text) { Write-Host "  [WARN] $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "  [FEHL] $text" -ForegroundColor Red }
function Ask-YesNo($question, $defaultYes = $true) {
    $hint = if ($defaultYes) { "(J/n)" } else { "(j/N)" }
    $a = Read-Host "  $question $hint"
    if (-not $a) { return $defaultYes }
    return ($a -match "^[jJyY]")
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
Write-Host "  #          dayz-ai-survivor  -  Setup-Assistent           #" -ForegroundColor Cyan
Write-Host "  #              isualc AI  -  game ready in 7 Schritten     #" -ForegroundColor Cyan
Write-Host "  ###########################################################" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Dieser Assistent richtet alles ein, was du zum Hosten der KI-" -ForegroundColor Gray
Write-Host "  Survivor brauchst: Werkzeuge, DayZ-Pfade, Workshop-Mods, Server-" -ForegroundColor Gray
Write-Host "  Mods und die Python-Pakete. Nur die API-Keys und der optionale" -ForegroundColor Gray
Write-Host "  Discord-Bot bleiben deine Sache - dabei fuehre ich dich Schritt" -ForegroundColor Gray
Write-Host "  fuer Schritt durch." -ForegroundColor Gray
Write-Host ""
Write-Host "  Du kannst den Assistenten jederzeit mit Strg+C abbrechen und" -ForegroundColor DarkGray
Write-Host "  spaeter erneut starten - schon erledigte Schritte werden" -ForegroundColor DarkGray
Write-Host "  uebersprungen." -ForegroundColor DarkGray
Write-Host ""
Write-Host ("  Projektordner: {0}" -f $RepoDir) -ForegroundColor DarkGray
if (-not (Ask-YesNo "Jetzt starten?")) { Write-Host "Abgebrochen." ; exit 0 }

# ===========================================================================
#  SCHRITT 1: Werkzeuge (Python, Node.js, Claude Code CLI)
# ===========================================================================
Write-Phase "Werkzeuge pruefen (Python, Node.js, Claude Code CLI)"

if ($SkipTools) {
    Write-Warn "-SkipTools gesetzt: ueberspringe die Werkzeug-Installation."
} else {
    $haveWinget = Test-Cmd winget
    if (-not $haveWinget) {
        Write-Warn "winget (App Installer) nicht gefunden. Fehlende Werkzeuge musst"
        Write-Warn "du dann manuell installieren - ich sage gleich, welche."
    }

    # --- Python ---
    if (Test-Cmd python) {
        $pv = (& python --version) 2>&1
        Write-Ok "Python gefunden: $pv"
    } else {
        Write-Info "Python fehlt."
        if ($haveWinget -and (Ask-YesNo "Python 3.12 jetzt per winget installieren?")) {
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            Update-SessionPath
        }
        if (-not (Test-Cmd python)) {
            Write-Err "Python weiterhin nicht im PATH. Bitte manuell installieren:"
            Write-Err "  https://www.python.org/downloads/  (Haken 'Add to PATH' setzen!)"
            Write-Err "Danach diesen Assistenten neu starten."
        } else { Write-Ok "Python installiert." }
    }

    # --- Node.js ---
    if (Test-Cmd node) {
        Write-Ok "Node.js gefunden: $((& node --version) 2>&1)"
    } else {
        Write-Info "Node.js fehlt (wird fuer die Claude Code CLI gebraucht)."
        if ($haveWinget -and (Ask-YesNo "Node.js LTS jetzt per winget installieren?")) {
            winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
            Update-SessionPath
        }
        if (-not (Test-Cmd node)) {
            Write-Err "Node.js weiterhin nicht im PATH. Bitte manuell installieren:"
            Write-Err "  https://nodejs.org/  (LTS-Version), danach Assistent neu starten."
        } else { Write-Ok "Node.js installiert." }
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
            Write-Info "Claude Code CLI fehlt."
            if (Ask-YesNo "Claude Code CLI jetzt per npm global installieren?") {
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
        Write-Warn "npm nicht verfuegbar (Node.js fehlt) - Claude Code CLI uebersprungen."
    }

    # Pin the resolved tool paths so the daemons do not have to guess.
    if (Test-Cmd node)   { Set-UserEnv "ISU_NODE_BIN"   (Get-Command node).Source }
    if ($claudeCli)      { Set-UserEnv "ISU_CLAUDE_CLI" $claudeCli }
}
Set-Status "Python"       $(if (Test-Cmd python) { "ok" } else { "FEHLT" })
Set-Status "Node.js"      $(if (Test-Cmd node)   { "ok" } else { "FEHLT" })
Set-Status "Claude CLI"   $(if ($env:ISU_CLAUDE_CLI) { "ok" } else { "pruefen" })

# ===========================================================================
#  SCHRITT 2: DayZ-Pfade erkennen
# ===========================================================================
Write-Phase "DayZ-Installation finden (Steam, DayZ, DayZ Server)"

$steam = Get-SteamPath
if (-not $steam) {
    Write-Err "Steam nicht gefunden. Bitte zuerst Steam installieren:"
    Write-Err "  https://store.steampowered.com/about/"
    Write-Err "Danach diesen Assistenten neu starten."
    Set-Status "DayZ-Pfade" "FEHLT (kein Steam)"
} else {
    Write-Ok "Steam: $steam"
    $libs = Get-SteamLibraries $steam
    Write-Info ("Steam-Bibliotheken: {0}" -f ($libs -join ", "))

    # DayZ client (paid - must already be owned/installed)
    $dayzLib = Find-SteamApp $libs $APP_DAYZ
    if ($dayzLib) {
        $dayzDir = Join-Path $dayzLib "steamapps\common\DayZ"
        Set-UserEnv "DAYZ_DIR" $dayzDir
        Set-UserEnv "DAYZ_WORKSHOP_DIR" (Join-Path $dayzLib "steamapps\workshop\content\$WORKSHOP_APP")
        Write-Ok "DayZ (Client): $dayzDir"
    } else {
        Write-Warn "DayZ (Client) nicht installiert. DayZ ist kostenpflichtig - du"
        Write-Warn "musst es besitzen und installieren, um spaeter selbst zuzuschauen."
        if (Ask-YesNo "Steam-Installationsseite fuer DayZ jetzt oeffnen?" $false) { Open-SteamInstall $APP_DAYZ }
    }

    # DayZ Server (free)
    $srvLib = Find-SteamApp $libs $APP_SERVER
    if ($srvLib) {
        $serverDir = Join-Path $srvLib "steamapps\common\DayZServer"
        Set-UserEnv "DAYZ_SERVER_DIR" $serverDir
        Write-Ok "DayZ Server: $serverDir"
    } else {
        Write-Warn "DayZ Server (kostenlos) nicht installiert - er wird zum Hosten gebraucht."
        if (Ask-YesNo "DayZ Server jetzt ueber Steam installieren (oeffnet Steam)?") {
            Open-SteamInstall $APP_SERVER
            Write-Info "Warte auf den Server-Download... (Enter druecken, sobald Steam fertig ist; 's' = ueberspringen)"
            do {
                $a = Read-Host "  Server installiert? Enter=pruefen, s=ueberspringen"
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
    Set-Status "DayZ-Pfade" $(if ($env:DAYZ_SERVER_DIR) { "ok" } else { "unvollstaendig" })
}

# ===========================================================================
#  SCHRITT 3: Workshop-Mods abonnieren
# ===========================================================================
Write-Phase "Workshop-Mods abonnieren (8 Abhaengigkeiten)"

if ($SkipWorkshop) {
    Write-Warn "-SkipWorkshop gesetzt: ueberspringe das Abo der Workshop-Mods."
} elseif (-not $env:DAYZ_WORKSHOP_DIR) {
    Write-Warn "Workshop-Ordner unbekannt (DayZ-Pfade unvollstaendig) - uebersprungen."
} else {
    Write-Info "DayZ kann sich nicht selbst abonnieren - das macht der Steam-Client."
    Write-Info "Ich oeffne eine Hilfsseite mit allen 8 Mods. Auf jeder Steam-Seite"
    Write-Info "auf 'Abonnieren' klicken; Steam laedt sie dann herunter."
    Write-Host ""
    foreach ($m in $WORKSHOP_MODS) {
        Write-Host ("        - {0,-32} id {1}" -f $m.Name, $m.Id) -ForegroundColor Gray
    }
    Write-Host ""

    # Build a tiny local HTML page with clickable links (hands off to Steam).
    $html = New-Object System.Text.StringBuilder
    [void]$html.AppendLine('<!doctype html><html><head><meta charset="utf-8">')
    [void]$html.AppendLine('<title>dayz-ai-survivor - Workshop-Mods abonnieren</title>')
    [void]$html.AppendLine('<style>body{font-family:Segoe UI,Arial;background:#1b2838;color:#c7d5e0;max-width:760px;margin:30px auto;padding:0 16px}')
    [void]$html.AppendLine('h1{color:#66c0f4}a{display:inline-block;background:#2a475e;color:#fff;text-decoration:none;padding:8px 14px;border-radius:4px;margin:4px 0}a:hover{background:#66c0f4;color:#1b2838}li{margin:10px 0}</style></head><body>')
    [void]$html.AppendLine('<h1>Workshop-Mods abonnieren</h1>')
    [void]$html.AppendLine('<p>Auf jeden Button klicken und im Steam-Fenster <b>Abonnieren</b> druecken. Steam laedt die Mods automatisch herunter. Danach im Setup-Assistenten Enter druecken.</p><ol>')
    foreach ($m in $WORKSHOP_MODS) {
        $url = "https://steamcommunity.com/sharedfiles/filedetails/?id=$($m.Id)"
        [void]$html.AppendLine(("<li><a href=""{0}"" target=""_blank"">{1}</a></li>" -f $url, $m.Name))
    }
    [void]$html.AppendLine('</ol></body></html>')
    $htmlPath = Join-Path $PSScriptRoot "_subscribe.html"
    [System.IO.File]::WriteAllText($htmlPath, $html.ToString(), (New-Object System.Text.UTF8Encoding($true)))
    Start-Process $htmlPath -ErrorAction SilentlyContinue
    Write-Ok "Hilfsseite geoeffnet: $htmlPath"
    Write-Host ""
    Write-Info "Ich pruefe jetzt, welche Mods schon heruntergeladen sind."

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
        Write-Host ("  {0} von {1} bereit." -f $present.Count, $WORKSHOP_MODS.Count) -ForegroundColor Cyan
        if ($missing.Count -eq 0) { Write-Ok "Alle Workshop-Mods heruntergeladen."; break }
        $a = Read-Host "  Enter=erneut pruefen, s=trotzdem weiter"
        if ($a -match "^[sS]") { Write-Warn "Weiter trotz fehlender Mods - der Server startet evtl. nicht."; break }
    } while ($true)
    Set-Status "Workshop-Mods" ("{0}/{1}" -f $present.Count, $WORKSHOP_MODS.Count)
}

# ===========================================================================
#  SCHRITT 4: Server verlinken + Server-Mods deployen
# ===========================================================================
Write-Phase "Server einrichten (Mods verlinken, Server-Mods deployen)"

if (-not $env:DAYZ_SERVER_DIR -or -not (Test-Path $env:DAYZ_SERVER_DIR)) {
    Write-Err "DayZ-Server-Ordner fehlt - dieser Schritt wird uebersprungen."
    Set-Status "Server-Setup" "FEHLT"
} else {
    # 4a) Junctions + bikeys + dev config (the existing one-time setup script).
    #     Run it in a CHILD powershell process: that script calls `exit 1` when
    #     workshop mods are missing, and an `exit` from an &-invoked script would
    #     otherwise terminate THIS wizard too. The child inherits our process env
    #     (DAYZ_SERVER_DIR/DAYZ_WORKSHOP_DIR are already set), so it finds the paths.
    $linkScript = Join-Path $RepoDir "tools\install_mods_to_server.ps1"
    Write-Info "Verlinke Workshop-Mods in den Server (Junctions, Bikeys, Dev-Config)..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $linkScript
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Workshop-Mods verlinkt + Dev-Config deployt."
    } else {
        Write-Warn "install_mods_to_server meldete einen Fehler (Code $LASTEXITCODE)."
        Write-Warn "(Meist nur fehlende Workshop-Mods - Schritt 3 nachholen und neu starten.)"
    }

    # 4b) Deploy the PRE-PACKED server mods. This is the big simplification:
    #     the release ships build\@IsuSurvivor + build\@IsuVoice PBOs, so the
    #     user never needs DayZ Tools or the AddonBuilder.
    $deployed = 0
    foreach ($modName in @("IsuSurvivor", "IsuVoice")) {
        $srcPbo = Join-Path $RepoDir "build\@$modName\addons\$modName.pbo"
        if (-not (Test-Path $srcPbo)) {
            Write-Warn "Mitgelieferte PBO fehlt: $srcPbo"
            Write-Warn "(Im Release-ZIP enthalten; bei einem Git-Klon erst tools\pack_mod.ps1 laufen lassen.)"
            continue
        }
        $dstDir = Join-Path $env:DAYZ_SERVER_DIR "@$modName\addons"
        try {
            New-Item -ItemType Directory -Force $dstDir | Out-Null
            Copy-Item $srcPbo (Join-Path $dstDir "$modName.pbo") -Force -ErrorAction Stop
            # bikey mitnehmen, falls vorhanden (Signaturpruefung ist im Dev-Cfg aus,
            # aber sauberer ist es trotzdem).
            $keySrc = Join-Path $RepoDir "build\@$modName\keys"
            if (Test-Path $keySrc) {
                $keyDst = Join-Path $env:DAYZ_SERVER_DIR "keys"
                New-Item -ItemType Directory -Force $keyDst | Out-Null
                Copy-Item (Join-Path $keySrc "*.bikey") $keyDst -Force -ErrorAction SilentlyContinue
            }
            Write-Ok "Deployt: @$modName"
            $deployed++
        } catch {
            Write-Warn "@$modName konnte nicht deployt werden (laeuft der Server noch?): $_"
        }
    }
    Set-Status "Server-Setup" $(if ($deployed -eq 2) { "ok" } else { "$deployed/2 Mods" })
}

# ===========================================================================
#  SCHRITT 5: Python-Pakete
# ===========================================================================
Write-Phase "Python-Pakete installieren"

if (-not (Test-Cmd python)) {
    Write-Err "Python fehlt - dieser Schritt wird uebersprungen (Schritt 1 nachholen)."
    Set-Status "Python-Pakete" "FEHLT"
} else {
    $req = Join-Path $RepoDir "requirements.txt"
    if (Test-Path $req) {
        Write-Info "Installiere die Pakete aus requirements.txt (kann 1-2 Minuten dauern)..."
        & python -m pip install --upgrade pip 2>&1 | Out-Null
        & python -m pip install -r $req
        if ($LASTEXITCODE -eq 0) { Write-Ok "Python-Pakete installiert."; Set-Status "Python-Pakete" "ok" }
        else { Write-Warn "pip meldete einen Fehler - Ausgabe oben pruefen."; Set-Status "Python-Pakete" "Fehler" }
    } else {
        Write-Warn "requirements.txt nicht gefunden - uebersprungen."
        Set-Status "Python-Pakete" "?"
    }
}

# ===========================================================================
#  SCHRITT 6: Claude-Anmeldung (das Gehirn)
# ===========================================================================
Write-Phase "Claude anmelden (das Gehirn der Survivor)"

Write-Info "Claude Code ist der Motor der KI-Survivor. Es gibt zwei Wege:"
Write-Host "        A) Max/Pro-Abo: im Terminal einmal  claude  tippen und  /login  -" -ForegroundColor Gray
Write-Host "           keine Kosten pro Zug (fuer die Modelle sonnet/opus/haiku)." -ForegroundColor Gray
Write-Host "        B) API-Key (Abrechnung pro Token): waehlt im Spiel ein 'api/'-Modell." -ForegroundColor Gray
Write-Host ""
if ($env:ANTHROPIC_API_KEY) {
    Write-Ok "ANTHROPIC_API_KEY ist bereits gesetzt."
} else {
    if (Ask-YesNo "Hast du ein Max/Pro-Abo und willst dich per /login anmelden (Weg A)?" $true) {
        Write-Info "Oeffne dazu nach dem Assistenten ein neues Terminal und tippe:  claude"
        Write-Info "Dann  /login  und dem Browser-Flow folgen. Kein Key noetig."
    } else {
        $k = Read-Host "  ANTHROPIC_API_KEY eingeben (leer = ueberspringen)"
        if ($k) { Set-UserEnv "ANTHROPIC_API_KEY" $k.Trim(); Write-Ok "API-Key gespeichert." }
    }
}
Set-Status "Claude-Login" $(if ($env:ANTHROPIC_API_KEY) { "API-Key" } else { "Abo /login (manuell)" })

# ===========================================================================
#  SCHRITT 7: Optionale Extras (Cloud-Modelle, Stimme, Discord)
# ===========================================================================
Write-Phase "Optionale Extras (gefuehrt) - Cloud-Modelle, Stimme, Discord"

# 7a) Cloud LLM provider keys
if (Ask-YesNo "Fremd-Modelle (OpenAI/Gemini/Grok) im Arena-Menue nutzen?" $false) {
    $sk = Join-Path $RepoDir "tools\set_api_keys.ps1"
    if (Test-Path $sk) { & $sk } else { Write-Warn "set_api_keys.ps1 nicht gefunden." }
}

# 7b) ElevenLabs voice (TTS + microphone STT)
if ($env:ELEVENLABS_API_KEY) {
    Write-Ok "ELEVENLABS_API_KEY ist bereits gesetzt (Stimme + Mikro aktiv)."
} elseif (Ask-YesNo "Stimmen + Mikrofon-Hoeren aktivieren (ElevenLabs-Key)?" $false) {
    Write-Info "Key holen: https://elevenlabs.io  ->  Profile  ->  API Keys"
    Start-Process "https://elevenlabs.io/app/settings/api-keys" -ErrorAction SilentlyContinue
    $k = Read-Host "  ELEVENLABS_API_KEY eingeben (leer = ueberspringen)"
    if ($k) { Set-UserEnv "ELEVENLABS_API_KEY" $k.Trim(); Write-Ok "ElevenLabs-Key gespeichert." }
}
Set-Status "Stimme (ElevenLabs)" $(if ($env:ELEVENLABS_API_KEY) { "ok" } else { "aus" })

# 7c) Discord voice bot
if ($env:DISCORD_BOT_TOKEN) {
    Write-Ok "DISCORD_BOT_TOKEN ist bereits gesetzt."
} elseif (Ask-YesNo "Discord-Sprachausgabe einrichten (eigener Bot)?" $false) {
    $doc = Join-Path $RepoDir "docs\discord_bot_setup_en.md"
    if (-not (Test-Path $doc)) { $doc = Join-Path $RepoDir "docs\discord_bot_setup.md" }
    Write-Info "Anleitung (Bot anlegen + Token holen):"
    Write-Info "  $doc"
    if (Test-Path $doc) { Start-Process $doc -ErrorAction SilentlyContinue }
    Start-Process "https://discord.com/developers/applications" -ErrorAction SilentlyContinue
    Write-Info "Kurz: New Application -> Bot -> Token kopieren -> OAuth2 URL Generator"
    Write-Info "(Scope 'bot', Rechte 'Connect'+'Speak') -> Bot auf deinen Server einladen."
    $k = Read-Host "  DISCORD_BOT_TOKEN eingeben (leer = ueberspringen)"
    if ($k) { Set-UserEnv "DISCORD_BOT_TOKEN" $k.Trim(); Write-Ok "Discord-Token gespeichert." }
}
Set-Status "Discord" $(if ($env:DISCORD_BOT_TOKEN) { "ok" } else { "aus" })

# ===========================================================================
#  ABSCHLUSS
# ===========================================================================
Write-Host ""
Write-Host ("=" * 74) -ForegroundColor DarkGreen
Write-Host "  FERTIG - Zusammenfassung" -ForegroundColor Green
Write-Host ("=" * 74) -ForegroundColor DarkGreen
foreach ($k in $script:Summary.Keys) {
    $v = $script:Summary[$k]
    $col = if ($v -match "FEHLT|Fehler") { "Red" } elseif ($v -match "aus|manuell|pruefen|\?|unvoll") { "Yellow" } else { "Green" }
    Write-Host ("  {0,-22} {1}" -f ($k + ":"), $v) -ForegroundColor $col
}
Write-Host ""
Write-Host "  WICHTIG: Schon offene Fenster sehen die neuen Variablen NICHT." -ForegroundColor Yellow
Write-Host "  Falls du dich noch per Abo anmelden musst: neues Terminal -> 'claude' -> /login" -ForegroundColor Yellow
Write-Host ""
Write-Host "  So spielst du:" -ForegroundColor Cyan
Write-Host "    1. start_game.bat starten (Server + Supervisor + Client, Karte waehlen)" -ForegroundColor Gray
Write-Host "    2. Im Spiel beitreten (127.0.0.1:2302) und Taste Einfg fuer das Arena-Menue" -ForegroundColor Gray
Write-Host "    3. Agenten/Modelle/Gesinnung waehlen -> START" -ForegroundColor Gray
Write-Host ""

if (-not $NoLaunch) {
    if (Ask-YesNo "start_game.bat jetzt starten?" $false) {
        $bat = Join-Path $RepoDir "start_game.bat"
        if (Test-Path $bat) { Start-Process $bat -WorkingDirectory $RepoDir }
        else { Write-Warn "start_game.bat nicht gefunden." }
    }
}
Write-Host "Viel Spass. - isualc AI" -ForegroundColor Cyan
exit 0
