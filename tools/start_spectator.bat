@echo off
rem IsuSurvivor Zuschauer-Server: read-only Dashboard fuer OBS/Twitch.
rem   tools\start_spectator.bat            -> Port 8090, oeffnet Browser
rem   tools\start_spectator.bat 8123       -> anderer Port
rem   tools\start_spectator.bat 8090 no    -> Browser NICHT oeffnen (OBS-Quelle)
cd /d "%~dp0\.."

set ISU_SPECTATOR_PORT=8090
if not "%~1"=="" set ISU_SPECTATOR_PORT=%~1

echo === IsuSurvivor Zuschauer-Server ===
echo Port: %ISU_SPECTATOR_PORT%
echo Dashboard: http://127.0.0.1:%ISU_SPECTATOR_PORT%/
echo API:       http://127.0.0.1:%ISU_SPECTATOR_PORT%/api/state
echo Beenden mit Strg+C.
echo.

if "%~2"=="no" goto :run
start "" "http://127.0.0.1:%ISU_SPECTATOR_PORT%/"

:run
python daemon\spectator_server.py --port %ISU_SPECTATOR_PORT%
if errorlevel 1 pause
