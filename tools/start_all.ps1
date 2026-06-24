# One-shot launcher: dev server (if not running) -> wait for bridge -> agent.
# All arguments are passed through to run_agent.py (e.g. --model, --idle, --once).
# ASCII only (PowerShell 5.1 safe).

$repo = Split-Path $PSScriptRoot -Parent
$serverDir = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" })
$state = Join-Path $serverDir "profiles\IsuSurvivor\state_viktor.json"

# 1) Server starten, falls die Dev-Instanz nicht laeuft
$running = $false
$procs = Get-CimInstance Win32_Process -Filter "Name='DayZServer_x64.exe'"
foreach ($p in $procs) {
    if ($p.CommandLine -match "serverDZ-isu.cfg") { $running = $true }
}
if ($running) {
    Write-Host "[start_all] Dev-Server laeuft schon."
} else {
    Write-Host "[start_all] Starte Dev-Server..."
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_server.ps1") | Out-Null
}

# 2) Auf die Bridge warten
Write-Host "[start_all] Warte auf Bridge (erster Start kann 2-4 Minuten dauern)..."
$deadline = (Get-Date).AddSeconds(420)
$alive = $false
while ((Get-Date) -lt $deadline) {
    if (Test-Path $state) {
        $age = ((Get-Date) - (Get-Item $state).LastWriteTime).TotalSeconds
        if ($age -lt 3) { $alive = $true; break }
    }
    Start-Sleep -Seconds 5
}
if (-not $alive) {
    Write-Host "[start_all] FEHLER: Bridge nicht aktiv. RPT pruefen: $serverDir\profiles\*.RPT" -ForegroundColor Red
    exit 1
}
Write-Host "[start_all] Bridge lebt."

# 3) Umgebungs-Hinweise (heutige Lektion: leise Warnungen werden uebersehen)
if (-not $env:DISCORD_BOT_TOKEN) {
    Write-Host "[start_all] Hinweis: DISCORD_BOT_TOKEN fehlt -> Discord-Funk bleibt AUS." -ForegroundColor Yellow
}
if (-not $env:ELEVENLABS_API_KEY) {
    Write-Host "[start_all] Hinweis: ELEVENLABS_API_KEY fehlt -> Bot waere stumm und taub." -ForegroundColor Yellow
}

# 4) Mikrofon-Check anbieten (Sprechprobe pro Geraet, Auswahl wird gespeichert)
if ($env:ELEVENLABS_API_KEY) {
    $micCfg = Join-Path $repo "arena\mic.json"
    if (Test-Path $micCfg) {
        $micAns = Read-Host "[start_all] Mikrofon-Check wiederholen? (j/N)"
    } else {
        Write-Host "[start_all] Noch kein Mikrofon eingerichtet." -ForegroundColor Yellow
        $micAns = Read-Host "[start_all] Mikrofon-Check jetzt ausfuehren (sprechen + auswaehlen)? (J/n)"
        if (-not $micAns) { $micAns = "j" }
    }
    if ($micAns -match "^[jJ]") {
        python (Join-Path $repo "daemon\mic_select.py")
    }
}

# 5) NPC-Auswahl (einer = dieses Fenster, mehrere = Arena-Modus)
$roster = (Get-Content (Join-Path $repo "arena\agents.json") -Raw | ConvertFrom-Json).agents
Write-Host ""
Write-Host "=== MITSPIELER WAEHLEN ===" -ForegroundColor Cyan
for ($i = 0; $i -lt $roster.Count; $i++) {
    $a = $roster[$i]
    Write-Host ("  [{0}] {1}  ({2}, Stimme: {3})" -f ($i + 1), $a.name, $a.model, $a.voice)
}
Write-Host "  [A] Alle vier (Arena)"
$sel = Read-Host "Auswahl (ENTER = Viktor, z.B. 2 oder 1,3 oder A)"
if (-not $sel) { $sel = "1" }

if ($sel -match "^[aA]" -or $sel -match ",") {
    $hostility = Read-Host "Gesinnung untereinander: [n]eutral oder [f]eindlich (ENTER = n)"
    if (-not $hostility) { $hostility = "n" }
    if ($sel -match "^[aA]") { $selArg = "alle" }
    else {
        $ids = @()
        foreach ($part in ($sel -split ",")) {
            $n = 0
            if ([int]::TryParse($part.Trim(), [ref]$n) -and $n -ge 1 -and $n -le $roster.Count) {
                $ids += $roster[$n - 1].id
            }
        }
        $selArg = $ids -join ","
    }
    Write-Host "[start_all] Mehrere Agenten -> Arena-Modus."
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_arena.ps1") $selArg $hostility
    exit $LASTEXITCODE
}

$n = 1
[void][int]::TryParse($sel.Trim(), [ref]$n)
if ($n -lt 1 -or $n -gt $roster.Count) { $n = 1 }
$agent = $roster[$n - 1]

$agentArgs = @(
    (Join-Path $repo "daemon\run_agent.py"),
    "--npc-id", $agent.id,
    "--name", $agent.name,
    "--model", $agent.model,
    "--voice", $agent.voice,
    "--spawn-x", $agent.spawn[0],
    "--spawn-z", $agent.spawn[1]
)
if ($agent.character) { $agentArgs += @("--character", (Join-Path $repo $agent.character)) }
$agentArgs += $args

# 6) Agent im Vordergrund (Strg+C beendet nur den Agenten, Server laeuft weiter)
Write-Host "[start_all] Starte $($agent.name) ($($agent.model))... (Strg+C beendet nur den Agenten)"
python @agentArgs
exit $LASTEXITCODE
