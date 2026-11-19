@echo off
setlocal
cd /d "%~dp0"
echo Choose the preferred model provider for this launch:
echo 1. OpenRouter
echo 2. Google AI Studio Gemini
echo 3. Groq
echo 4. GitHub Models
echo 5. Local Ollama
echo 6. Automatic provider order
set /p choice="Selection (1-6): "
set "JROCAI_PROVIDERS="
if "%choice%"=="1" set "JROCAI_PROVIDERS=openrouter"
if "%choice%"=="2" set "JROCAI_PROVIDERS=gemini"
if "%choice%"=="3" set "JROCAI_PROVIDERS=groq"
if "%choice%"=="4" set "JROCAI_PROVIDERS=github_models"
if "%choice%"=="5" set "JROCAI_PROVIDERS=ollama"
if "%choice%"=="6" set "JROCAI_PROVIDERS="
if not defined JROCAI_PROVIDERS if not "%choice%"=="6" (
    echo Invalid selection.
    pause
    exit /b 1
)
call "%~dp0run_app.bat"
endlocal
