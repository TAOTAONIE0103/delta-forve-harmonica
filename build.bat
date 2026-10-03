@echo off
rem ============================================================
rem  Build DeltaHarmonica.exe  (PySide6 + PyInstaller)
rem    build.bat            -> folder build (dist\DeltaHarmonica\, recommended)
rem    build.bat onefile    -> single file  (dist\DeltaHarmonica.exe)
rem  Requires .venv-qt with PySide6-Essentials and pyinstaller.
rem  Keep CRLF line endings. Do not convert.
rem ============================================================
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "VPY=%HERE%.venv-qt\Scripts\python.exe"

if not exist "%VPY%" (
    echo.
    echo   [ERROR] .venv-qt not found.
    echo   Create it first:
    echo       python -m venv .venv-qt
    echo       .venv-qt\Scripts\python -m pip install PySide6-Essentials pyinstaller
    echo.
    pause
    exit /b 1
)

set "PACK=--onedir"
if /i "%~1"=="onefile" set "PACK=--onefile"

echo Building with %PACK% ...
echo   onedir  = dist\DeltaHarmonica\DeltaHarmonica.exe  ^(start fast, verified^)
echo   onefile = dist\DeltaHarmonica.exe                ^(single file^)
echo.
"%VPY%" -m PyInstaller --noconfirm --clean --windowed %PACK% ^
  --name DeltaHarmonica ^
  --icon "%HERE%assets\icon.ico" ^
  --distpath "%HERE%dist" --workpath "%HERE%build" --specpath "%HERE%build" ^
  --exclude-module tkinter ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQuick3D ^
  --exclude-module PySide6.QtQuickWidgets ^
  --exclude-module PySide6.QtQuickControls2 ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtWebChannel ^
  --exclude-module PySide6.QtWebSockets ^
  --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.Qt3DRender ^
  --exclude-module PySide6.Qt3DInput ^
  --exclude-module PySide6.Qt3DLogic ^
  --exclude-module PySide6.Qt3DAnimation ^
  --exclude-module PySide6.Qt3DExtras ^
  --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization ^
  --exclude-module PySide6.QtGraphs ^
  --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtMultimediaWidgets ^
  --exclude-module PySide6.QtSpatialAudio ^
  --exclude-module PySide6.QtPdf ^
  --exclude-module PySide6.QtPdfWidgets ^
  --exclude-module PySide6.QtSql ^
  --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtDesigner ^
  --exclude-module PySide6.QtHelp ^
  --exclude-module PySide6.QtUiTools ^
  --exclude-module PySide6.QtBluetooth ^
  --exclude-module PySide6.QtNfc ^
  --exclude-module PySide6.QtPositioning ^
  --exclude-module PySide6.QtLocation ^
  --exclude-module PySide6.QtSerialPort ^
  --exclude-module PySide6.QtSensors ^
  --exclude-module PySide6.QtTextToSpeech ^
  --exclude-module PySide6.QtVirtualKeyboard ^
  --exclude-module PySide6.QtRemoteObjects ^
  --exclude-module PySide6.QtScxml ^
  --exclude-module PySide6.QtStateMachine ^
  "%HERE%harp_qt.py"

if errorlevel 1 (
    echo.
    echo   [ERROR] build failed.
    pause
    exit /b 1
)
echo.
echo   Done. See the dist folder.
dir /b "%HERE%dist"
pause
