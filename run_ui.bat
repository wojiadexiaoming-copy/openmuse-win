@echo off
setlocal
title AlterEgo Desktop
pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Cannot access the AlterEgo project directory.
    pause
    exit /b 1
)
echo ===================================================
echo   AlterEgo - Windows Desktop Launcher
set "PYDANTIC_DISABLE_PLUGINS=1"
if not defined PYTHONPYCACHEPREFIX (
    if defined LOCALAPPDATA (
        set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\AlterEgo\pycache"
    )
)
call "%~dp0ensure_env.bat"
if errorlevel 1 (
    echo [ERROR] Python environment setup failed. See messages above.
    popd
    pause
    exit /b 1
)
if not defined ALT_VENV (
    echo [ERROR] Python environment was not prepared.
    popd
    pause
    exit /b 1
)
echo [AlterEgo] Using %ALT_VENV%
echo [AlterEgo] Startup phases and slow-start stacks: logs\startup-*.log
if exist "%ALT_VENV%\Scripts\python.exe" (
    set "ALT_PY_EXE=%ALT_VENV%\Scripts\python.exe"
) else if exist "%CD%\%ALT_VENV%\Scripts\python.exe" (
    set "ALT_PY_EXE=%CD%\%ALT_VENV%\Scripts\python.exe"
) else (
    echo [ERROR] Cannot find python.exe in %ALT_VENV%
    popd
    pause
    exit /b 1
)
"%ALT_PY_EXE%" -X utf8 -u -m ui.launcher
set "ALTEREGO_LAUNCH_EXIT=%ERRORLEVEL%"
popd
if not "%ALTEREGO_LAUNCH_EXIT%"=="0" (
    echo [ERROR] Launcher exited with code %ALTEREGO_LAUNCH_EXIT%
    echo [ERROR] Check the last startup phase and logs\startup-*.log
    pause
)
exit /b %ALTEREGO_LAUNCH_EXIT%
