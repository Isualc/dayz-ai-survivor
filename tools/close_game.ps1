# Sauberes Schliessen des kompletten IsuSurvivor-Stacks. ASCII only (PS 5.1).
#
# Reihenfolge ist wichtig:
#   1. Agenten SANFT stoppen (stop.flag) - run_agent despawnt den Koerper und
#      sichert das Inventar (last_inventory.json) ueber den finally-Pfad,
#      solange der Server noch laeuft.
#   2. Supervisor, Voice-Router, Discord-Bots, Runner + Claude-CLI beenden.
#   3. Lokale Backends (llama-server / claude-code-router) beenden.
#   4. DayZ-Client schliessen.
#   5. SERVER ZULETZT und GRACEFUL (tools\stop_server.ps1, taskkill OHNE /F) -
#      nur so speichert DayZ die Persistenz (Zelte/Tonnen/Autos + Inhalt) nach
#      storage_1. Ein Hart-Kill verloere alles seit dem letzten Save.

$repo = Split-Path $PSScriptRoot -Parent

Write-Host "=== ISU SURVIVOR - SPIEL SCHLIESSEN (graceful) ===" -ForegroundColor Cyan

# 1) Agenten sanft stoppen: stop.flag in jede Agenten-Heimat legen.
Write-Host "[1/5] Agenten sanft stoppen (stop.flag) - despawnt + sichert Inventar..."
$homes = @(Join-Path $repo "agent_home")
$homes += (Get-ChildItem (Join-Path $repo "agent_homes") -Directory -ErrorAction SilentlyContinue |
           ForEach-Object { $_.FullName })
foreach ($h in $homes) {
    $flag = Join-Path $h "stop.flag"
    try {
        if (-not (Test-Path $flag)) { New-Item -ItemType File -Path $flag | Out-Null }
        else { (Get-Item $flag).LastWriteTime = Get-Date }
    } catch {}
}
Write-Host "      warte auf sauberes Despawnen/Inventar-Sichern (8 s)..."
Start-Sleep -Seconds 8

# 2)-3) Hintergrund-Prozesse beenden (halten keine Persistenz -> Force ok).
function Kill-ByCmd([string]$pattern, [string]$label) {
    # frische Prozessliste pro Aufruf: ein /T-Tree-Kill kann Geschwister schon
    # mitgenommen haben, dann nichts doppelt als "beendet" melden
    $hits = Get-CimInstance Win32_Process |
            Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern }
    foreach ($p in $hits) {
        try { taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null } catch {}
        Write-Host ("      beendet: {0} (PID {1})" -f $label, $p.ProcessId)
    }
}

Write-Host "[2/5] Supervisor, Voice-Router, Discord-Bots, Runner beenden..."
# Supervisor zuerst (/T nimmt seine Kind-Prozesse - Runner, Voice, node - mit)
Kill-ByCmd "arena_supervisor\.py"          "Supervisor"
Kill-ByCmd "run_agent\.py"                 "Runner"
Kill-ByCmd "voice_router\.py"              "Voice-Router"
Kill-ByCmd "discord_voice\.py"             "Discord-Bot"
Kill-ByCmd "mic_listener\.py"              "Mikro-Listener"

Write-Host "[3/5] Lokale Backends beenden (llama-server / claude-code-router)..."
Kill-ByCmd "llama-server"                  "llama-server"
Kill-ByCmd "claude-code-router|@musistudio" "claude-code-router"

# 4) DayZ-Client schliessen (erst sanft via Fenster, dann hart).
Write-Host "[4/5] DayZ-Client schliessen..."
$clientNames = @("DayZ_x64", "DayZ_BE", "DayZ")
foreach ($n in $clientNames) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.CloseMainWindow() | Out-Null } catch {}
    }
}
Start-Sleep -Seconds 2
foreach ($n in $clientNames) {
    Get-Process -Name $n -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

# 5) SERVER ZULETZT und SAUBER (kritisch: Persistenz speichern). NIE Force.
Write-Host "[5/5] Server SAUBER stoppen (Persistenz speichern)..." -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop_server.ps1")

Write-Host ""
Write-Host "FERTIG. Alles geschlossen, Server-Persistenz gespeichert." -ForegroundColor Green
