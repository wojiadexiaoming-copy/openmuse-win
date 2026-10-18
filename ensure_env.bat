@echo off
rem =====================================================================
rem  AlterEgo - per-machine Python environment bootstrap
rem
rem  The project folder is shared between a host PC and Hyper-V VMs, but
rem  a venv only works on the machine that created it (it hardcodes an
rem  absolute Python path). So every machine gets its own
rem  ".venv-<COMPUTERNAME>". This script:
rem    1. picks the venv that belongs to THIS machine (a legacy ".venv"
rem       is accepted when it actually runs here),
rem    2. creates it with a local Python when missing,
rem    3. installs / updates dependencies from requirements.txt.
rem
rem  Usage:  call ensure_env.bat     (run_ui.bat / run_cli.bat do this)
rem          ensure_env.bat          (standalone; sets ALT_VENV on success)
rem          set ALTER_PY=...        (optional: pick a specific python.exe)
rem          set ALTER_PIP_INDEX=... (optional: pip mirror, e.g. a
rem                                  Tsinghua/Aliyun mirror for CN networks)
rem =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
    echo [env] ERROR: cannot enter the project directory.
    exit /b 1
)

set "VENV="
set "FRESH="

rem 1) Machine-specific venv (the normal case)
if exist ".venv-%COMPUTERNAME%\Scripts\python.exe" (
    ".venv-%COMPUTERNAME%\Scripts\python.exe" -c "pass" >nul 2>nul
    if not errorlevel 1 set "VENV=.venv-%COMPUTERNAME%"
)

rem 2) Legacy shared ".venv": trusted only when it actually runs on THIS machine
if not defined VENV if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "pass" >nul 2>nul
    if not errorlevel 1 set "VENV=.venv"
)

rem 3) Nothing usable -> create a fresh machine-specific venv
if not defined VENV (
    set "VENV=.venv-%COMPUTERNAME%"
    goto :create_venv
)
goto :check_deps

:create_venv
set "PYCMD="
if defined ALTER_PY set "PYCMD=%ALTER_PY%"
if defined PYCMD goto :do_create
where py >nul 2>nul
if not errorlevel 1 (set "PYCMD=py -3" & goto :do_create)
where python >nul 2>nul
if not errorlevel 1 (set "PYCMD=python" & goto :do_create)
echo [env] ERROR: no Python found on PATH.
echo [env] Install Python 3.11+ from python.org, or set ALTER_PY to a python.exe path.
exit /b 1

:do_create
echo [env] Creating %VENV% with "%PYCMD%" ...
%PYCMD% -m venv "%VENV%"
if errorlevel 1 (
    echo [env] ERROR: failed to create the virtual environment.
    exit /b 1
)
set "FRESH=1"

:check_deps
set "STAMP_FILE=%VENV%\.requirements.stamp"
for %%I in ("requirements.txt") do (set "R_SIZE=%%~zI" & set "R_TIME=%%~tI")
set "REQ_STAMP=%R_SIZE% %R_TIME%"
set "OLD_STAMP="
if exist "%STAMP_FILE%" set /p OLD_STAMP=<"%STAMP_FILE%"
if defined FRESH goto :install_deps
if not "%OLD_STAMP%"=="%REQ_STAMP%" goto :install_deps
echo [env] OK: %VENV% (dependencies up to date)
goto :done

:install_deps
set "PIP_INDEX_ARG="
if defined ALTER_PIP_INDEX set "PIP_INDEX_ARG=--index-url %ALTER_PIP_INDEX%"
echo [env] Installing dependencies into %VENV% ...
"%VENV%\Scripts\python.exe" -m pip --version >nul 2>nul
if not errorlevel 1 goto :install_with_pip
rem uv-style venv: no pip inside -> reuse uv when available
where uv >nul 2>nul
if not errorlevel 1 (
    echo [env] pip is not available in this venv; using uv instead.
    uv pip install %PIP_INDEX_ARG% --python "%CD%\%VENV%\Scripts\python.exe" -r requirements.txt
    if errorlevel 1 (
        echo [env] ERROR: uv pip install failed. Check network and re-run.
        exit /b 1
    )
    goto :stamp
)
"%VENV%\Scripts\python.exe" -m ensurepip --upgrade >nul 2>nul
"%VENV%\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [env] ERROR: neither pip nor uv can install dependencies here.
    exit /b 1
)

:install_with_pip
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check %PIP_INDEX_ARG% --upgrade pip >nul 2>nul
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check %PIP_INDEX_ARG% -r requirements.txt
if errorlevel 1 (
    echo [env] ERROR: pip install failed. Check network/proxy and re-run this script.
    exit /b 1
)

:stamp
> "%STAMP_FILE%" echo %REQ_STAMP%
echo [env] OK: %VENV% ready.

:done
endlocal & set "ALT_VENV=%VENV%"
exit /b 0
