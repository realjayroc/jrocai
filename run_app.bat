@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 jroc_bridge.py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or later was not found. Install Python and enable "Add Python to PATH".
        pause
        exit /b 1
    )
    python jroc_bridge.py
)
if errorlevel 1 (
    echo jrocai exited with an error.
    pause
    exit /b 1
)
endlocal
