# One-time server setup: links workshop mods into the server dir (junctions),
# copies signing keys and the dev server config. ASCII only (PowerShell 5.1 safe).
#
# Prerequisite: subscribe the three workshop items with the Steam client first.
# CF and Expansion-Core are already on this machine; Expansion-AI may be missing.

param(
    [string]$ServerDir   = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" }),
    [string]$WorkshopDir = $(if ($env:DAYZ_WORKSHOP_DIR) { $env:DAYZ_WORKSHOP_DIR } else { "C:\Program Files (x86)\Steam\steamapps\workshop\content\221100" })
)

if (-not (Test-Path $ServerDir)) { Write-Error "Server dir not found: $ServerDir"; exit 1 }

$mods = @(
    @{ Name = "@CF";                        Id = "1559212036" },
    @{ Name = "@DF";                        Id = "2545327648" },  # Dabs Framework
    @{ Name = "@DayZ-Expansion-Bundle";     Id = "2572331007" },  # enthaelt Core+AI+Chat+Market+...
    @{ Name = "@DayZ-Expansion-Licensed";   Id = "2116157322" },
    @{ Name = "@DayZ-Expansion-Animations"; Id = "2793893086" },
    @{ Name = "@VPPAdminTools";             Id = "1828439124" },
    # Nur Junctions, NICHT in -mod laden (im Bundle enthalten; doppelt = Konflikt):
    @{ Name = "@DayZ-Expansion-Core";       Id = "2291785308" },
    @{ Name = "@DayZ-Expansion-AI";         Id = "2792982069" }
)

$missing = 0
foreach ($m in $mods) {
    $src = Join-Path $WorkshopDir $m.Id
    $dst = Join-Path $ServerDir $m.Name

    if (-not (Test-Path $src)) {
        Write-Warning ("{0}: workshop item {1} not found. Subscribe first: steam://url/CommunityFilePage/{1}" -f $m.Name, $m.Id)
        $missing++
        continue
    }

    if (-not (Test-Path $dst)) {
        New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
        Write-Host ("Junction: {0} -> {1}" -f $m.Name, $src)
    } else {
        Write-Host ("Exists:   {0}" -f $m.Name)
    }

    $keysDst = Join-Path $ServerDir "keys"
    if (-not (Test-Path $keysDst)) { New-Item -ItemType Directory -Force $keysDst | Out-Null }
    foreach ($keyDirName in @("keys", "Keys", "key", "Key")) {
        $keysSrc = Join-Path $src $keyDirName
        if (Test-Path $keysSrc) {
            Copy-Item (Join-Path $keysSrc "*.bikey") $keysDst -Force -ErrorAction SilentlyContinue
        }
    }
}

# Dev server config (BattlEye off, signature check off - LOCAL DEV ONLY)
$cfgSrc = Join-Path $PSScriptRoot "serverDZ-isu.cfg"
Copy-Item $cfgSrc (Join-Path $ServerDir "serverDZ-isu.cfg") -Force
Write-Host "Config:   serverDZ-isu.cfg deployed"

# Profile dir (state.json/commands.json will live here)
New-Item -ItemType Directory -Force (Join-Path $ServerDir "profiles") | Out-Null

if ($missing -gt 0) {
    Write-Warning "$missing workshop item(s) missing - subscribe in Steam, let it download, then re-run this script."
    exit 1
}
Write-Host "Setup complete. Next: tools\pack_mod.ps1, then tools\start_server.ps1"
