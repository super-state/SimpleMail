@echo off
rem SimpleMail launcher - works on Windows x64 and ARM64
cd /d "%~dp0"

rem Try the py launcher first, then python
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 patch_pywebview.py
    py -3 mailapp.py %*
    goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
    python patch_pywebview.py
    python mailapp.py %*
    goto :eof
)

echo Python 3 is required. Install it from https://www.python.org/downloads/
echo Make sure to tick "Add python.exe to PATH" during installation.
pause
