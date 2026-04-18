@echo off
REM autoFDX Windows setup: Python 3.12 + pip (see tools/setup_windows.ps1)
REM Requirements: tools\requirements.txt ; root requirements.txt uses -r to include it
REM Manual install: pip install -r requirements.txt
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "tools\setup_windows.ps1" (
    echo tools\setup_windows.ps1 not found.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_windows.ps1"
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    pause
    exit /b %EXIT_CODE%
)

pause
exit /b 0
