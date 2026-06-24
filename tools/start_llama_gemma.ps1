# Startet llama-server mit Gemma 4 E4B (lokal, GRATIS) als Anthropic-
# kompatibles Backend fuer die Agenten (Modell "local/gemma-4-E4B-it" im
# Arena-Menue). llama-server hat /v1/messages nativ; Tool-Calls brauchen
# --jinja und einen Build >= b8641 (Gemma-4-Template-Fixes, 2026-04).
# Wird vom arena_supervisor automatisch gestartet. ASCII only (PS 5.1).
#
# llama-server.exe wird so gesucht:
#   1. Umgebungsvariable ISU_LLAMA_SERVER (voller Pfad zur exe)
#   2. llama-server im PATH
#   3. tools\llama-bin\ (von diesem Skript heruntergeladen)
#   4. sonst: automatischer Download des aktuellen win-vulkan-x64-Builds

param(
    [int]$Port = 8080,
    # Grundprompt von Claude Code (System + dayz-Tools + Persona) ist mit
    # --strict-mcp-config ~25k Tokens; 48k laesst Luft fuer lange Sessions.
    # 128k hat auf der GPU den KV-Aufbau gesprengt (Server-Crash beim
    # Prompt-Processing) - nicht hochdrehen, ausser die GPU ist gross.
    [int]$Ctx = 49152,
    [string]$ModelUrl = "https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf",
    [string]$ModelFile = "gemma-4-E4B-it-Q4_K_M.gguf",
    [string]$Alias = "gemma-4-E4B-it"
)

$repo = Split-Path $PSScriptRoot -Parent
$modelsDir = Join-Path $repo "models"
$binDir = Join-Path $PSScriptRoot "llama-bin"
$modelPath = Join-Path $modelsDir $ModelFile

$ProgressPreference = "SilentlyContinue"  # PS-5.1-Progress-Bar bremst Downloads massiv

function Test-Port([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $null
    try {
        $iar = $client.BeginConnect("127.0.0.1", $p, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(1000) -and $client.Connected) { return $true }
        return $false
    } catch { return $false } finally {
        if ($iar) { $iar.AsyncWaitHandle.Close() }
        $client.Close()
    }
}

Write-Host "=== ISU SURVIVOR - llama-server (Gemma 4 E4B, lokal) ===" -ForegroundColor Cyan

if (Test-Port $Port) {
    Write-Host "llama-server laeuft schon auf Port $Port - nichts zu tun."
    exit 0
}

# 1) llama-server.exe finden
$exe = $null
if ($env:ISU_LLAMA_SERVER -and (Test-Path $env:ISU_LLAMA_SERVER)) {
    $exe = $env:ISU_LLAMA_SERVER
    Write-Host "[1/3] llama-server aus ISU_LLAMA_SERVER: $exe"
    Write-Host "      HINWEIS: Build muss >= b8641 sein (Gemma-4-Tool-Call-Fixes)." -ForegroundColor Yellow
}
if ($null -eq $exe) {
    $cmd = Get-Command llama-server -ErrorAction SilentlyContinue
    if ($cmd) {
        $exe = $cmd.Source
        Write-Host "[1/3] llama-server im PATH: $exe"
        Write-Host "      HINWEIS: Build muss >= b8641 sein (Gemma-4-Tool-Call-Fixes)." -ForegroundColor Yellow
    }
}
if ($null -eq $exe) {
    $local = Get-ChildItem $binDir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($local) {
        $exe = $local.FullName
        Write-Host "[1/3] llama-server aus tools\llama-bin: $exe"
    }
}
if ($null -eq $exe) {
    Write-Host "[1/3] llama-server fehlt - lade aktuellen Vulkan-Build von GitHub..."
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" -UseBasicParsing
        $asset = $rel.assets | Where-Object { $_.name -match "bin-win-vulkan-x64\.zip$" } | Select-Object -First 1
        if ($null -eq $asset) { throw "Kein win-vulkan-x64-Asset im Release $($rel.tag_name)." }
        New-Item -ItemType Directory -Force $binDir | Out-Null
        $zip = Join-Path $binDir $asset.name
        Write-Host "      $($asset.name) ($([math]::Round($asset.size/1MB)) MB)..."
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
        Expand-Archive $zip -DestinationPath $binDir -Force
        Remove-Item $zip -Force -Confirm:$false
        $local = Get-ChildItem $binDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
        if ($null -eq $local) { throw "llama-server.exe nicht im Zip gefunden." }
        $exe = $local.FullName
        Write-Host "      OK: $exe (Build $($rel.tag_name))"
    } catch {
        Write-Host "FEHLER beim Binary-Download: $_" -ForegroundColor Red
        Write-Host "Manuell: https://github.com/ggml-org/llama.cpp/releases (llama-*-bin-win-vulkan-x64.zip)" -ForegroundColor Yellow
        Write-Host "nach tools\llama-bin entpacken ODER ISU_LLAMA_SERVER auf deine exe zeigen lassen." -ForegroundColor Yellow
        pause; exit 1
    }
}

