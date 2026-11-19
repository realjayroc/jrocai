@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 ecosystem_hub.py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or later was not found. Install Python and enable "Add Python to PATH".
        pause
        exit /b 1
    )
    python ecosystem_hub.py
)
if errorlevel 1 (
    echo Jrocai ecosystem hub exited with an error.
    pause
    exit /b 1
)
endlocal
