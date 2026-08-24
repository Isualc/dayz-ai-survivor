# Startet den claude-code-router (Cloud-Gateway fuer OpenAI / Google Gemini /
# xAI Grok). Die Agenten-Runner zeigen dann per ANTHROPIC_BASE_URL auf
# http://127.0.0.1:3456 und waehlen das Modell als "provider,modell".
# Wird vom arena_supervisor automatisch gestartet, wenn ein openai/, google/
# oder xai/-Modell gewaehlt ist. ASCII only (PowerShell 5.1 safe).
#
# API-Keys kommen aus den Umgebungsvariablen:
#   OPENAI_API_KEY, GEMINI_API_KEY (oder GOOGLE_API_KEY), XAI_API_KEY
# Die Config wird NUR angelegt, wenn noch keine existiert
# (C:\Users\<du>\.claude-code-router\config.json) - eine vorhandene
# Config wird nie angefasst.

param(
    [int]$Port = 3456
)

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

Write-Host "=== ISU SURVIVOR - Cloud-Router (claude-code-router) ===" -ForegroundColor Cyan

if (Test-Port $Port) {
    Write-Host "Router laeuft schon auf Port $Port - nichts zu tun."
    exit 0
}

# 1) ccr installiert?
$ccr = Get-Command ccr -ErrorAction SilentlyContinue
if ($null -eq $ccr) {
    Write-Host "[1/3] claude-code-router fehlt - installiere (npm -g)..."
    npm install -g "@musistudio/claude-code-router"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FEHLER: npm install fehlgeschlagen." -ForegroundColor Red
        pause; exit 1
    }
    $ccr = Get-Command ccr -ErrorAction SilentlyContinue
    if ($null -eq $ccr) {
        Write-Host "FEHLER: 'ccr' nach Installation nicht im PATH." -ForegroundColor Red
        pause; exit 1
    }
} else {
    Write-Host "[1/3] ccr gefunden: $($ccr.Source)"
}

# 2) Config anlegen, falls keine existiert
$cfgDir = Join-Path $env:USERPROFILE ".claude-code-router"
$cfgFile = Join-Path $cfgDir "config.json"

# gpt5fix-Transformer immer aktuell halten (Repo-Kopie ist kanonisch).
# Ohne ihn lehnen GPT-5-Modelle Requests mit 400 ab (max_tokens bzw.
# reasoning_effort-Default bei gpt-5.6* mit Function-Tools).
$fixSrc = Join-Path $PSScriptRoot "gpt5fix.js"
$fixDst = Join-Path $cfgDir "gpt5fix.js"
if (Test-Path $fixSrc) {
    New-Item -ItemType Directory -Force $cfgDir | Out-Null
    Copy-Item $fixSrc $fixDst -Force
}

if (Test-Path $cfgFile) {
    Write-Host "[2/3] Config existiert schon: $cfgFile (wird nicht angefasst)."
} else {
    $openaiKey = $env:OPENAI_API_KEY
    $geminiKey = $env:GEMINI_API_KEY
    if (-not $geminiKey) { $geminiKey = $env:GOOGLE_API_KEY }
    $xaiKey = $env:XAI_API_KEY
    $missing = @()
    if (-not $openaiKey) { $missing += "OPENAI_API_KEY"; $openaiKey = "" }
    if (-not $geminiKey) { $missing += "GEMINI_API_KEY/GOOGLE_API_KEY"; $geminiKey = "" }
    if (-not $xaiKey)    { $missing += "XAI_API_KEY"; $xaiKey = "" }
    if ($missing.Count -gt 0) {
        Write-Host "WARNUNG: Keys fehlen: $($missing -join ', ')" -ForegroundColor Yellow
        Write-Host "         Die jeweiligen Provider liefern dann 401-Fehler." -ForegroundColor Yellow
        Write-Host "         Key setzen und diese Config editieren: $cfgFile" -ForegroundColor Yellow
    }

    # Modell-Listen muessen zu den Eintraegen im Arena-Menue passen
    # (IsuArenaMenu.c s_Models, Praefix openai/ google/ xai/).
    $cfg = @"
{
  "LOG": false,
  "PORT": $Port,
  "API_TIMEOUT_MS": 600000,
  "transformers": [
    { "name": "gpt5fix", "path": "__GPT5FIX__" }
  ],
  "Providers": [
    {
      "name": "openai",
      "api_base_url": "https://api.openai.com/v1/chat/completions",
      "api_key": "__OPENAI__",
      "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
      "transformer": {
        "use": [["maxtoken", { "max_tokens": 16000 }], "gpt5fix"]
      }
    },
    {
      "name": "gemini",
      "api_base_url": "https://generativelanguage.googleapis.com/v1beta/models/",
      "api_key": "__GEMINI__",
      "models": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro", "gemini-3.1-pro-preview"],
      "transformer": { "use": ["gemini"] }
    },
    {
      "name": "xai",
      "api_base_url": "https://api.x.ai/v1/chat/completions",
      "api_key": "__XAI__",
      "models": ["grok-4.6", "grok-4.5", "grok-4.3"]
    }
  ],
  "Router": {
    "default": "openai,gpt-5.6-luna"
  }
}
"@
    $cfg = $cfg.Replace("__OPENAI__", $openaiKey).Replace("__GEMINI__", $geminiKey).Replace("__XAI__", $xaiKey)
    $cfg = $cfg.Replace("__GPT5FIX__", $fixDst.Replace("\", "\\"))
    New-Item -ItemType Directory -Force $cfgDir | Out-Null
    # BOM-frei schreiben - Node JSON.parse stolpert ueber UTF-8-BOM
    [System.IO.File]::WriteAllText($cfgFile, $cfg, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[2/3] Config angelegt: $cfgFile"
}

# 3) Router starten (ccr restart startet detached im Hintergrund)
Write-Host "[3/3] Starte Router..."
ccr restart
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    if (Test-Port $Port) {
        Write-Host "OK: Router laeuft auf http://127.0.0.1:$Port" -ForegroundColor Green
        Write-Host "Stoppen: ccr stop   Status: ccr status   UI: ccr ui"
        exit 0
    }
    Start-Sleep -Seconds 2
}
Write-Host "FEHLER: Port $Port antwortet nicht. 'ccr status' pruefen." -ForegroundColor Red
pause
exit 1
