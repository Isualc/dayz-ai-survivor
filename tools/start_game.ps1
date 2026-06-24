# Ein-Klick-Start fuer den Menue-Workflow: Server + Arena-Supervisor +
# Spiel-Client. Die Agenten selbst werden danach IM SPIEL gestartet
# (Taste Pos1/Home -> Arena-Menue). ASCII only (PowerShell 5.1 safe).

$repo = Split-Path $PSScriptRoot -Parent
$serverDir = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" })
$state = Join-Path $serverDir "profiles\IsuSurvivor\state_viktor.json"
$cfgFile = Join-Path $serverDir "serverDZ-isu.cfg"
$activeMapFile = Join-Path $repo "arena\active_map.txt"
$campFile = Join-Path $serverDir "profiles\IsuSurvivor\camp.txt"

Write-Host "=== ISU SURVIVOR - SPIELSTART ===" -ForegroundColor Cyan

# 0) Karte waehlen. Jede Karte = eigene Mission + eigene Persistenz; auf Sakhal
#    spawnen die NPCs mit Winterkleidung, das Lager liegt an einem Landpunkt.
#    Map-Schluessel -> Mission-Template, Standard-Lagerpunkt (x,z auf Land).
$maps = @{
    "1" = @{ key = "chernarus"; template = "dayzOffline.chernarusplus"; name = "Chernarus"; camp = "4233.7,8512.2" }
    "2" = @{ key = "enoch";     template = "dayzOffline.enoch";         name = "Livonia (DLC)"; camp = "7900,6700" }
    "3" = @{ key = "sakhal";    template = "dayzOffline.sakhal";        name = "Sakhal (DLC, Winter)"; camp = "7680,7800" }
}
$lastMap = ""
if (Test-Path $activeMapFile) { $lastMap = (Get-Content $activeMapFile -TotalCount 1).Trim() }

Write-Host "[0/4] Karte waehlen:" -ForegroundColor Cyan
Write-Host "      1) Chernarus            (Basisspiel)"
Write-Host "      2) Livonia             (braucht Livonia-DLC im Steam-Konto)"
Write-Host "      3) Sakhal / Winter     (braucht Frostline-DLC im Steam-Konto)"
$mapAns = Read-Host "      Nummer (Enter = letzte/Chernarus)"
$sel = $maps[$mapAns]
if (-not $sel) {
    if ($lastMap) { $sel = ($maps.Values | Where-Object { $_.key -eq $lastMap } | Select-Object -First 1) }
    if (-not $sel) { $sel = $maps["1"] }
}
Write-Host ("      Karte: {0}" -f $sel.name) -ForegroundColor Green

# Map-Wechsel an der ECHTEN Server-Config festmachen (welche Karte laedt der
# Server gerade), NICHT an active_map.txt - das kann mit der cfg auseinander-
# laufen und genau das fuehrte dazu, dass eine andere als die gewaehlte Karte
# startete.
$curTemplate = ""
if (Test-Path $cfgFile) {
    $cm = [regex]::Match((Get-Content $cfgFile -Raw), 'template\s*=\s*"([^"]*)"')
    if ($cm.Success) { $curTemplate = $cm.Groups[1].Value }
}
$mapChange = ($curTemplate -ne $sel.template)

# Laeuft ein Server mit unserer Config?
$running = $false
$procs = Get-CimInstance Win32_Process -Filter "Name='DayZServer_x64.exe'"
foreach ($p in $procs) {
    if ($p.CommandLine -match "serverDZ-isu.cfg") { $running = $true }
}

# 1) Bei Map-Wechsel den laufenden Server ZUERST sauber stoppen (Persistenz
#    speichern), DANN die Config schreiben. Reihenfolge ist kritisch: solange
#    der Server laeuft, haelt er serverDZ-isu.cfg und der Template-Schreib-
#    vorgang verpufft - der Server lud dann weiter die alte Karte.
if ($running -and $mapChange) {
    Write-Host "[1/4] Kartenwechsel -> stoppe laufenden Server SAUBER (Persistenz speichern)..."
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop_server.ps1") | Out-Null
    $running = $false
}

# Template jetzt schreiben (Config nicht mehr gesperrt) und GEGENPRUEFEN.
if (-not (Test-Path $cfgFile)) {
    Write-Host "FEHLER: serverDZ-isu.cfg fehlt - tools\install_mods_to_server.ps1 zuerst laufen lassen." -ForegroundColor Red
    exit 1
}
$cfg = Get-Content $cfgFile -Raw
$cfg = [regex]::Replace($cfg, 'template\s*=\s*"[^"]*"', 'template = "' + $sel.template + '"')
[System.IO.File]::WriteAllText($cfgFile, $cfg, (New-Object System.Text.UTF8Encoding($false)))
$check = [regex]::Match((Get-Content $cfgFile -Raw), 'template\s*=\s*"([^"]*)"')
if (-not $check.Success -or $check.Groups[1].Value -ne $sel.template) {
    Write-Host ("FEHLER: Config-Template steht auf '{0}', sollte '{1}'. Haelt noch ein Server die Datei? Abbruch." -f $check.Groups[1].Value, $sel.template) -ForegroundColor Red
    exit 1
}
Write-Host ("      Config-Template gesetzt: {0}" -f $sel.template) -ForegroundColor Green

