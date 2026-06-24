@echo off
rem IsuSurvivor One-Shot-Start: Server + Agent in einem.
rem Doppelklick reicht. Argumente gehen an run_agent.py durch,
rem z.B.:  start_all.bat --idle 300 --model sonnet
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "tools\start_all.ps1" %*
if errorlevel 1 pause
