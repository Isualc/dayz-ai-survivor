# Starts the local dev server with Expansion-AI and the IsuSurvivor server mod.
# ASCII only (PowerShell 5.1 safe).

param(
    [string]$ServerDir  = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" }),
    [int]$Port          = 2302,
    # Bundle enthaelt Core+AI+Chat+Market+BaseBuilding usw. - Core/AI NICHT separat laden!
    # @IsuVoice = Voice-Lines des Agenten (Client+Server)
    [string]$Mods       = "@CF;@DF;@DayZ-Expansion-Bundle;@DayZ-Expansion-Licensed;@DayZ-Expansion-Animations;@VPPAdminTools;@IsuVoice",
    [string]$ServerMods = "@IsuSurvivor"
)

$exe = Join-Path $ServerDir "DayZServer_x64.exe"
if (-not (Test-Path $exe)) { Write-Error "Not found: $exe"; exit 1 }

# Auto-deploy: copy freshly packed PBOs from build\ if they are newer than
# the deployed ones (pack_mod.ps1 cannot copy while server/client lock them).
$repoDir = Split-Path $PSScriptRoot -Parent
foreach ($modName in @("IsuSurvivor", "IsuVoice")) {
    $srcPbo = Join-Path $repoDir "build\@$modName\addons\$modName.pbo"
    $dstDir = Join-Path $ServerDir "@$modName\addons"
    $dstPbo = Join-Path $dstDir "$modName.pbo"
    if (-not (Test-Path $srcPbo)) { continue }
    $needCopy = $true
    if (Test-Path $dstPbo) {
        if ((Get-Item $srcPbo).LastWriteTime -le (Get-Item $dstPbo).LastWriteTime) { $needCopy = $false }
    }
    if ($needCopy) {
        New-Item -ItemType Directory -Force $dstDir | Out-Null
        try {
            Copy-Item $srcPbo $dstPbo -Force -ErrorAction Stop
            Write-Host "Auto-deployed fresh $modName.pbo from build\."
        } catch {
            Write-Warning "$modName.pbo is locked (server or game client still running?) - using the old build."
        }
    }
}
if (-not (Test-Path (Join-Path $ServerDir "serverDZ-isu.cfg"))) {
    Write-Error "serverDZ-isu.cfg missing - run tools\install_mods_to_server.ps1 first."
    exit 1
}
if (-not (Test-Path (Join-Path $ServerDir "@IsuSurvivor\addons"))) {
    Write-Error "@IsuSurvivor not deployed - run tools\pack_mod.ps1 first."
    exit 1
}

$args = @(
    "-config=serverDZ-isu.cfg",
    "-port=$Port",
    "-profiles=profiles",
    "-mod=$Mods",
    "-servermod=$ServerMods",
    "-dologs",
    "-adminlog",
    "-freezecheck"
)

Start-Process -FilePath $exe -WorkingDirectory $ServerDir -ArgumentList $args
Write-Host "Server starting on port $Port ..."
Write-Host "Logs (RPT): $ServerDir\profiles\*.RPT"
Write-Host "Bridge dir: $ServerDir\profiles\IsuSurvivor\"
Write-Host "First start takes 1-3 minutes (Expansion generates its settings files)."