# Map-Wahl fuer den Supervisor (Winter-Loadouts + Spawn-Koordinaten)
New-Item -ItemType Directory -Force (Split-Path $activeMapFile) | Out-Null
Set-Content -Path $activeMapFile -Value $sel.key -Encoding ASCII
# Lagerpunkt der Karte vorgeben, damit das Zelt auf Land spawnt (nur bei
# Map-Wechsel ueberschreiben, sonst die im Spiel gesetzte Position behalten)
if ($mapChange -or ($sel.key -ne $lastMap) -or -not (Test-Path $campFile)) {
    New-Item -ItemType Directory -Force (Split-Path $campFile) | Out-Null
    $xz = $sel.camp.Split(",")
    Set-Content -Path $campFile -Value @($xz[0], $xz[1]) -Encoding ASCII
}

# Server starten, falls nicht (mehr) laufend
if ($running) {
    Write-Host "[1/4] Server laeuft schon (gleiche Karte)."
} else {
    Write-Host "[1/4] Starte Server..."
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_server.ps1") | Out-Null
}

# 2) Auf die Bridge warten
Write-Host "[2/4] Warte auf Bridge (erster Start kann 2-4 Minuten dauern)..."
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
    Write-Host "FEHLER: Bridge nicht aktiv. RPT pruefen: $serverDir\profiles\*.RPT" -ForegroundColor Red
    exit 1
}
Write-Host "      Bridge lebt."

# Gegenpruefung: hat der Server wirklich die gewaehlte Karte geladen?
$rpt = Get-ChildItem (Join-Path $serverDir "profiles") -Filter *.RPT -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($rpt -and (Select-String -Path $rpt.FullName -Pattern $sel.template -SimpleMatch -Quiet)) {
    Write-Host ("      Karte bestaetigt geladen: {0}" -f $sel.name) -ForegroundColor Green
} elseif ($rpt) {
    Write-Host ("WARNUNG: RPT zeigt nicht '{0}'. Evtl. laeuft noch ein alter Server auf einer anderen Karte - mit tools\stop_server.ps1 stoppen und neu starten." -f $sel.template) -ForegroundColor Yellow
}

# Umgebungs-Hinweise
if (-not $env:DISCORD_BOT_TOKEN) {
    Write-Host "      Hinweis: DISCORD_BOT_TOKEN fehlt -> Discord-Funk bleibt AUS." -ForegroundColor Yellow
}
if (-not $env:ELEVENLABS_API_KEY) {
    Write-Host "      Hinweis: ELEVENLABS_API_KEY fehlt -> kein TTS/Mikro." -ForegroundColor Yellow
}
# Mikrofon waehlen - IMMER anbieten, damit man bei Stille oder einem nicht
# erkannten/falschen Geraet jederzeit neu waehlen kann (nicht nur beim ersten
# Mal). Erststart: Default JA. Spaeter: Default NEIN (Enter ueberspringt).
if ($env:ELEVENLABS_API_KEY) {
    $micCfg = Join-Path $repo "arena\mic.json"
    if (Test-Path $micCfg) {
        $micAns = Read-Host "      Mikrofon neu waehlen/pruefen? (j/N) - bei Stille oder falschem Geraet"
        $doMic = ($micAns -match "^[jJ]")
    } else {
        $micAns = Read-Host "      Mikrofon noch nicht eingerichtet - jetzt waehlen? (J/n)"
        $doMic = (-not $micAns -or $micAns -match "^[jJ]")
    }
    if ($doMic) {
        python (Join-Path $repo "daemon\mic_select.py")
    }
}

# 3) Arena-Supervisor starten (falls nicht schon einer laeuft)
$sup = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "arena_supervisor\.py" }
if ($sup) {
    Write-Host "[3/4] Supervisor laeuft schon."
} else {
    Write-Host "[3/4] Starte Arena-Supervisor (eigenes Fenster, offen lassen)..."
    Start-Process powershell -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "start_supervisor.ps1"))
}

# 4) Spiel-Client starten (mit allen Mods inkl. @IsuVoice) - OHNE
#    Auto-Connect: selbst joinen ist stabiler (Auto-Connect kickte, wenn
#    der Server noch nicht ganz bereit war)
$client = Get-Process -Name "DayZ_x64" -ErrorAction SilentlyContinue
if ($client) {
    Write-Host "[4/4] Client laeuft schon."
} else {
    Write-Host "[4/4] Starte Spiel-Client mit BattlEye (DayZ_BE.exe, ohne Auto-Connect)..."
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_client.ps1") -NoConnect
}

Write-Host ""
Write-Host "FERTIG. So geht es weiter:" -ForegroundColor Green
Write-Host "  1. Im Spiel ueber den Server-Browser joinen (LAN/Community: 127.0.0.1:2302)" -ForegroundColor Green
Write-Host "  2. Einfg  ->  Arena-Menue (Agenten, Modelle, Gesinnung, Lager, Start/Stop)" -ForegroundColor Green

