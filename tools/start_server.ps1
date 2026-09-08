# Starts the local dev server with Expansion-AI and the IsuSurvivor server mod.
# UTF-8 with BOM for Windows PowerShell 5.1.

param(
    [string]$ServerDir  = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" }),
    [int]$Port          = 2302,
    # Bundle enthaelt Core+AI+Chat+Market+BaseBuilding usw. - Core/AI NICHT separat laden!
    # @IsuVoice = Voice-Lines des Agenten (Client+Server)
    [string]$Mods       = "@CF;@DF;@DayZ-Expansion-Bundle;@DayZ-Expansion-Licensed;@DayZ-Expansion-Animations;@VPPAdminTools;@IsuVoice",
    [string]$ServerMods = "@IsuSurvivor",
    [ValidateSet('de', 'en')][string]$UiLanguage
)

. (Join-Path $PSScriptRoot 'ui_language.ps1')
$isuLanguageArgs = @{ StatePath = (Join-Path $ServerDir 'profiles\IsuSurvivor\ui_language.txt') }
if ($PSBoundParameters.ContainsKey('UiLanguage')) { $isuLanguageArgs.UiLanguage = $UiLanguage }
$UiLanguage = Select-IsuUiLanguage @isuLanguageArgs
function Get-IsuLaunchText([string]$English, [string]$German) {
    Get-IsuUiText -English $English -German $German -UiLanguage $UiLanguage
}

$exe = Join-Path $ServerDir "DayZServer_x64.exe"
if (-not (Test-Path $exe)) { Write-Error (Get-IsuLaunchText "Not found: $exe" "Nicht gefunden: $exe"); exit 1 }

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
            Write-Host (Get-IsuLaunchText "Auto-deployed fresh $modName.pbo from build\." "Aktuelle $modName.pbo aus build\ installiert.")
        } catch {
            Write-Warning (Get-IsuLaunchText "$modName.pbo is locked (server or game client still running?) - using the old build." "$modName.pbo ist gesperrt (Server oder Spiel noch aktiv?) - verwende vorhandenen Build.")
        }
    }
}
if (-not (Test-Path (Join-Path $ServerDir "serverDZ-isu.cfg"))) {
    Write-Error (Get-IsuLaunchText "serverDZ-isu.cfg missing - run tools\install_mods_to_server.ps1 first." "serverDZ-isu.cfg fehlt - zuerst tools\install_mods_to_server.ps1 ausführen.")
    exit 1
}
if (-not (Test-Path (Join-Path $ServerDir "@IsuSurvivor\addons"))) {
    Write-Error (Get-IsuLaunchText "@IsuSurvivor not deployed - run tools\pack_mod.ps1 first." "@IsuSurvivor fehlt - zuerst tools\pack_mod.ps1 ausführen.")
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

Start-Process -FilePath $exe -WorkingDirectory $ServerDir -ArgumentList $args -WindowStyle Hidden
Write-Host (Get-IsuLaunchText "Server starting on port $Port ..." "Server startet auf Port $Port ...")
Write-Host (Get-IsuLaunchText "UI language: $UiLanguage" "Menüsprache: $UiLanguage")
Write-Host (Get-IsuLaunchText "Logs (RPT): $ServerDir\profiles\*.RPT" "Protokolle (RPT): $ServerDir\profiles\*.RPT")
Write-Host (Get-IsuLaunchText "Bridge dir: $ServerDir\profiles\IsuSurvivor\" "Bridge-Ordner: $ServerDir\profiles\IsuSurvivor\")
Write-Host (Get-IsuLaunchText "First start takes 1-3 minutes (Expansion generates its settings files)." "Der erste Start dauert 1-3 Minuten (Expansion erstellt seine Einstellungen).")
