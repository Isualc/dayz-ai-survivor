@echo off
rem IsuSurvivor Spiel SAUBER schliessen: Agenten -> Voice/Backends -> Client
rem -> zuletzt der Server GRACEFUL (taskkill ohne /F), damit die Persistenz
rem (Zelte/Tonnen/Autos + Inhalt) nach storage_1 gespeichert wird.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "tools\close_game.ps1"
if errorlevel 1 pause
timeout /t 5 >nul
