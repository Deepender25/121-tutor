@echo off
rem ==========================================================================
rem  Voice Tutor launcher
rem    1. Creates the virtual environment in .venv (inside this folder) if missing
rem    2. Installs / updates dependencies into it (when requirements.lock changes
rem       or packages are missing). Full pip log: logs\setup.log
rem    3. Creates .env from .env.example if missing
rem    4. Starts the server in its own terminal window ("Voice Tutor - Server")
rem       and opens the app in your browser. Server logs: logs\voice-tutor.log
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
echo [1/4] Creating the virtual environment in .venv  ^(base: %BASEPY_DESC%^)
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
echo [1/4] Virtual environment: %VENV%  ^(Python %PYVER%^)

rem ---------------------------------------------------------------- 2. dependencies
if not exist "%STAMP%" goto :install
fc /b "requirements.lock" "%STAMP%" >nul 2>&1
if errorlevel 1 (
    echo [2/4] requirements.lock changed: updating dependencies
    goto :install
)
"%PY%" -c "import importlib.util as u, sys; mods = ['fastapi', 'uvicorn', 'lancedb', 'torch', 'torchvision', 'sentence_transformers', 'google.genai', 'pymupdf4llm', 'ddgs']; sys.exit(0 if all(u.find_spec(m) for m in mods) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [2/4] Some packages are missing or broken: reinstalling
    goto :install
)
echo [2/4] Dependencies are installed in the virtual environment
goto :env

:install
echo [2/4] Installing dependencies into .venv. The first time downloads ~1-2 GB and takes a few minutes.
echo       Full log: logs\setup.log
"%PY%" -m pip install --upgrade pip --disable-pip-version-check --quiet --log "%SETUPLOG%"
if errorlevel 1 goto :install_failed
"%PY%" -m pip install -r requirements.lock --extra-index-url https://download.pytorch.org/whl/cpu --disable-pip-version-check --log "%SETUPLOG%"
if errorlevel 1 goto :install_failed
"%PY%" -c "import fastapi, uvicorn, lancedb, torch, torchvision, sentence_transformers, google.genai, pymupdf4llm, ddgs" >>"%SETUPLOG%" 2>&1
if errorlevel 1 goto :install_failed
copy /y "requirements.lock" "%STAMP%" >nul
echo [2/4] Dependencies installed
goto :env

:install_failed
echo [error] Installing dependencies failed. The reason is at the end of logs\setup.log
echo         Common fixes: check your internet connection; pause OneDrive syncing; run this file again.
goto :fail

rem ---------------------------------------------------------------- 3. .env
:env
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [3/4] Created .env from .env.example
) else (
    echo [3/4] Found .env
)
findstr /r /c:"^GEMINI_API_KEY=..*" ".env" >nul 2>&1
if errorlevel 1 (
    echo       GEMINI_API_KEY is empty: voice is disabled until you add your free key to .env
    echo       ^(get one at https://aistudio.google.com/apikey^), then restart.
)

rem ---------------------------------------------------------------- 4. server
"%PY%" app.py --open-if-running %* 2>nul
if not errorlevel 1 (
    echo [4/4] The server is already running; opened it in your browser.
    goto :done
)
echo [4/4] Starting the server in its own window: "Voice Tutor - Server"
echo       The browser opens automatically when it's ready ^(first start also downloads
echo       the ~1.5 GB embedding model once^). Logs: logs\voice-tutor.log
start "Voice Tutor - Server" /D "%ROOT%" cmd /k call "%PY%" app.py %*
>>"%SETUPLOG%" echo Started server window

:done
echo.
echo   You can close this window. To stop the app, close the "Voice Tutor - Server" window.
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
