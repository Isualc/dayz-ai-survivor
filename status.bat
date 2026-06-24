@echo off
rem Wer lebt? Zeigt Gehirn- und Koerper-Status aller vier Agenten.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "tools\status.ps1"
pause
