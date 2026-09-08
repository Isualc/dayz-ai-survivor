# Shared launcher language selection. Dot-source this file; it starts no processes.

function Get-IsuUiLanguage {
    param([Parameter(Mandatory = $true)][string]$StatePath)
    if (Test-Path -LiteralPath $StatePath) {
        $saved = ([System.IO.File]::ReadAllText($StatePath)).Trim().ToLowerInvariant()
        if ($saved -in @('de', 'en')) { return $saved }
    }
    return 'en'
}

function Set-IsuUiLanguage {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][ValidateSet('de', 'en')][string]$UiLanguage
    )
    $language = $UiLanguage.ToLowerInvariant()
    $parentDir = Split-Path -Parent $StatePath
    if ($parentDir -and -not (Test-Path -LiteralPath $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }
    # The game reads one plain ASCII line. No BOM is written into its settings.
    [System.IO.File]::WriteAllText($StatePath, $language + "`r`n", [System.Text.Encoding]::ASCII)
    return $language
}

function Select-IsuUiLanguage {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [ValidateSet('de', 'en')][string]$UiLanguage
    )
    if ($UiLanguage) {
        return (Set-IsuUiLanguage -StatePath $StatePath -UiLanguage $UiLanguage)
    }
    $language = Get-IsuUiLanguage -StatePath $StatePath
    $defaultName = 'English'
    if ($language -eq 'de') { $defaultName = 'Deutsch' }
    Write-Host 'UI language / Menüsprache: 1) English  2) Deutsch' -ForegroundColor Cyan
    while ($true) {
        $answer = Read-Host ("Choose / Auswahl [en/de], Enter = {0}" -f $defaultName)
        if ([string]::IsNullOrWhiteSpace($answer)) { break }
        switch ($answer.Trim().ToLowerInvariant()) {
            { $_ -in @('1', 'en', 'english') } { $language = 'en'; break }
            { $_ -in @('2', 'de', 'deutsch') } { $language = 'de'; break }
            default { Write-Host 'Please choose en or de / Bitte en oder de wählen.' -ForegroundColor Yellow; continue }
        }
        if ($answer.Trim().ToLowerInvariant() -in @('1', 'en', 'english', '2', 'de', 'deutsch')) { break }
    }
    return (Set-IsuUiLanguage -StatePath $StatePath -UiLanguage $language)
}

function Get-IsuUiText {
    param(
        [Parameter(Mandatory = $true)][string]$English,
        [Parameter(Mandatory = $true)][string]$German,
        [ValidateSet('de', 'en')][string]$UiLanguage = 'en'
    )
    if ($UiLanguage -eq 'de') { return $German }
    return $English
}
