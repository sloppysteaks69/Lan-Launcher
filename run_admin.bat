@echo off
rem Starts LAN Party Launcher as administrator (needed for Internet Party: n2n's edge.exe requires it).
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
python -m lanparty %*
if errorlevel 1 pause
