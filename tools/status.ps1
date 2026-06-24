# Wer lebt? Slot-, Koerper- und GEHIRN-Status aller Agenten auf einen Blick.
# ASCII only (PowerShell 5.1 safe).

$repo = Split-Path $PSScriptRoot -Parent
$profileDir = Join-Path $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" }) "profiles\IsuSurvivor"
$roster = (Get-Content (Join-Path $repo "arena\agents.json") -Raw | ConvertFrom-Json).agents

# Laufende Runner-Prozesse einsammeln (python run_agent.py --npc-id X)
$runners = @{}
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
foreach ($p in $procs) {
    if ($p.CommandLine -match "run_agent\.py" -and $p.CommandLine -match "--npc-id\s+(\w+)") {
        $runners[$Matches[1]] = $p.ProcessId
    } elseif ($p.CommandLine -match "run_agent\.py") {
        $runners["viktor"] = $p.ProcessId
    }
}

$serverUp = $false
$srv = Get-CimInstance Win32_Process -Filter "Name='DayZServer_x64.exe'"
foreach ($s in $srv) { if ($s.CommandLine -match "serverDZ-isu.cfg") { $serverUp = $true } }
if ($serverUp) { Write-Host "Server: LAEUFT" -ForegroundColor Green }
else { Write-Host "Server: AUS" -ForegroundColor Red }
Write-Host ""
Write-Host ("{0,-8} {1,-10} {2,-22} {3}" -f "AGENT", "GEHIRN", "KOERPER", "POSITION")

foreach ($a in $roster) {
    $brain = "---"
    $brainColor = "DarkGray"
    if ($runners.ContainsKey($a.id)) { $brain = "LAEUFT"; $brainColor = "Green" }

    $body = "---"
    $bodyColor = "DarkGray"
    $pos = ""
    $stateFile = Join-Path $profileDir "state_$($a.id).json"
    if (Test-Path $stateFile) {
        $age = ((Get-Date) - (Get-Item $stateFile).LastWriteTime).TotalSeconds
        if ($age -lt 5) {
            $s = Get-Content $stateFile -Raw | ConvertFrom-Json
            if ($s.npc.spawned -and $s.npc.alive) {
                $body = "lebt"
                $bodyColor = "Green"
                $pos = "x=$([math]::Round($s.npc.pos_x)) z=$([math]::Round($s.npc.pos_z))"
                if ($brain -eq "---") {
                    $body = "lebt OHNE GEHIRN (stumm!)"
                    $bodyColor = "Yellow"
                }
            } elseif ($s.npc.spawned) {
                $body = "TOT"
                $bodyColor = "Red"
            } else {
                $body = "kein Koerper"
            }
        } else {
            $body = "Slot inaktiv"
        }
    }

    Write-Host ("{0,-8} " -f $a.name) -NoNewline
    Write-Host ("{0,-10} " -f $brain) -ForegroundColor $brainColor -NoNewline
    Write-Host ("{0,-22} " -f $body) -ForegroundColor $bodyColor -NoNewline
    Write-Host $pos
}
Write-Host ""
Write-Host "Merke: Ein Koerper OHNE Gehirn-Fenster antwortet nicht. Agenten startest"
Write-Host "du mit start_all.bat (Auswahl) oder start_arena.bat (mehrere)."
