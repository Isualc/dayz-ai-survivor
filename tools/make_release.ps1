# ===========================================================================
#  make_release.ps1 - build the distributable Setup ZIP
# ===========================================================================
#  Produces dist\dayz-ai-survivor-Setup.zip: the public repo tree PLUS the two
#  pre-packed server-mod PBOs (build\@IsuSurvivor, build\@IsuVoice), minus the
#  dev-only heavy folders. Recipients unzip it and double-click INSTALL.bat -
#  they never need DayZ Tools, because the PBOs ship inside.
#
#  Run this from the PUBLIC repo after mirroring your latest changes into it.
#  ASCII only (PowerShell 5.1 safe).
#
#  Params:
#    -BuildSource  where the freshly packed PBOs live. Defaults to this repo's
#                  own build\, then falls back to a sibling working copy.
#    -OutDir       where the ZIP lands (default: dist\).
# ===========================================================================

param(
    [string]$BuildSource = "",
    [string]$OutDir      = ""
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path $PSScriptRoot -Parent
if (-not $OutDir) { $OutDir = Join-Path $RepoDir "dist" }

$mods = @("IsuSurvivor", "IsuVoice")

# --- 1) Locate the packed PBOs -------------------------------------------
function Test-BuildHasPbos($buildDir) {
    foreach ($m in $mods) {
        if (-not (Test-Path (Join-Path $buildDir "@$m\addons\$m.pbo"))) { return $false }
    }
    return $true
}

$candidates = @()
if ($BuildSource) { $candidates += $BuildSource }
$candidates += (Join-Path $RepoDir "build")
# sibling private working copy (common dev layout: ...\dayz-ai-survivor\build)
$parent = Split-Path $RepoDir -Parent
$candidates += (Join-Path $parent "dayz-ai-survivor\build")

$srcBuild = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c) -and (Test-BuildHasPbos $c)) { $srcBuild = $c; break }
}
if (-not $srcBuild) {
    Write-Error ("Keine fertigen PBOs gefunden. Erst die Mods packen (tools\pack_mod.ps1 fuer IsuSurvivor UND IsuVoice), oder -BuildSource auf den build-Ordner zeigen lassen. Gesucht in:`n  " + ($candidates -join "`n  "))
    exit 1
}
Write-Host "PBO-Quelle: $srcBuild" -ForegroundColor Cyan

# --- 2) Ensure the PBOs are inside THIS repo's build\ (so the zip carries them) ---
$repoBuild = Join-Path $RepoDir "build"
if ($srcBuild -ne $repoBuild) {
    foreach ($m in $mods) {
        $dst = Join-Path $repoBuild "@$m\addons"
        New-Item -ItemType Directory -Force $dst | Out-Null
        Copy-Item (Join-Path $srcBuild "@$m\addons\$m.pbo") $dst -Force
        $keys = Join-Path $srcBuild "@$m\keys"
        if (Test-Path $keys) {
            $kd = Join-Path $repoBuild "@$m\keys"
            New-Item -ItemType Directory -Force $kd | Out-Null
            Copy-Item (Join-Path $keys "*.bikey") $kd -Force -ErrorAction SilentlyContinue
        }
        Write-Host "  PBO uebernommen: @$m" -ForegroundColor Green
    }
}

# --- 3) Stage a clean tree (exclude dev-only / heavy dirs) ----------------
# Top-level folders we never ship. build\ is KEPT (carries the PBOs).
$excludeDirs = @(".git", ".github", "dist", "models", "reference", "llama-bin", "Logs", "__pycache__")
# Per-agent runtime noise we do not need in a fresh install.
$excludeGlobs = @("journal", "_memory_backup", "_memory_backup_pre_restructure")

$stage = Join-Path $env:TEMP ("isu_release_" + [System.IO.Path]::GetRandomFileName().Substring(0,6))
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Write-Host "Staging: $stage" -ForegroundColor DarkGray

robocopy $RepoDir $stage /MIR /XD $excludeDirs $excludeGlobs /XF "*.log" "*.pyc" /NFL /NDL /NJH /NJS /NP | Out-Null
# robocopy exit codes 0-7 are success; 8+ are real errors.
if ($LASTEXITCODE -ge 8) { Write-Error "robocopy meldete Fehler (Code $LASTEXITCODE)."; exit 1 }

# --- 4) Zip it -----------------------------------------------------------
New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path $OutDir "dayz-ai-survivor-Setup.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
# Build entries by hand so paths use forward slashes (ZIP spec; keeps 7-Zip,
# WinRAR, Linux unzip happy). .NET Framework's CreateFromDirectory would write
# backslashes on Windows, which some extractors mishandle.
$archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $stageFull = (Resolve-Path $stage).Path.TrimEnd('\')
    foreach ($file in Get-ChildItem $stage -Recurse -File) {
        $rel = $file.FullName.Substring($stageFull.Length + 1) -replace '\\', '/'
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $rel) | Out-Null
    }
} finally { $archive.Dispose() }
Remove-Item $stage -Recurse -Force

$size = [Math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "FERTIG: $zip ($size MB)" -ForegroundColor Green
Write-Host "Empfaenger: entpacken -> INSTALL.bat doppelklicken." -ForegroundColor Green
