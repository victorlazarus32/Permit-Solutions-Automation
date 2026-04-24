@echo off
REM ===========================================================================
REM  Start the Day — Permit Solutions Automation
REM
REM  Double-click this file (or a shortcut to it) to run the morning routine.
REM  The employee should never need to touch a terminal directly.
REM
REM  To put this on the desktop:
REM    Right-click this file  ->  Send to  ->  Desktop (create shortcut)
REM    Then rename the shortcut to "Start the Day".
REM ===========================================================================

setlocal
title Start the Day - Permit Solutions

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  PROBLEM: The Python environment isn't set up on this computer yet.
    echo           ^(.venv\Scripts\python.exe is missing^)
    echo.
    echo  What to do: Tell Victor — someone needs to run the one-time setup.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "scripts\morning_run.py"
set "RUN_EXIT=%ERRORLEVEL%"

echo.
echo.
echo ===========================================================
echo  Press any key to close this window.
echo ===========================================================
pause >nul

exit /b %RUN_EXIT%
