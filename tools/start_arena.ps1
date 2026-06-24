# Arena-Launcher: Auswahl der Modelle/Agenten, Hostilitaet, zentrale Voice.
# ASCII only (PowerShell 5.1 safe).
#
#   start_arena.bat                 -> interaktives Menue
#   start_arena.bat viktor,igor f   -> Viktor+Igor, feindlich
#   start_arena.bat alle n          -> alle vier, neutral

param(
    [string]$Selection = "",
    [string]$Hostility = ""
)

$repo = Split-Path $PSScriptRoot -Parent
$roster = (Get-Content (Join-Path $repo "arena\agents.json") -Raw | ConvertFrom-Json).agents
$serverDir = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" })

# Start-Process (PS 5.1) joined ArgumentList-Arrays OHNE Quoting - Argumente
# mit Leerzeichen (Stimme "Helmut - German Epic") zerfallen sonst in mehrere
# Args und argparse im Agenten bricht mit exit 2 ab. Daher selbst quoten.
function ConvertTo-CmdLine([object[]]$items) {
    ($items | ForEach-Object {
        $s = "$_"
        if ($s -match '\s') { '"' + $s + '"' } else { $s }
    }) -join " "
}

# ---------------------------------------------------------------- Auswahl
if (-not $Selection) {
    Write-Host ""
    Write-Host "=== ISU MODELL-ARENA ===" -ForegroundColor Cyan
    for ($i = 0; $i -lt $roster.Count; $i++) {
        $a = $roster[$i]
        Write-Host ("  [{0}] {1}  ({2}, Stimme: {3})" -f ($i + 1), $a.name, $a.model, $a.voice)
    }
    Write-Host "  [A] Alle vier"
    $answer = Read-Host "Auswahl (z.B. 1 oder 1,3 oder A)"
    $Selection = $answer
}

$selected = @()
if ($Selection -match "^[aA]") {
    $selected = $roster
} else {
    foreach ($part in ($Selection -split ",")) {
        $part = $part.Trim()
        $byIndex = 0
        if ([int]::TryParse($part, [ref]$byIndex) -and $byIndex -ge 1 -and $byIndex -le $roster.Count) {
            $selected += $roster[$byIndex - 1]
        } else {
            $hit = $roster | Where-Object { $_.id -eq $part.ToLower() }
            if ($hit) { $selected += $hit }
        }
    }
}
if ($selected.Count -eq 0) { Write-Host "Keine gueltige Auswahl."; exit 1 }

if (-not $Hostility) {
    $Hostility = Read-Host "Gesinnung untereinander: [n]eutral oder [f]eindlich"
}
$hostile = $Hostility -match "^[fF]"
if ($hostile) {
    Write-Host "FEINDLICH: Die Agenten bekommen verfeindete Fraktionen." -ForegroundColor Red
    Write-Host "WARNUNG: Auch bewaffnete Spieler koennen als Ziel gelten - Spectate empfohlen." -ForegroundColor Red
} else {
    Write-Host "NEUTRAL: Alle Agenten sind Zivilisten (friedlich)." -ForegroundColor Green
}

# ------------------------------------------------------------------ Server
$running = $false
$procs = Get-CimInstance Win32_Process -Filter "Name='DayZServer_x64.exe'"
foreach ($p in $procs) { if ($p.CommandLine -match "serverDZ-isu.cfg") { $running = $true } }
if (-not $running) {
    Write-Host "[arena] Starte Dev-Server..."
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_server.ps1") | Out-Null
}
Write-Host "[arena] Warte auf Bridge..."
$state = Join-Path $serverDir "profiles\IsuSurvivor\state_viktor.json"
$deadline = (Get-Date).AddSeconds(420)
$alive = $false
while ((Get-Date) -lt $deadline) {
    if (Test-Path $state) {
        $age = ((Get-Date) - (Get-Item $state).LastWriteTime).TotalSeconds
        if ($age -lt 3) { $alive = $true; break }
    }
    Start-Sleep -Seconds 5
}
if (-not $alive) { Write-Host "[arena] Bridge antwortet nicht - RPT pruefen." -ForegroundColor Red; exit 1 }

# --------------------------------------------------- Zentrale Voice-Prozesse
$sharedOutbox = Join-Path $repo "agent_home\voice_outbox.jsonl"
if (Test-Path $sharedOutbox) { Remove-Item $sharedOutbox -Force }

if ($env:DISCORD_BOT_TOKEN) {
    Start-Process python -ArgumentList (ConvertTo-CmdLine @((Join-Path $repo "daemon\discord_voice.py")))
    Write-Host "[arena] Discord-Bot gestartet (eigenes Fenster)."
} else {
    Write-Host "[arena] Kein DISCORD_BOT_TOKEN - kein Funk-Ausgang." -ForegroundColor Yellow
}

$ids = ($selected | ForEach-Object { $_.id }) -join ","
if ($env:ELEVENLABS_API_KEY) {
    $micCfg = Join-Path $repo "arena\mic.json"
    if (-not (Test-Path $micCfg)) {
        Write-Host "[arena] Noch kein Mikrofon eingerichtet." -ForegroundColor Yellow
        $micAns = Read-Host "[arena] Mikrofon-Check jetzt ausfuehren (sprechen + auswaehlen)? (J/n)"
        if (-not $micAns -or $micAns -match "^[jJ]") {
            python (Join-Path $repo "daemon\mic_select.py")
        }
    } else {
        $micAns = Read-Host "[arena] Mikrofon-Check wiederholen? (j/N)"
        if ($micAns -match "^[jJ]") {
            python (Join-Path $repo "daemon\mic_select.py")
        }
    }
    Start-Process python -ArgumentList (ConvertTo-CmdLine @((Join-Path $repo "daemon\voice_router.py"), "--selected", $ids))
    Write-Host "[arena] Voice-Router gestartet (Zustellung: Name > Naehe)."
    $names = ($selected | ForEach-Object { $_.name }) -join ", "
    Write-Host "[arena] ANSPRACHE PER STIMME: Namen nennen ($names)." -ForegroundColor Cyan
    Write-Host "[arena] Ohne Namen antwortet, wer dir im Spiel am naechsten steht." -ForegroundColor Cyan
} else {
    Write-Host "[arena] Kein ELEVENLABS_API_KEY - kein Mikro-Hoeren." -ForegroundColor Yellow
}

# -------------------------------------------------------------------- Agenten
$first = $true
foreach ($a in $selected) {
    $faction = "civilian"
    if ($hostile) { $faction = $a.faction_hostile }

    $agentArgs = @(
        (Join-Path $repo "daemon\run_agent.py"),
        "--npc-id", $a.id,
        "--name", $a.name,
        "--model", $a.model,
        "--voice", $a.voice,
        "--spawn-x", $a.spawn[0],
        "--spawn-z", $a.spawn[1],
        "--faction", $faction,
        "--no-voice-procs"
    )
    if ($a.character) { $agentArgs += @("--character", (Join-Path $repo $a.character)) }
    if ($a.loadout) { $agentArgs += @("--loadout", $a.loadout) }
    if (-not $first) { $agentArgs += "--no-tp" }
    $first = $false

    Start-Process python -ArgumentList (ConvertTo-CmdLine $agentArgs)
    Write-Host ("[arena] {0} gestartet ({1}, {2})" -f $a.name, $a.model, $faction)
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "[arena] Alle Agenten laufen in eigenen Fenstern. Viel Vergnuegen!" -ForegroundColor Cyan
