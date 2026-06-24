# Einmaliges Setup der Cloud-API-Keys fuer die Fremd-Modelle im Arena-Menue.
# Fragt die drei Keys ab, speichert sie DAUERHAFT als Benutzer-
# Umgebungsvariablen und traegt sie zusaetzlich in eine evtl. schon
# vorhandene claude-code-router-Config ein. ASCII only (PowerShell 5.1 safe).
#
# Start: Rechtsklick -> Mit PowerShell ausfuehren
#        oder: powershell -ExecutionPolicy Bypass -File tools\set_api_keys.ps1
#
# Keys bekommst du hier:
#   OpenAI : https://platform.openai.com/api-keys
#   Google : https://aistudio.google.com/apikey   (Free Tier vorhanden)
#   xAI    : https://console.x.ai

$keys = @(
    @{ Name = "OPENAI_API_KEY"; Label = "OpenAI (GPT-5.4-mini / GPT-5.5)"; CcrProvider = "openai" },
    @{ Name = "GEMINI_API_KEY"; Label = "Google Gemini (3.5 Flash / Flash-Lite)"; CcrProvider = "gemini" },
    @{ Name = "XAI_API_KEY";    Label = "xAI (Grok 4.3)"; CcrProvider = "xai" }
)

Write-Host "=== ISU SURVIVOR - API-Keys einrichten ===" -ForegroundColor Cyan
Write-Host "Leer lassen = aktuellen Wert behalten. Minus (-) eingeben = Key loeschen."
Write-Host ""

$changed = @{}
foreach ($k in $keys) {
    $current = [Environment]::GetEnvironmentVariable($k.Name, "User")
    $status = "NICHT gesetzt"
    if ($current) { $status = "gesetzt (" + $current.Substring(0, [Math]::Min(8, $current.Length)) + "...)" }
    Write-Host "$($k.Label)" -ForegroundColor Yellow
    $answer = Read-Host "  $($k.Name) [$status]"
    if ($answer -eq "-") {
        [Environment]::SetEnvironmentVariable($k.Name, $null, "User")
        Set-Item -Path "Env:$($k.Name)" -Value "" -ErrorAction SilentlyContinue
        $changed[$k.CcrProvider] = ""
        Write-Host "  -> geloescht." -ForegroundColor Yellow
    } elseif ($answer) {
        $answer = $answer.Trim()
        [Environment]::SetEnvironmentVariable($k.Name, $answer, "User")
        Set-Item -Path "Env:$($k.Name)" -Value $answer
        $changed[$k.CcrProvider] = $answer
        Write-Host "  -> gespeichert (dauerhaft, Benutzer-Ebene)." -ForegroundColor Green
    } else {
        Write-Host "  -> unveraendert."
    }
    Write-Host ""
}

# Router-Config aktualisieren, falls sie schon existiert (start_router.ps1
# erzeugt sie sonst beim ersten Start selbst aus den Env-Variablen)
$cfgFile = Join-Path $env:USERPROFILE ".claude-code-router\config.json"
if (($changed.Count -gt 0) -and (Test-Path $cfgFile)) {
    try {
        $cfg = Get-Content $cfgFile -Raw | ConvertFrom-Json
        $updated = 0
        foreach ($p in $cfg.Providers) {
            if ($changed.ContainsKey($p.name)) {
                $p.api_key = $changed[$p.name]
                $updated++
            }
        }
        if ($updated -gt 0) {
            $json = $cfg | ConvertTo-Json -Depth 10
            [System.IO.File]::WriteAllText($cfgFile, $json, (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "Router-Config aktualisiert ($updated Provider): $cfgFile" -ForegroundColor Green
            Write-Host "Falls der Router laeuft: einmal 'ccr restart' ausfuehren."
        }
    } catch {
        Write-Host "WARNUNG: Router-Config konnte nicht aktualisiert werden: $_" -ForegroundColor Yellow
        Write-Host "Bitte manuell pruefen: $cfgFile" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "FERTIG. Wichtig: Schon offene Fenster (Supervisor, Spiel) sehen die" -ForegroundColor Green
Write-Host "neuen Keys NICHT - einmal schliessen und neu starten (start_game.bat)." -ForegroundColor Green
pause
