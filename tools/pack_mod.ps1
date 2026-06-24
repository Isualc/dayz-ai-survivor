# Packs mod\IsuSurvivor into @IsuSurvivor\addons\IsuSurvivor.pbo and deploys it
# to the DayZ server directory. ASCII only (PowerShell 5.1 safe).
#
# If the AddonBuilder CLI flags ever change, use the GUI fallback documented
# in README.md (section "Mod packen").

param(
    [string]$DayZToolsDir = $(if ($env:DAYZ_TOOLS_DIR) { $env:DAYZ_TOOLS_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZ Tools" }),
    [string]$ServerDir   = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" }),
    [string]$ProjectDir  = (Split-Path $PSScriptRoot -Parent),
    [string]$ModName     = "IsuSurvivor"   # oder IsuVoice
)

$src = Join-Path $ProjectDir "mod\$ModName"
$out = Join-Path $ProjectDir "build\@$ModName\addons"
$include = Join-Path $PSScriptRoot "packing_include.txt"

if (-not (Test-Path $src)) { Write-Error "Mod source not found: $src"; exit 1 }
New-Item -ItemType Directory -Force $out | Out-Null

$ab = Join-Path $DayZToolsDir "Bin\AddonBuilder\AddonBuilder.exe"
if (-not (Test-Path $ab)) {
    Write-Error "AddonBuilder.exe not found at $ab - is DayZ Tools installed via Steam?"
    exit 1
}

Write-Host "Packing $src -> $out"
& $ab $src $out -clear -packonly "-include=$include" "-prefix=$ModName"
if ($LASTEXITCODE -ne 0) {
    Write-Error "AddonBuilder failed with exit code $LASTEXITCODE. Use the GUI fallback (README, 'Mod packen')."
    exit 1
}

$pbo = Get-ChildItem $out -Filter "*.pbo" | Select-Object -First 1
if ($null -eq $pbo) {
    Write-Error "No PBO produced - check AddonBuilder output above."
    exit 1
}

$dstAddons = Join-Path $ServerDir "@$ModName\addons"
New-Item -ItemType Directory -Force $dstAddons | Out-Null
try {
    Copy-Item (Join-Path $out "*.pbo") $dstAddons -Force -ErrorAction Stop
} catch {
    Write-Error "Deploy failed (server still running and locking the PBO?): $_"
    Write-Host "Stop the server first, then re-run this script or copy manually from $out"
    exit 1
}

Write-Host "OK: deployed $($pbo.Name) to $dstAddons"
Write-Host "Restart the server to load the new build."
