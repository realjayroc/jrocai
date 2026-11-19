@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m unittest discover -s tests -v
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or later was not found. Install Python and enable "Add Python to PATH".
        pause
        exit /b 1
    )
    python -m unittest discover -s tests -v
)
if errorlevel 1 (
    echo Tests failed.
    pause
    exit /b 1
)
echo All tests passed.
pause
endlocal
