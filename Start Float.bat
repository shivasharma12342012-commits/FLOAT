@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  Start Float.bat
rem
rem  You shouldn't normally see this window — double-click
rem  "Start Float.vbs" instead, and this runs invisibly behind it.
rem
rem  Running this file directly (not the .vbs) is useful once:
rem  if something goes wrong and you want to see the actual error
rem  instead of a silent failure, run this one by hand.
rem ============================================================

cd /d "%~dp0"

rem --- find a working Python -------------------------------------------
set "PY="
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 set "PY=py"
)

if "%PY%"=="" (
    call :fail "Float needs Python, and it isn't installed (or isn't on PATH).\n\nInstall it from python.org/downloads - during setup, tick 'Add python.exe to PATH' - then double-click Start Float again."
    exit /b 1
)

rem --- first run only: install the optional extras ----------------------
if not exist "data\.deps_installed" (
    %PY% -m pip install -r requirements.txt --quiet --disable-pip-version-check >"%TEMP%\float_pip.log" 2>&1
    if not exist "data" mkdir "data"
    echo ok > "data\.deps_installed"
)

rem --- launch, with no console window if possible ------------------------
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "main.py"
) else (
    start "" %PY% "main.py"
)

exit /b 0

:fail
set "MSG=%~1"
set "MSG=%MSG:\n=&echo:%"
mshta "javascript:var sh=new ActiveXObject('WScript.Shell');sh.Popup('%~1',0,'Float',16);close();"
exit /b 1
