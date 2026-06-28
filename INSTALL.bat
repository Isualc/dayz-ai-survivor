@echo off
rem ===========================================================================
rem  dayz-ai-survivor - One-click installer
rem ===========================================================================
rem  Double-click this file. It launches the guided PowerShell setup wizard,
rem  which detects your Steam/DayZ install, fetches the prerequisites, links
rem  the server mods and gets you to "game ready". Only the API keys and the
rem  optional Discord bot stay manual - the wizard walks you through those too.
rem
rem  No admin rights are required for the normal path. If a tool install
rem  (Python/Node) needs elevation, Windows asks for it on its own.
rem ===========================================================================
cd /d "%~dp0"
echo Starting the dayz-ai-survivor setup wizard...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\installer\install.ps1" %*
set EXITCODE=%ERRORLEVEL%
echo.
if not "%EXITCODE%"=="0" (
    echo The wizard reported a problem (exit code %EXITCODE%^). See the messages above.
)
pause
exit /b %EXITCODE%
