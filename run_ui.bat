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
echo ===================================================
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
"%CD%\%ALT_VENV%\Scripts\python.exe" -X utf8 -u -m ui.launcher
set "ALTEREGO_LAUNCH_EXIT=%ERRORLEVEL%"
popd
if not "%ALTEREGO_LAUNCH_EXIT%"=="0" (
    echo [ERROR] Launcher exited with code %ALTEREGO_LAUNCH_EXIT%
    echo [ERROR] Check the last startup phase and logs\startup-*.log
    pause
)
exit /b %ALTEREGO_LAUNCH_EXIT%
