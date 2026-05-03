@echo off
REM Launch the Operator Console and open it in the default browser.
REM Double-click this file to start your day.

cd /d "%~dp0\.."
start "" http://127.0.0.1:8000
python -m app.server
