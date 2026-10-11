@echo off
rem ==========================================================================
rem  Voice Tutor launcher
rem    1. Creates the Python virtual environment in .venv (inside this folder) if missing
rem    2. Installs / updates Python dependencies into it (when requirements.lock changes
rem       or packages are missing). Full pip log: logs\setup.log
rem    3. Creates .env from .env.example if missing
rem    4. Installs / builds the user interface (Next.js, needs Node.js 20.9+) when it changed
rem    5. Starts two windows: "Voice Tutor - API" (Python) and "Voice Tutor - UI" (Next.js),
rem       then opens the app in your browser. Logs: logs\voice-tutor.log and logs\ui.log
rem  Drag a PDF onto this file to open it in a new notebook.
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
title Voice Tutor - Setup

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\.venv"
if defined VOICE_TUTOR_VENV set "VENV=%VOICE_TUTOR_VENV%"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps-installed"
set "LOGDIR=%ROOT%\logs"
set "SETUPLOG=%LOGDIR%\setup.log"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
>>"%SETUPLOG%" echo.
>>"%SETUPLOG%" echo ===== run.bat %DATE% %TIME% =====

echo.
echo   Voice Tutor
echo   ------------------------------------------------------------
echo.

rem ---------------------------------------------------------------- 1. venv
if exist "%PY%" goto :venv_check

call :find_python
if not defined BASEPY (
    echo [error] Python 3.10+ was not found.
    echo         Install Python 3.13 from https://www.python.org/downloads/
    echo         ^(tick "Add python.exe to PATH"^), then run this file again.
    goto :fail
)
echo [1/5] Creating the virtual environment in .venv  ^(base: %BASEPY_DESC%^)
echo %ROOT% | findstr /i "OneDrive" >nul && (
    echo       Note: this folder is synced by OneDrive. The first install writes ~2 GB into .venv;
    echo       if it fails with "Access is denied", pause OneDrive syncing and run again.
)
>>"%SETUPLOG%" echo Creating venv at %VENV% with %BASEPY_DESC%
%BASEPY% -m venv "%VENV%" >>"%SETUPLOG%" 2>&1
if not exist "%PY%" (
    echo [error] Creating the virtual environment failed. Details: logs\setup.log
    goto :fail
)

:venv_check
"%PY%" -c "import sys; sys.exit(0 if sys.prefix != sys.base_prefix and sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [error] "%VENV%" is not a working Python 3.10+ virtual environment.
    echo         Delete the .venv folder and run this file again.
    goto :fail
)
"%PY%" -c "import platform; print(platform.python_version())" > "%VENV%\pyversion.txt" 2>nul
set /p PYVER=<"%VENV%\pyversion.txt"
echo [1/5] Virtual environment: %VENV%  ^(Python %PYVER%^)

rem ---------------------------------------------------------------- 2. dependencies
if not exist "%STAMP%" goto :install
fc /b "requirements.lock" "%STAMP%" >nul 2>&1
if errorlevel 1 (
    echo [2/5] requirements.lock changed: updating dependencies
    goto :install
)
"%PY%" -c "import importlib.util as u, sys; mods = ['fastapi', 'uvicorn', 'lancedb', 'torch', 'torchvision', 'sentence_transformers', 'google.genai', 'pymupdf4llm', 'ddgs']; sys.exit(0 if all(u.find_spec(m) for m in mods) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [2/5] Some packages are missing or broken: reinstalling
    goto :install
)
echo [2/5] Python dependencies are installed in the virtual environment
goto :env

:install
echo [2/5] Installing dependencies into .venv. The first time downloads ~1-2 GB and takes a few minutes.
echo       Full log: logs\setup.log
"%PY%" -m pip install --upgrade pip --disable-pip-version-check --quiet --log "%SETUPLOG%"
if errorlevel 1 goto :install_failed
"%PY%" -m pip install -r requirements.lock --extra-index-url https://download.pytorch.org/whl/cpu --disable-pip-version-check --log "%SETUPLOG%"
if errorlevel 1 goto :install_failed
"%PY%" -c "import fastapi, uvicorn, lancedb, torch, torchvision, sentence_transformers, google.genai, pymupdf4llm, ddgs" >>"%SETUPLOG%" 2>&1
if errorlevel 1 goto :install_failed
copy /y "requirements.lock" "%STAMP%" >nul
echo [2/5] Dependencies installed
goto :env

:install_failed
echo [error] Installing dependencies failed. The reason is at the end of logs\setup.log
echo         Common fixes: check your internet connection; pause OneDrive syncing; run this file again.
goto :fail

rem ---------------------------------------------------------------- 3. .env
:env
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [3/5] Created .env from .env.example
) else (
    echo [3/5] Found .env
)
findstr /r /c:"^GEMINI_API_KEY=..*" ".env" >nul 2>&1
if errorlevel 1 (
    echo       GEMINI_API_KEY is empty: voice is disabled until you add your free key to .env
    echo       ^(get one at https://aistudio.google.com/apikey^), then restart.
)

rem ---------------------------------------------------------------- 4. user interface
echo [4/5] User interface
"%PY%" ui.py build
if errorlevel 1 goto :fail

rem ---------------------------------------------------------------- 5. servers
set "API_UP=0"
set "UI_UP=0"
"%PY%" app.py --check >nul 2>&1 && set "API_UP=1"
"%PY%" ui.py check >nul 2>&1 && set "UI_UP=1"
if "%API_UP%%UI_UP%"=="11" (
    echo [5/5] The app is already running; opening it in your browser.
    "%PY%" ui.py open %*
    goto :done
)
if "%API_UP%"=="0" (
    echo [5/5] Starting the API in its own window: "Voice Tutor - API"  ^(logs\voice-tutor.log^)
    start "Voice Tutor - API" /D "%ROOT%" cmd /k call "%PY%" app.py
    >>"%SETUPLOG%" echo Started API window
)
rem If only the API was missing (the UI was already up), open the app once the API is ready.
if "%UI_UP%"=="0" (
    echo [5/5] Starting the interface in its own window: "Voice Tutor - UI"  ^(logs\ui.log^)
    echo       The browser opens automatically when both are ready ^(with EMBEDDING_MODEL=offline,
    echo       the first start also downloads the ~1.5 GB embedding model once^).
    start "Voice Tutor - UI" /D "%ROOT%" cmd /k call "%PY%" ui.py serve %*
    >>"%SETUPLOG%" echo Started UI window
) else (
    start "Voice Tutor - Opening" /MIN "%PY%" ui.py open --wait %*
)

:done
echo.
echo   You can close this window. To stop the app, close the "Voice Tutor - API" and
echo   "Voice Tutor - UI" windows.
timeout /t 8 >nul 2>&1
endlocal
exit /b 0

:fail
>>"%SETUPLOG%" echo FAILED
echo.
pause
endlocal
exit /b 1

rem ---------------------------------------------------------------- helpers
:find_python
rem Prefer the python.org 3.13 build; skip conda/Anaconda interpreters (venvs from them are fragile).
set "BASEPY="
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined BASEPY call :try_python "py -%%V" "Python %%V (py launcher)"
)
if not defined BASEPY call :try_python "python" "python on PATH"
exit /b 0

:try_python
%~1 -c "import sys, os; sys.exit(0 if sys.version_info >= (3, 10) and not os.path.exists(os.path.join(sys.prefix, 'conda-meta')) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "BASEPY=%~1"
    set "BASEPY_DESC=%~2"
)
exit /b 0
