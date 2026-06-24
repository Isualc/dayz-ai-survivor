@echo off
rem IsuSurvivor Spielstart (Menue-Workflow): Server + Supervisor + Client.
rem Die Agenten startest du danach IM SPIEL: Taste Einfg -> Arena-Menue.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "tools\start_game.ps1"
if errorlevel 1 pause

