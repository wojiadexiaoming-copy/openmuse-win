@echo off
setlocal
title AlterEgo CLI
pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Cannot access the AlterEgo project directory.
    pause
    exit /b 1
)
echo ===================================================
echo   AlterEgo - CLI Terminal
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
"%CD%\%ALT_VENV%\Scripts\python.exe" -X utf8 -u -m agent.main
set "ALTEREGO_LAUNCH_EXIT=%ERRORLEVEL%"
popd
if not "%ALTEREGO_LAUNCH_EXIT%"=="0" (
    echo [ERROR] Exit with error code %ALTEREGO_LAUNCH_EXIT%
    pause
)
exit /b %ALTEREGO_LAUNCH_EXIT%
