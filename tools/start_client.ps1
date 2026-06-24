# Starts the DayZ client and connects to the local dev server, loading the
# workshop mods plus @IsuVoice directly. Default start is VIA the BattlEye
# launcher (DayZ_BE.exe), sonst kickt jeder Server mit BattlEye den Spieler.
# ASCII only (PowerShell 5.1 safe).

param(
    [string]$DayZDir     = $(if ($env:DAYZ_DIR) { $env:DAYZ_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZ" }),
    [string]$WorkshopDir = $(if ($env:DAYZ_WORKSHOP_DIR) { $env:DAYZ_WORKSHOP_DIR } else { "C:\Program Files (x86)\Steam\steamapps\workshop\content\221100" }),
    [string]$ServerIp    = "127.0.0.1",
    [int]$Port           = 2302,
    [string]$PlayerName  = "Survivor",
    [switch]$NoConnect,  # Plan C: ohne -connect starten, dann im Server-Browser joinen
    [switch]$NoBE        # Notausgang: Direktstart ohne BattlEye (DayZ_x64.exe),
                         # nur fuer den BE=0-Dev-Server. Default ist MIT BattlEye.
)

# DayZ_BE.exe ist der BattlEye-Launcher: er initialisiert den BE-Client und
# reicht -mod/-connect/-name an DayZ_x64 durch. Ohne ihn fehlt BattlEye und
# der Server kickt beim Join.
$exeName = "DayZ_BE.exe"
if ($NoBE) { $exeName = "DayZ_x64.exe" }
$exe = Join-Path $DayZDir $exeName
if (-not (Test-Path $exe)) { Write-Error "Not found: $exe"; exit 1 }

$mods = @(
    (Join-Path $WorkshopDir "1559212036"),  # @CF
    (Join-Path $WorkshopDir "2545327648"),  # @DF (Dabs Framework)
    (Join-Path $WorkshopDir "2572331007"),  # @DayZ-Expansion-Bundle (Core+AI+Chat+...)
    (Join-Path $WorkshopDir "2116157322"),  # @DayZ-Expansion-Licensed
    (Join-Path $WorkshopDir "2793893086"),  # @DayZ-Expansion-Animations
    (Join-Path $WorkshopDir "1828439124"),  # @VPPAdminTools
    (Join-Path $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" }) "@IsuVoice")  # Voice-Lines (lokal deployed)
) -join ";"

$args = @(
    "-mod=$mods",
    "-name=$PlayerName"
)
if (-not $NoConnect) {
    $args += "-connect=$ServerIp"
    $args += "-port=$Port"
}

Start-Process -FilePath $exe -WorkingDirectory $DayZDir -ArgumentList $args

if ($NoBE) {
    Write-Host "Client start OHNE BattlEye ($exeName) - nur fuer den BE=0-Dev-Server."
} else {
    Write-Host "Client start MIT BattlEye ($exeName). Der BE-Launcher zeigt kurz ein"
    Write-Host "eigenes Fenster und startet dann das Spiel - das ist normal."
}
if ($NoConnect) {
    Write-Host "Ohne Auto-Connect. Join via Server-Browser: Community/LAN oder direkt $ServerIp`:$Port"
} else {
    Write-Host "Verbinde zu $ServerIp`:$Port als $PlayerName ..."
}
