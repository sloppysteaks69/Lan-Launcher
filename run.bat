@echo off
rem Starts LAN Party Launcher and opens it in your browser.
cd /d "%~dp0"
python -m lanparty %*
if errorlevel 1 pause
