# Ein-Klick-Start fuer den Menue-Workflow: Server + Arena-Supervisor +
# Spiel-Client. Die Agenten selbst werden danach IM SPIEL gestartet
# (Taste Einfg -> Arena-Menue). UTF-8 BOM for Windows PowerShell 5.1.

param([ValidateSet('de', 'en')][string]$UiLanguage)

$repo = Split-Path $PSScriptRoot -Parent
$serverDir = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" })
$state = Join-Path $serverDir "profiles\IsuSurvivor\state_viktor.json"
$cfgFile = Join-Path $serverDir "serverDZ-isu.cfg"
$activeMapFile = Join-Path $repo "arena\active_map.txt"
$campFile = Join-Path $serverDir "profiles\IsuSurvivor\camp.txt"

. (Join-Path $PSScriptRoot 'ui_language.ps1')
$isuLanguageArgs = @{ StatePath = (Join-Path $serverDir 'profiles\IsuSurvivor\ui_language.txt') }
if ($PSBoundParameters.ContainsKey('UiLanguage')) { $isuLanguageArgs.UiLanguage = $UiLanguage }
$UiLanguage = Select-IsuUiLanguage @isuLanguageArgs
function Get-IsuLaunchText([string]$English, [string]$German) {
    Get-IsuUiText -English $English -German $German -UiLanguage $UiLanguage
}

Write-Host (Get-IsuLaunchText '=== ISU SURVIVOR - LAUNCH ===' '=== ISU SURVIVOR - SPIELSTART ===') -ForegroundColor Cyan

# 0) Karte wählen. Jede Karte = eigene Mission + eigene Persistenz; auf Sakhal
#    spawnen die NPCs mit Winterkleidung, das Lager liegt an einem Landpunkt.
#    Map-Schluessel -> Mission-Template, Standard-Lagerpunkt (x,z auf Land).
$maps = @{
    "1" = @{ key = "chernarus"; template = "dayzOffline.chernarusplus"; name = "Chernarus"; camp = "4233.7,8512.2" }
    "2" = @{ key = "enoch";     template = "dayzOffline.enoch";         name = "Livonia (DLC)"; camp = "7900,6700" }
    "3" = @{ key = "sakhal";    template = "dayzOffline.sakhal";        name = "Sakhal (DLC, Winter)"; camp = "7680,7800" }
}
$lastMap = ""
if (Test-Path $activeMapFile) { $lastMap = (Get-Content $activeMapFile -TotalCount 1).Trim() }

Write-Host (Get-IsuLaunchText '[0/4] Choose a map:' '[0/4] Karte wählen:') -ForegroundColor Cyan
Write-Host (Get-IsuLaunchText '      1) Chernarus            (base game)' '      1) Chernarus            (Basisspiel)')
Write-Host (Get-IsuLaunchText '      2) Livonia             (requires Livonia DLC on Steam)' '      2) Livonia             (braucht Livonia-DLC im Steam-Konto)')
Write-Host (Get-IsuLaunchText '      3) Sakhal / Winter     (requires Frostline DLC on Steam)' '      3) Sakhal / Winter     (braucht Frostline-DLC im Steam-Konto)')
$mapAns = Read-Host (Get-IsuLaunchText '      Number (Enter = previous/Chernarus)' '      Nummer (Enter = letzte/Chernarus)')
$sel = $maps[$mapAns]
if (-not $sel) {
    if ($lastMap) { $sel = ($maps.Values | Where-Object { $_.key -eq $lastMap } | Select-Object -First 1) }
    if (-not $sel) { $sel = $maps["1"] }
}
Write-Host ((Get-IsuLaunchText '      Map: {0}' '      Karte: {0}') -f $sel.name) -ForegroundColor Green

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
#    der Server läuft, haelt er serverDZ-isu.cfg und der Template-Schreib-
#    vorgang verpufft - der Server lud dann weiter die alte Karte.
if ($running -and $mapChange) {
    Write-Host (Get-IsuLaunchText '[1/4] Map change -> stopping server gracefully (saving persistence)...' '[1/4] Kartenwechsel -> stoppe laufenden Server sauber (Persistenz speichern)...')
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop_server.ps1") | Out-Null
    $running = $false
}

# Template jetzt schreiben (Config nicht mehr gesperrt) und GEGENPRUEFEN.
if (-not (Test-Path $cfgFile)) {
    Write-Host (Get-IsuLaunchText 'ERROR: serverDZ-isu.cfg missing - run tools\install_mods_to_server.ps1 first.' 'FEHLER: serverDZ-isu.cfg fehlt - tools\install_mods_to_server.ps1 zuerst laufen lassen.') -ForegroundColor Red
    exit 1
}
$cfg = Get-Content $cfgFile -Raw
$cfg = [regex]::Replace($cfg, 'template\s*=\s*"[^"]*"', 'template = "' + $sel.template + '"')
[System.IO.File]::WriteAllText($cfgFile, $cfg, (New-Object System.Text.UTF8Encoding($false)))
$check = [regex]::Match((Get-Content $cfgFile -Raw), 'template\s*=\s*"([^"]*)"')
if (-not $check.Success -or $check.Groups[1].Value -ne $sel.template) {
    Write-Host ((Get-IsuLaunchText "ERROR: Config template is '{0}', expected '{1}'. Is a server locking the file? Aborting." "FEHLER: Config-Template steht auf '{0}', sollte '{1}'. Hält noch ein Server die Datei? Abbruch.") -f $check.Groups[1].Value, $sel.template) -ForegroundColor Red
    exit 1
}
Write-Host ((Get-IsuLaunchText '      Config template set: {0}' '      Config-Template gesetzt: {0}') -f $sel.template) -ForegroundColor Green

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
    Write-Host (Get-IsuLaunchText '[1/4] Server already running (same map).' '[1/4] Server läuft schon (gleiche Karte).')
} else {
    Write-Host (Get-IsuLaunchText '[1/4] Starting server...' '[1/4] Starte Server...')
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_server.ps1") -UiLanguage $UiLanguage | Out-Null
}

