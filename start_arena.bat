@echo off
rem ISU Modell-Arena: mehrere Claude-Modelle als Survivor gleichzeitig.
rem   start_arena.bat                -> Menue (Auswahl + Gesinnung)
rem   start_arena.bat alle n         -> alle vier, neutral
rem   start_arena.bat viktor,igor f  -> zwei Agenten, feindlich
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "tools\start_arena.ps1" %1 %2
if errorlevel 1 pause
