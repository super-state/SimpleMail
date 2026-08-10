@echo off
rem Build the ARM64 exe and upload it to the current GitHub release.
rem Run this on an ARM64 Windows machine after tagging a new version.
rem Usage:  publish_arm64.bat v1.0.0
rem (If the release doesn't exist yet, the x64 workflow creates it when
rem  the tag is pushed - this script then adds the ARM64 asset.)

cd /d "%~dp0"
setlocal

if "%~1"=="" (
    echo Usage: publish_arm64.bat v1.0.0
    exit /b 1
)
set TAG=%~1

where py >nul 2>nul
if %errorlevel%==0 ( set PY=py -3 ) else ( set PY=python )

%PY% patch_pywebview.py
%PY% make_icon.py

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name SimpleMail ^
    --icon assets\icon.ico ^
    --add-data "web;web" ^
    --add-data "assets;assets" ^
    --add-data "runtimeconfig.json;." ^
    --hidden-import webview.platforms.winforms ^
    mailapp.py

copy /y dist\SimpleMail.exe dist\SimpleMail-arm64.exe >nul
del /q dist\SimpleMail.exe

echo Uploading dist\SimpleMail-arm64.exe to release %TAG%...
gh release upload %TAG% dist/SimpleMail-arm64.exe --clobber
if %errorlevel% neq 0 (
    echo Upload failed. Is the release %TAG% created? (Push the tag - the
    echo x64 workflow creates the release automatically.)
    exit /b 1
)
echo Done! ARM64 build uploaded to https://github.com/super-state/SimpleMail/releases
pause
