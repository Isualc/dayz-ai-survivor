# Einmaliges Setup der Cloud-API-Keys fuer die Fremd-Modelle im Arena-Menue.
# Fragt die optionalen Keys verdeckt ab, speichert sie DAUERHAFT als Benutzer-
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
#   Mistral: https://console.mistral.ai
#   Moonshot/Kimi: https://platform.kimi.ai
# Codex CLI / Antigravity CLI verwenden separat ihren vorhandenen Login.

$keys = @(
    @{ Name = "OPENAI_API_KEY"; Label = "OpenAI API (Cloud-Router)"; CcrProvider = "openai" },
    @{ Name = "GEMINI_API_KEY"; Label = "Google Gemini API (Cloud-Router)"; CcrProvider = "gemini" },
    @{ Name = "XAI_API_KEY"; Label = "xAI API (Cloud-Router)"; CcrProvider = "xai" },
    @{ Name = "MISTRAL_API_KEY"; Label = "Mistral API (direkt)"; CcrProvider = "" },
    @{ Name = "MOONSHOT_API_KEY"; Label = "Moonshot/Kimi API (direkt)"; CcrProvider = "" }
)

Write-Host "=== ISU SURVIVOR - API-Keys einrichten ===" -ForegroundColor Cyan
Write-Host "Leer lassen = aktuellen Wert behalten. Minus (-) eingeben = Key loeschen."
Write-Host ""

$changed = @{}
foreach ($k in $keys) {
    $status = "NICHT gesetzt"
    if ([Environment]::GetEnvironmentVariable($k.Name, "User")) { $status = "gesetzt" }
    Write-Host "$($k.Label)" -ForegroundColor Yellow
    # Nie Schluessel oder Praefixe im Terminal anzeigen/History speichern.
    $secretInput = Read-Host "  $($k.Name) [$status]" -AsSecureString
    $secretPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretInput)
    try {
        $answer = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPtr).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPtr)
        $secretInput.Dispose()
    }
    if ($answer -eq "-") {
        [Environment]::SetEnvironmentVariable($k.Name, $null, "User")
        Set-Item -Path "Env:$($k.Name)" -Value "" -ErrorAction SilentlyContinue
        if ($k.CcrProvider) { $changed[$k.CcrProvider] = "" }
        Write-Host "  -> geloescht." -ForegroundColor Yellow
    } elseif ($answer) {
        $answer = $answer.Trim()
        [Environment]::SetEnvironmentVariable($k.Name, $answer, "User")
        Set-Item -Path "Env:$($k.Name)" -Value $answer
        if ($k.CcrProvider) { $changed[$k.CcrProvider] = $answer }
        Write-Host "  -> gespeichert (dauerhaft, Benutzer-Ebene)." -ForegroundColor Green
    } else {
        Write-Host "  -> unveraendert."
    }
    $answer = $null
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
        Write-Host "WARNUNG: Router-Config konnte nicht aktualisiert werden." -ForegroundColor Yellow
        Write-Host "Bitte manuell pruefen: $cfgFile" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "FERTIG. Wichtig: Schon offene Fenster (Supervisor, Spiel) sehen die" -ForegroundColor Green
Write-Host "neuen Keys NICHT - einmal schliessen und neu starten (start_game.bat)." -ForegroundColor Green
Write-Host "Codex CLI: 'codex login'; Antigravity CLI: 'agy' einmal interaktiv anmelden."
Write-Host "Im Arena-Menue 'Codex CLI' / 'Antigravity CLI' nutzt diesen Login (Kontolimits gelten)."
Write-Host "Gemini CLI unterstuetzt keinen persoenlichen Google-Login mehr; dafuer Antigravity waehlen."
Write-Host "Mistral und Moonshot brauchen keinen claude-code-router."
pause
