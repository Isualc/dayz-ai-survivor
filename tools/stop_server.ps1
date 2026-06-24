# Beendet den DayZ-Server SAUBER, damit die Persistenz (Zelte, Tonnen, Autos
# + Inhalt) nach storage_1 gespeichert wird. ASCII only (PS 5.1 safe).
#
# WICHTIG: DayZ speichert Base-Objekte NICHT periodisch, sondern nur beim
# geordneten Shutdown. Ein Hart-Kill (Stop-Process -Force / taskkill /F)
# verliert alles seit dem letzten Save. Darum hier taskkill OHNE /F (sendet
# eine normale Close-Anforderung) und Warten auf den Save-Beweis im RPT.

param(
    [string]$ServerDir = $(if ($env:DAYZ_SERVER_DIR) { $env:DAYZ_SERVER_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\DayZServer" })
)

$proc = Get-CimInstance Win32_Process -Filter "Name='DayZServer_x64.exe'"
if (-not $proc) {
    Write-Host "Kein DayZServer-Prozess laeuft - nichts zu beenden."
    exit 0
}

# Neuestes RPT VOR dem Stop merken (dort erscheint die Save-/Termination-Zeile)
$rpt = Get-ChildItem "$ServerDir\profiles\*.RPT" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host "Beende Server SAUBER (graceful, kein Force) - speichert Persistenz..."
foreach ($p in $proc) {
    # taskkill OHNE /F = normale Close-Anforderung, Server flusht storage_1
    taskkill /pid $p.ProcessId | Out-Null
}

# Bis zu 60 s auf sauberes Ende warten (Save kann ein paar Sekunden dauern)
$deadline = (Get-Date).AddSeconds(60)
$saved = $false
while ((Get-Date) -lt $deadline) {
    $still = Get-CimInstance Win32_Process -Filter "Name='DayZServer_x64.exe'"
    if (-not $still) { $saved = $true; break }
    Start-Sleep -Seconds 2
}

if ($saved) {
    Write-Host "Server beendet." -ForegroundColor Green
    if ($rpt) {
        $tail = Get-Content $rpt.FullName -Tail 8 -ErrorAction SilentlyContinue
        if ($tail -match "Termination successfully completed") {
            Write-Host "Save bestaetigt: 'Termination successfully completed' im RPT." -ForegroundColor Green
        } else {
            Write-Host "Hinweis: Save-Zeile im RPT-Ende nicht gefunden (evtl. noch am Schreiben)." -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "Server reagiert nach 60 s nicht auf den sauberen Stop." -ForegroundColor Yellow
    Write-Host "Erst NACH diesem Hinweis als letzten Ausweg hart beenden:" -ForegroundColor Yellow
    Write-Host "  taskkill /im DayZServer_x64.exe /F   (Persistenz seit letztem Save geht verloren)" -ForegroundColor Yellow
}
