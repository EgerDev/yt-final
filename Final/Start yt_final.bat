@echo off
REM DisableDelayedExpansion (explicitly — the machine-wide registry default can be ON):
REM only :notstore uses !var! (under its own nested setlocal); out here delayed expansion
REM would silently EAT '!' characters out of
REM expanded paths (%LOCALAPPDATA%, %%p), causing a false 'No usable Python found'.
setlocal DisableDelayedExpansion
title yt_final
cd /d "%~dp0"

echo.
echo  Starting yt_final...
echo  Browser will open. Your login password is shown below and saved to webui.txt.
echo  Leave THIS window open while you use the app.
echo.

REM PYCMD carries its own quoting so it can hold either a full path or "py -3".
set "PYCMD="

REM 1) An operator-created project venv may be used. Never probe or execute the
REM app-data managed target: it is writable install state, not trusted startup code.
if exist ".venv\Scripts\python.exe" (
  call ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set PYCMD=".venv\Scripts\python.exe"
)

REM 2) The py launcher — ships with the python.org installer, absent on a stock Windows.
REM    Probe by actually RUNNING it; `where py` succeeding does not prove it works.
if not defined PYCMD (
  call py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set PYCMD=py -3
)

REM 3) Bare `python` — but REJECT the Microsoft Store App Execution Alias. A stub named
REM    python.exe lives in %LOCALAPPDATA%\Microsoft\WindowsApps (which is on the default
REM    PATH); it opens the Store instead of running code, which is the classic "the
REM    launcher did nothing" failure on a fresh machine.
REM    Pass 1 prefers a NON-WindowsApps python. Pass 2 then allows a WindowsApps path, but only
REM    if it actually runs Python — a real Store install lives at that same path shape as the stub,
REM    so rejecting the shape outright would dead-end a user whose only Python IS the Store one.
REM    (Substring test, not `echo %%p| find`: piping re-parses the path in a child cmd, so a path
REM    containing & ( ) would break the match and leak a stray error.)
if not defined PYCMD (
  for /f "delims=" %%p in ('where python 2^>nul') do (
    if not defined PYCMD (
      call :notstore "%%p"
      if not errorlevel 1 (
        call "%%p" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set PYCMD="%%p"
      )
    )
  )
)
if not defined PYCMD (
  for /f "delims=" %%p in ('where python 2^>nul') do (
    if not defined PYCMD (
      REM No size gate here. Pass 1 already excluded every WindowsApps path, so this pass exists
      REM solely to admit a user whose ONLY Python is the Store install — gating on %%~zs made that
      REM unreachable (and the "a real Store alias is not zero-length" premise was never verified).
      REM The probe itself is the filter: the disabled stub fails `-c "import sys"`.
      call "%%p" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
      if not errorlevel 1 set PYCMD="%%p"
    )
  )
)

if not defined PYCMD (
  echo  [X] No usable Python 3.11 or newer found.
  echo.
  echo      The app requires Python 3.11 or newer. Windows ships a placeholder "python"
  echo      that only opens the Microsoft Store, so the app cannot start. Install Python 3.11+ from:
  echo          https://www.python.org/downloads/
  echo      and TICK "Add python.exe to PATH" in the installer. Then run this file again.
  echo.
  pause
  exit /b 9009
)

echo  Using Python: %PYCMD%
echo.
call %PYCMD% yt_final.py serve
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo  [X] yt_final exited with error code %RC%.
  echo.
  echo      Common causes:
  echo        - Missing packages:  %PYCMD% -m pip install -r requirements.txt
  echo        - Port 8765 already in use ^(another copy still running^)
  echo        - See final_yt.log in %LOCALAPPDATA%\yt_final for the traceback
) else (
  echo  yt_final stopped.
)
echo.
pause
exit /b %RC%

REM --- helpers -------------------------------------------------------------
REM :notstore <path>  -> errorlevel 0 if it is NOT a Microsoft Store alias stub, 1 if it is.
REM Uses %%~1 (dequoted) and a substring replacement, so no child cmd re-parses the path.
:notstore
REM Set CAND with delayed expansion OFF so a '!' in the path survives, THEN enable it to read.
set "CAND=%~1"
setlocal EnableDelayedExpansion
if /i "!CAND:WindowsApps\=!" neq "!CAND!" (endlocal & exit /b 1)
endlocal & exit /b 0