# 2) Auf die Bridge warten
Write-Host (Get-IsuLaunchText '[2/4] Waiting for bridge (first start may take 2-4 minutes)...' '[2/4] Warte auf Bridge (erster Start kann 2-4 Minuten dauern)...')
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
    Write-Host (Get-IsuLaunchText "ERROR: Bridge inactive. Check RPT: $serverDir\profiles\*.RPT" "FEHLER: Bridge nicht aktiv. RPT prüfen: $serverDir\profiles\*.RPT") -ForegroundColor Red
    exit 1
}
Write-Host (Get-IsuLaunchText '      Bridge is active.' '      Bridge lebt.')

# Gegenpruefung: hat der Server wirklich die gewaehlte Karte geladen?
$rpt = Get-ChildItem (Join-Path $serverDir "profiles") -Filter *.RPT -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($rpt -and (Select-String -Path $rpt.FullName -Pattern $sel.template -SimpleMatch -Quiet)) {
    Write-Host ((Get-IsuLaunchText '      Map load confirmed: {0}' '      Karte bestätigt geladen: {0}') -f $sel.name) -ForegroundColor Green
} elseif ($rpt) {
    Write-Host ((Get-IsuLaunchText "WARNING: RPT does not show '{0}'. Another server may still be running a different map. Use tools\stop_server.ps1 and restart." "WARNUNG: RPT zeigt nicht '{0}'. Evtl. läuft noch ein alter Server auf einer anderen Karte - mit tools\stop_server.ps1 stoppen und neu starten.") -f $sel.template) -ForegroundColor Yellow
}

# Umgebungs-Hinweise
if (-not $env:DISCORD_BOT_TOKEN) {
    Write-Host (Get-IsuLaunchText '      Note: DISCORD_BOT_TOKEN missing -> Discord radio stays OFF.' '      Hinweis: DISCORD_BOT_TOKEN fehlt -> Discord-Funk bleibt AUS.') -ForegroundColor Yellow
}
if (-not $env:ELEVENLABS_API_KEY) {
    Write-Host (Get-IsuLaunchText '      Note: ELEVENLABS_API_KEY missing -> no TTS/microphone.' '      Hinweis: ELEVENLABS_API_KEY fehlt -> kein TTS/Mikro.') -ForegroundColor Yellow
}
# Mikrofon wählen - IMMER anbieten, damit man bei Stille oder einem nicht
# erkannten/falschen Gerät jederzeit neu wählen kann (nicht nur beim ersten
# Mal). Erststart: Default JA. Spaeter: Default NEIN (Enter ueberspringt).
if ($env:ELEVENLABS_API_KEY) {
    $micCfg = Join-Path $repo "arena\mic.json"
    if (Test-Path $micCfg) {
        $micAns = Read-Host (Get-IsuLaunchText '      Select/check microphone again? (y/N) - if silent or using the wrong device' '      Mikrofon neu wählen/prüfen? (j/N) - bei Stille oder falschem Gerät')
        $doMic = ($micAns -match '^[jJyY]')
    } else {
        $micAns = Read-Host (Get-IsuLaunchText '      Microphone not configured yet - select now? (Y/n)' '      Mikrofon noch nicht eingerichtet - jetzt wählen? (J/n)')
        $doMic = (-not $micAns -or $micAns -match '^[jJyY]')
    }
    if ($doMic) {
        python (Join-Path $repo "daemon\mic_select.py")
    }
}

# 3) Arena-Supervisor starten (falls nicht schon einer läuft)
$sup = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "arena_supervisor\.py" }
if ($sup) {
    Write-Host (Get-IsuLaunchText '[3/4] Supervisor already running.' '[3/4] Supervisor läuft schon.')
} else {
    Write-Host (Get-IsuLaunchText '[3/4] Starting arena supervisor in the background...' '[3/4] Starte Arena-Supervisor im Hintergrund...')
    Start-Process powershell -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "start_supervisor.ps1")) -WindowStyle Hidden
}

# 4) Spiel-Client starten (mit allen Mods inkl. @IsuVoice) - OHNE
#    Auto-Connect: selbst joinen ist stabiler (Auto-Connect kickte, wenn
#    der Server noch nicht ganz bereit war)
$client = Get-Process -Name "DayZ_x64" -ErrorAction SilentlyContinue
if ($client) {
    Write-Host (Get-IsuLaunchText '[4/4] Client already running.' '[4/4] Client läuft schon.')
} else {
    Write-Host (Get-IsuLaunchText '[4/4] Starting game client with BattlEye (DayZ_BE.exe, no auto-connect)...' '[4/4] Starte Spiel-Client mit BattlEye (DayZ_BE.exe, ohne Auto-Connect)...')
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_client.ps1") -NoConnect
}

Write-Host ""
Write-Host (Get-IsuLaunchText 'READY. Next steps:' 'FERTIG. So geht es weiter:') -ForegroundColor Green
Write-Host (Get-IsuLaunchText '  1. Join through the in-game server browser (LAN/Community: 127.0.0.1:2302)' '  1. Im Spiel über den Server-Browser joinen (LAN/Community: 127.0.0.1:2302)') -ForegroundColor Green
Write-Host (Get-IsuLaunchText '  2. Insert -> Arena menu (survivors, models, mode, camp, start/stop)' '  2. Einfg  -> Arena-Menü (Überlebende, Modelle, Modus, Lager, Start/Stop)') -ForegroundColor Green
