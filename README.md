# SimpleMail — a beautiful, minimal Fasthosts mail app for Windows

A small native Windows email app so you can read and send mail from your
Fasthosts mailbox **without logging into webmail.fasthosts.co.uk**.

Works on **both Windows x64 and Windows ARM64** with a modern, clean UI
(WebView2 + Pico CSS — the same rendering engine as Edge), including proper
HTML email rendering, a signature, and Sent/Drafts/Junk/Trash folders.

## Features

- ✅ **Inbox, Sent, Drafts, Junk, Trash** — one click in the sidebar
- ✅ **Real HTML email rendering** (sandboxed iframe, scripts stripped)
- ✅ **Compose with signature** — set it once in Settings, auto-appended
- ✅ **Save drafts**, delete messages, reply (Re: prefilled)
- ✅ Unread counts, message snippets, modern three-pane layout
- ✅ Credentials + signature stored locally in `%APPDATA%\SimpleMail`
- ✅ Connection test button in Settings
- ✅ Native ARM64 and x64 builds from the same codebase

## How it connects

Fasthosts mailboxes are provisioned on the **livemail** platform. The app
uses these settings (editable in Settings):

| Protocol | Server                 | Port | Security        |
|----------|------------------------|------|-----------------|
| IMAP     | `mail.livemail.co.uk`  | 993  | SSL/TLS         |
| SMTP     | `smtp.fasthosts.co.uk` | 587  | STARTTLS        |

Your login is your **full email address** + your normal mailbox password.

## Run from source

1. Install Python from https://www.python.org/downloads/ — the **ARM64**
   installer on a Snapdragon/Eloise Windows laptop, the **64-bit** installer
   otherwise. Tick *"Add python.exe to PATH"*.
2. One-time setup:
   ```
   py -3 -m pip install pywebview==5.3.2 pythonnet==3.0.5 bottle pillow
   ```
   > If you're on ARM64, also pin `cffi==1.17.1` (newer cffi has no ARM64
   > wheel and silently falls back to pure-Python, breaking pythonnet).
3. Double-click `run.bat` (or `py -3 mailapp.py`).
   `run.bat` auto-applies a small patch to pywebview that makes it work with
   .NET Core (needed for ARM64; idempotent, safe to re-run).

## Build a standalone .exe (single icon, taskbar-pinnable)

Run `build.bat` (or the commands below). The result is a single
`dist\SimpleMail.exe` with the app icon embedded — pin it to the taskbar,
drop it in your Start menu, or copy it to any machine **without Python**.

```
py -3 -m pip install pyinstaller
py -3 patch_pywebview.py
py -3 make_icon.py
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name SimpleMail ^
    --icon assets\icon.ico ^
    --add-data "web;web" --add-data "assets;assets" --add-data "runtimeconfig.json;." ^
    --hidden-import webview.platforms.winforms ^
    mailapp.py
```

**Build once on an x64 machine, once on an ARM64 machine** — PyInstaller
produces a native binary for the machine it runs on (verified: `SimpleMail.exe`
is `PE32+ ... ARM64` on this machine). The taskbar icon + pinning work via the
embedded icon and the app's `AppUserModelID` (`SimpleMail.App`).

## Requirements on the target machine (for the .exe)

- **WebView2 runtime** — preinstalled on Windows 11 and most Windows 10
  machines (it's what Edge uses). If missing, grab the Evergreen runtime:
  https://developer.microsoft.com/microsoft-edge/webview2/
- **.NET 8 WindowsDesktop runtime** — needed by pywebview's WinForms layer.
  Install "Windows Desktop Runtime 8.x" from https://dotnet.microsoft.com/download
  (ARM64 variant on ARM64 machines). Auto-installable on first run if you
  ever add a bootstrapper.

## CLI check (no GUI)

```
py -3 mailapp.py --check
```

Prints IMAP + SMTP connection results — handy for debugging.

## Project layout

```
mailapp.py            Python backend (IMAP/SMTP + pywebview bridge)
web/index.html        Frontend (Pico CSS)
web/app.js            Frontend logic
web/pico.min.css      Design framework (local, no CDN)
patch_pywebview.py    One-time pywebview/.NET Core compat patch (idempotent)
make_icon.py          Generates assets/icon.ico
runtimeconfig.json    .NET Core WindowsDesktop runtime config for pythonnet
run.bat               Launcher (applies patch, starts app)
build.bat             One-click .exe builder
```

## Troubleshooting

- **"pythonnet cannot be loaded"** → check `cffi==1.17.1` is installed on
  ARM64 (`py -3 -m pip install cffi==1.17.1`), and that the .NET 8
  WindowsDesktop runtime is present (`dotnet --list-runtimes`).
- **System.Windows.Forms not found** → the runtimeconfig.json next to the
  app forces the WindowsDesktop runtime; make sure it ships with the .exe
  (it's bundled by build.bat) or the .NET Desktop runtime is installed.
- **IMAP login failed but SMTP works** → wrong IMAP host. Fasthosts uses
  `mail.livemail.co.uk`, *not* `imap.1and1.co.uk`.
- **Credentials** are saved plaintext in `%APPDATA%\SimpleMail\config.json`.
  Don't share that file.