# 2) GGUF laden, falls es fehlt (4,98 GB, ungated, apache-2.0)
if (Test-Path $modelPath) {
    $gb = [math]::Round((Get-Item $modelPath).Length / 1GB, 2)
    Write-Host "[2/3] Modell vorhanden: $modelPath ($gb GB)"
} else {
    Write-Host "[2/3] Lade Gemma 4 E4B Q4_K_M (4,98 GB) - das dauert ein paar Minuten..."
    New-Item -ItemType Directory -Force $modelsDir | Out-Null
    $tmp = "$modelPath.part"
    # Laeuft schon ein Download (anderes Fenster)? Frische .part = ja.
    if (Test-Path $tmp) {
        $ageMin = ((Get-Date) - (Get-Item $tmp).LastWriteTime).TotalMinutes
        if ($ageMin -lt 2) {
            Write-Host "Download laeuft offenbar schon in einem anderen Fenster (.part ist $([math]::Round($ageMin, 1)) min alt) - beende." -ForegroundColor Yellow
            exit 1
        }
        Remove-Item $tmp -Force -Confirm:$false  # alter Abbruch-Rest
    }
    $bitsOk = $false
    try {
        Start-BitsTransfer -Source $ModelUrl -Destination $tmp -DisplayName "Gemma 4 E4B" -ErrorAction Stop
        $bitsOk = $true
    } catch {
        Write-Host "      BITS fehlgeschlagen ($_) - Fallback auf curl.exe..." -ForegroundColor Yellow
    }
    if (-not $bitsOk) {
        # KEIN Invoke-WebRequest: PS 5.1 puffert die Response komplett im
        # RAM (MemoryStream, 2-GB-Limit) - bei 5 GB deterministischer OOM.
        # curl.exe (Windows 11 System32) streamt direkt auf die Platte.
        $curl = Join-Path $env:SystemRoot "System32\curl.exe"
        & $curl -L --fail --retry 3 -o $tmp $ModelUrl
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FEHLER beim Modell-Download (curl exit $LASTEXITCODE)." -ForegroundColor Red
            if (Test-Path $tmp) { Remove-Item $tmp -Force -Confirm:$false }
            pause; exit 1
        }
    }
    Move-Item $tmp $modelPath -Force
    Write-Host "      OK: $modelPath"
}

# 3) Server starten (Vordergrund - dieses Fenster IST der Server)
#    Sampling = Google-Defaults fuer Gemma 4 (temp 1.0, top-p 0.95, top-k 64).
#    q8-KV-Cache + Flash Attention halten den 128k-Kontext bezahlbar
#    (Gemma nutzt Sliding-Window-Attention, der KV-Cache bleibt moderat).
Write-Host "[3/3] Starte llama-server auf http://127.0.0.1:$Port (Kontext $Ctx)..."
Write-Host "      Fenster offen lassen - STRG+C beendet den Server." -ForegroundColor Yellow
Write-Host "      Der ERSTE Zug eines Agenten verarbeitet ~77k Prompt-Tokens" -ForegroundColor Yellow
Write-Host "      und kann 1-3 Minuten dauern; danach greift der Prompt-Cache." -ForegroundColor Yellow
& $exe -m $modelPath `
    --jinja `
    -ngl 99 `
    -c $Ctx `
    -b 4096 -ub 2048 `
    --parallel 1 `
    --flash-attn on `
    --cache-type-k q8_0 `
    --cache-type-v q8_0 `
    --kv-unified `
    --temp 1.0 --top-p 0.95 --top-k 64 `
    --alias $Alias `
    --host 127.0.0.1 --port $Port
# Bei Crash/Fehlstart: Fenster offen halten, damit die Ursache lesbar ist
Write-Host ""
Write-Host "llama-server wurde beendet (Exit $LASTEXITCODE)." -ForegroundColor Yellow
Write-Host "Bei 'out of memory': Skript mit -Ctx 98304 (oder 65536) starten."
pause
