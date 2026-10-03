@echo off
rem ============================================================
rem  Delta Harmonica Auto-Player  /  launcher
rem  Order: packaged build -> PySide6 source in .venv-qt ->
rem         tkinter version with any usable Python.
rem  Keep CRLF line endings. Do not convert.
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "DRY="
if /i "%~1"=="/dry" set "DRY=1"

rem ---------- 1) packaged build ----------
if exist "%HERE%dist-qt\DeltaHarmonica\DeltaHarmonica.exe" (
    if defined DRY (echo DRY_RESOLVED=%HERE%dist-qt\DeltaHarmonica\DeltaHarmonica.exe& exit /b 0)
    echo Starting packaged build ...
    start "" "%HERE%dist-qt\DeltaHarmonica\DeltaHarmonica.exe"
    exit /b 0
)
if exist "%HERE%dist\DeltaHarmonica\DeltaHarmonica.exe" (
    if defined DRY (echo DRY_RESOLVED=%HERE%dist\DeltaHarmonica\DeltaHarmonica.exe& exit /b 0)
    echo Starting packaged build ...
    start "" "%HERE%dist\DeltaHarmonica\DeltaHarmonica.exe"
    exit /b 0
)
if exist "%HERE%dist\DeltaHarmonica.exe" (
    if defined DRY (echo DRY_RESOLVED=%HERE%dist\DeltaHarmonica.exe& exit /b 0)
    echo Starting packaged build ...
    start "" "%HERE%dist\DeltaHarmonica.exe"
    exit /b 0
)

rem ---------- 2) PySide6 version from source ----------
if exist "%HERE%.venv-qt\Scripts\pythonw.exe" (
    if defined DRY (echo DRY_RESOLVED=%HERE%.venv-qt\Scripts\pythonw.exe& exit /b 0)
    echo Starting Qt version from source ...
    start "" "%HERE%.venv-qt\Scripts\pythonw.exe" "%HERE%harp_qt.py"
    exit /b 0
)

rem ---------- 3) tkinter fallback ----------
set "PY="
call :trydir "%HERE:~0,-1%"
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-*") do call :trydir "%%D"
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :trydir "%%D"
for /d %%D in ("%ProgramFiles%\Python3*") do call :trydir "%%D"
call :trydir "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python"
for %%P in (pythonw.exe) do call :trydir "%%~dp$PATH:P."

if /i "%~1"=="/dry" (
    if defined PY (echo DRY_RESOLVED=%PY%) else (echo DRY_RESOLVED=NONE)
    exit /b 0
)

if not defined PY (
    echo.
    echo   Nothing to run.
    echo.
    echo   Option A: build or unzip  dist\DeltaHarmonica\  ^(no Python needed^)
    echo   Option B: install Python 3.9+ from python.org, tick
    echo             "Add python.exe to PATH", then run this file again.
    echo   Option B needs no third-party packages ^(tkinter only^).
    echo.
    pause
    exit /b 1
)

echo Starting with: %PY%
start "" "%PY%" "%HERE%harmonica_auto.py"
exit /b 0

:trydir
if defined PY exit /b 0
if not exist "%~1\python.exe" exit /b 0
echo %~1 | findstr /i "WindowsApps" >nul && exit /b 0
"%~1\python.exe" -c "import tkinter" >nul 2>&1
if errorlevel 1 exit /b 0
if exist "%~1\pythonw.exe" (set "PY=%~1\pythonw.exe") else (set "PY=%~1\python.exe")
exit /b 0
