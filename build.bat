@echo off
rem Build a standalone SimpleMail.exe with PyInstaller.
rem Run this ONCE on an x64 Windows machine, and once on an ARM64 Windows
rem machine, to get a native .exe for each architecture.
rem
rem Prerequisites (run once):
rem   py -3 -m pip install pyinstaller pywebview==5.3.2 pythonnet==3.0.5 bottle

cd /d "%~dp0"
setlocal

set PY=

where py >nul 2>nul
if %errorlevel%==0 ( set PY=py -3 ) else ( set PY=python )

%PY% patch_pywebview.py

%PY% -m pip show pyinstaller >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing PyInstaller...
    %PY% -m pip install pyinstaller
)

%PY% make_icon.py

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name SimpleMail ^
    --icon assets\icon.ico ^
    --add-data "web;web" ^
    --add-data "assets;assets" ^
    --add-data "runtimeconfig.json;." ^
    --hidden-import webview.platforms.winforms ^
    mailapp.py

echo.
echo Build complete. The .exe is in the "dist" folder.
echo Note: build on x64 for x64 machines, build on ARM64 for ARM64 machines.
pause
