@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0.."

set "LOGFILE=%~dp0keep_session_result.txt"
set "SID="

echo Run time: %date% %time% > "%LOGFILE%"

for /f "tokens=1,2,3" %%a in ('query session ^| findstr /i /c:"rdp-tcp#"') do (
    echo candidate: a=%%a b=%%b c=%%c >> "%LOGFILE%"
    set "SID=%%c"
)

echo Parsed SID=%SID% >> "%LOGFILE%"

if not defined SID (
    echo [ERROR] No active rdp-tcp# session found. >> "%LOGFILE%"
    query session >> "%LOGFILE%" 2>&1
    echo [ERROR] No active RDP session found. Are you inside RDP?
    type "%LOGFILE%"
    pause
    exit /b 1
)

echo %SID% | findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERROR] Parsed SID "%SID%" is not numeric - aborting. >> "%LOGFILE%"
    echo [ERROR] Parsed SID "%SID%" is not numeric.
    query session
    pause
    exit /b 1
)

echo Found RDP session ID = %SID% >> "%LOGFILE%"
echo Found RDP session ID = %SID%

set "PYEXE=python"
if exist "%~dp0..\venv\Scripts\python.exe" set "PYEXE=%~dp0..\venv\Scripts\python.exe"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"

echo Starting delayed desktop snapshot (45s) via: %PYEXE% >> "%LOGFILE%"
start "snapshot" /B cmd /c ""%PYEXE%" "%~dp0snapshot_headless.py" 45"

echo Running: tscon %SID% /dest:console
echo Your RDP window will disconnect now - this is NORMAL.
echo Wait 60s, then test booking from another PC.

tscon %SID% /dest:console >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo tscon exit code = %RC% >> "%LOGFILE%"

if "%RC%"=="0" (
    echo [OK] Session switched to console. Desktop stays active. >> "%LOGFILE%"
    echo [OK] Session switched to console. Snapshot -> scripts\headless_snapshot.txt
    echo [OK] Closing this cmd window so it does not stay in foreground and block GUI clicks.
    rem Close the cmd window entirely - otherwise it stays in the console foreground
    rem after tscon, and the booking driver's physical clicks hit this window / desktop
    rem instead of the WeMeet client.
    exit
) else (
    echo [FAIL] tscon failed with code %RC%. Run as Administrator. >> "%LOGFILE%"
    echo [FAIL] tscon failed, code %RC%. See %LOGFILE%
    type "%LOGFILE%"
    pause
)
