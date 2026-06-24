# Startet den Arena-Supervisor: nimmt Befehle aus dem In-Game-Menue
# (Taste Pos1/Home) entgegen und verwaltet die Agenten-Prozesse.
# ASCII only (PowerShell 5.1 safe).

$repo = Split-Path $PSScriptRoot -Parent

Write-Host "=== ISU Arena-Supervisor ===" -ForegroundColor Cyan
Write-Host "Im Spiel: Einfg oeffnet das Arena-Menue."
Write-Host "Dieses Fenster offen lassen - es startet/stoppt die Agenten."
Write-Host ""

python (Join-Path $repo "daemon\arena_supervisor.py")

