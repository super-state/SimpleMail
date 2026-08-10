#!/usr/bin/env python3
"""
SimpleMail - a minimal, beautiful IMAP/SMTP mail client for Fasthosts.

Architecture: Python backend (IMAP/SMTP, stdlib) + WebView2 frontend
(pywebview + Pico CSS). Runs on Windows x64 AND Windows ARM64.

Servers (Fasthosts mailboxes are provisioned on the livemail platform):
    IMAP: mail.livemail.co.uk:993   (SSL/TLS)
    SMTP: smtp.fasthosts.co.uk:587 (STARTTLS)

Usage:
    python mailapp.py            # launch the GUI
    python mailapp.py --check    # test connectivity from CLI (no GUI)
"""

import json
import os
import re
import smtplib
import subprocess
import sys
import threading
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from pathlib import Path
import html as html_lib
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# pythonnet / pywebview environment (must be set BEFORE importing webview)
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent

if os.name == "nt":
    # Force pythonnet to use the .NET Core WindowsDesktop runtime (contains
    # WinForms, which pywebview needs). Without this, pythonnet loads the
    # console runtime and System.Windows.Forms is missing.
    os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
    _rc = _BASE_DIR / "runtimeconfig.json"
    if _rc.exists():
        os.environ.setdefault("PYTHONNET_CORECLR_RUNTIME_CONFIG", str(_rc))

try:
    import webview
except Exception as e:  # pragma: no cover
    webview = None
    _WEBVIEW_IMPORT_ERROR = e
else:
    _WEBVIEW_IMPORT_ERROR = None

APP_NAME = "SimpleMail"
APP_VERSION = "1.0.6"
APP_REPO = "super-state/SimpleMail"  # owner/repo for auto-updates
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "email": "",
    "password": "",
    "signature": "",
    "imap_host": "mail.livemail.co.uk",
    "imap_port": 993,
    "smtp_host": "smtp.fasthosts.co.uk",
    "smtp_port": 587,
    "smtp_starttls": True,
    "folder": "INBOX",
    "max_messages": 100,
    "ui_scale": "default",  # compact | default | large
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value


# ---------------------------------------------------------------------------
# Mail plumbing (pure logic - also used by --check)
# ---------------------------------------------------------------------------

def connect_imap(cfg):
    """Open an authenticated IMAP connection. Returns (imap, folder_list)."""
    import imaplib
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=30)
    imap.login(cfg["email"], cfg["password"])
    folders = []
    sent_folder = None
    try:
        typ, data = imap.list()
        if typ == "OK":
            for line in data:
                text = line.decode("utf-8", "replace")
                m = re.search(r'"([^"]+)"\s*$', text)
                if m:
                    name = m.group(1)
                    folders.append(name)
                    if r"\Sent" in text and sent_folder is None:
                        sent_folder = name
    except Exception:
        pass
    return imap, folders, sent_folder


def pick_sent_folder(folders, sent_flag_folder):
    """Best folder for saving sent copies: the one flagged \\Sent, else by name."""
    if sent_flag_folder:
        return sent_flag_folder
    for f in folders:
        if f.lower() in ("sent messages", "sent", "sent items"):
            return f
    return "Sent"


FOLDER_ORDER = ["inbox", "sent", "drafts", "junk", "trash"]
FOLDER_LABELS = {
    "inbox": "Inbox", "sent": "Sent", "drafts": "Drafts",
    "junk": "Junk", "trash": "Trash",
}


def map_folders(server_folders):
    """Map server folder names to logical keys. Returns [{key,name,server}]."""
    lower = {f.lower(): f for f in server_folders}
    picks = {}
    if "inbox" in lower:
        picks["inbox"] = lower["inbox"]
    for key, candidates in {
        "sent": ["sent messages", "sent", "sent items"],
        "drafts": ["drafts"],
        "junk": ["junk", "junk email", "spam"],
        "trash": ["trash", "deleted items", "deleted messages"],
    }.items():
        for c in candidates:
            if c in lower:
                picks[key] = lower[c]
                break
    out = []
    for key in FOLDER_ORDER:
        if key in picks:
            out.append({"key": key, "name": FOLDER_LABELS[key], "server": picks[key]})
    # include any leftover server folders so nothing is unreachable
    known = set(picks.values())
    used_keys = set(picks.keys())
    for f in server_folders:
        key = f.lower().replace(" ", "-")
        if f not in known and key not in used_keys:
            out.append({"key": key, "name": f, "server": f})
            used_keys.add(key)
    return out


def fetch_envelopes(imap, folder, limit):
    """Return list of dicts: uid, sender, subject, date, seen, snippet."""
    import imaplib
    imap.select(folder, readonly=True)
    typ, data = imap.uid("search", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()[-limit:]
    uidlist = b",".join(uids)

    # Pass 1: flags - this server drops FLAGS when combined with BODY.PEEK,
    # so it must be a separate (batched) fetch.
    flags_map = {}
    try:
        typ, fdata = imap.uid("fetch", uidlist, "(UID FLAGS)")
        for item in fdata:
            line = item if isinstance(item, bytes) else item[0]
            m = re.search(rb"UID (\d+) FLAGS \(([^)]*)\)", line)
            if m:
                flags_map[m.group(1).decode()] = m.group(2).decode("utf-8", "replace")
    except Exception:
        pass

    # Pass 2: headers + 200-byte body peek, batched. Response items alternate
    # (desc_line, payload) tuples per message part.
    envelopes = []
    try:
        typ, fdata = imap.uid(
            "fetch", uidlist,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT]<0.2000>)",
        )
        cur = None  # current envelope being assembled
        for item in fdata:
            if not isinstance(item, tuple):
                continue
            desc = item[0].decode("utf-8", "replace")
            payload = item[1]
            if "BODY[HEADER.FIELDS" in desc:
                m = re.search(r"UID (\d+)", desc)
                if not m:
                    continue
                msg = message_from_bytes(payload, policy=email_policy)
                cur = {
                    "uid": m.group(1),
                    "sender": str(msg.get("From", "")),
                    "subject": str(msg.get("Subject", "(no subject)")),
                    "date": str(msg.get("Date", "")),
                    "seen": r"\Seen" in flags_map.get(m.group(1), ""),
                    "snippet": "",
                }
                envelopes.append(cur)
            elif "BODY[TEXT]" in desc and cur is not None:
                snippet = decode_snippet(payload)
                cur["snippet"] = snippet[:180]
    except Exception:
        pass
    envelopes.reverse()
    return envelopes


def decode_snippet(raw):
    """Turn a raw BODY[TEXT] peek into a clean one-line preview.

    The peek is the *start of the MIME structure* for multipart messages:
    possibly a leading blank line, a boundary line, Content-* headers, then
    the body (quoted-printable or base64). We extract the real boundary
    name, re-wrap it as a multipart message, and pick the longest readable
    text part - so previews are real sentences, not MIME scaffolding.
    """
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return ""
    text = text.lstrip("\r\n")  # some peeks start with a blank line
    m = re.match(r"^(--[^\r\n]+)", text)
    if not m:
        # some messages start with a short preamble ("This is a multi-part
        # message in MIME format.") before the first boundary - find it
        m2 = re.search(r"\r?\n(--[^\r\n]+)", text)
        if m2:
            m = m2
    if m:
        boundary = m.group(1)
        try:
            from email import message_from_string
            from email.policy import default as _pol
            wrapped = (
                f"Content-Type: multipart/mixed; boundary=\"{boundary[2:]}\"\r\n\r\n"
                + text
            )
            msg = message_from_string(wrapped, policy=_pol)
            best = ""
            for part in msg.walk():
                ct = part.get_content_type()
                try:
                    body = part.get_content()
                except Exception:
                    continue
                if not body or not body.strip():
                    continue
                if ct == "text/plain":
                    cand = collapse_snippet(body)
                elif ct == "text/html":
                    cand = collapse_snippet(html_to_text(body))
                else:
                    continue
                if len(cand) > len(best):
                    best = cand
            if best:
                return best
        except Exception:
            pass
    # not multipart scaffolding - plain text or html directly
    if "<" in text:
        return collapse_snippet(html_to_text(text))
    return collapse_snippet(text)


def collapse_snippet(s):
    """Flatten whitespace/newlines into a single readable line."""
    s = re.sub(r"\s+", " ", s or "")
    return s.strip()


def fetch_message(imap, folder, uid):
    """Return dict with parsed message; marks it \\Seen."""
    import imaplib
    imap.select(folder, readonly=False)
    typ, data = imap.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        raise RuntimeError("Could not fetch message")
    raw = data[0][1]
    msg = message_from_bytes(raw, policy=email_policy)
    try:
        imap.uid("store", uid, "+FLAGS", r"(\Seen)")
    except Exception:
        pass
    text, html = extract_bodies(msg)
    attachments = []
    for idx, part in enumerate(msg.walk()):
        fn = part.get_filename()
        if fn:
            try:
                size = len(part.get_payload(decode=True) or b"")
            except Exception:
                size = 0
            attachments.append({
                "index": idx,
                "name": fn,
                "size": size,
                "content_type": part.get_content_type(),
            })
    return {
        "subject": str(msg.get("Subject", "(no subject)")),
        "sender": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "date": str(msg.get("Date", "")),
        "text": text,
        "html": html,
        "attachments": attachments,
    }


def extract_bodies(msg):
    """Return (plain_text, html) - prefer plain, fall back to html -> text."""
    text, html = None, None
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_disposition() in ("attachment", "inline") and ct != "text/html":
            continue
        if ct == "text/plain" and text is None:
            try:
                text = part.get_content()
            except Exception:
                pass
        elif ct == "text/html" and html is None:
            try:
                html = part.get_content()
            except Exception:
                pass
    if html and not text:
        text = html_to_text(html)
    return text or "(no readable text body)", html


class _TE(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "blockquote"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html):
    p = _TE()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or "(empty html message)"


def send_message(cfg, to, subject, body, body_html=None, save_sent=True):
    """Send via SMTP (HTML body supported), then append a copy to Sent."""
    msg = EmailMessage()
    msg["From"] = cfg["email"]
    msg["To"] = to
    msg["Subject"] = subject
    if body_html:
        msg.set_content(body or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body or "")
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=30) as smtp:
        smtp.ehlo()
        if cfg["smtp_starttls"]:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(cfg["email"], cfg["password"])
        smtp.send_message(msg)
    if save_sent:
        try:
            imap, folders, sent_flag = connect_imap(cfg)
            try:
                sent = pick_sent_folder(folders, sent_flag)
                imap.append(sent, r"(\Seen)", None, msg.as_bytes())
            finally:
                imap.logout()
        except Exception:
            pass  # sent copy is best-effort; the mail itself went out


def save_draft_message(cfg, to, subject, body, body_html=None):
    import imaplib
    msg = EmailMessage()
    msg["From"] = cfg["email"]
    msg["To"] = to
    msg["Subject"] = subject
    if body_html:
        msg.set_content(body or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body or "")
    imap, folders, _ = connect_imap(cfg)
    try:
        drafts = next((f for f in folders if f.lower() in ("drafts",)), "Drafts")
        imap.append(drafts, r"(\Draft)", None, msg.as_bytes())
    finally:
        imap.logout()


def mark_all_read(cfg, folder):
    """Mark every message in a folder as \\Seen. Returns count marked."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        imap.select(folder, readonly=False)
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return 0
        uids = data[0].split()
        imap.store(b",".join(uids), "+FLAGS", r"(\Seen)")
        return len(uids)
    finally:
        imap.logout()


def set_seen(cfg, folder, uid, seen):
    """Mark a single message read (seen=True) or unread (seen=False)."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        imap.select(folder, readonly=False)
        imap.uid("store", uid, "+FLAGS" if seen else "-FLAGS", r"(\Seen)")
    finally:
        imap.logout()


def save_attachment(cfg, folder, uid, part_index):
    """Save one attachment part to ~/Downloads/SimpleMail/. Returns path."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        imap.select(folder, readonly=True)
        typ, data = imap.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError("Could not fetch message")
        msg = message_from_bytes(data[0][1], policy=email_policy)
        parts = list(msg.walk())
        part = parts[part_index]
        payload = part.get_payload(decode=True)
        if payload is None:
            raise RuntimeError("Attachment is not decodable")
        name = part.get_filename() or f"attachment-{part_index}"
        out_dir = Path.home() / "Downloads" / "SimpleMail"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / os.path.basename(name)
        out.write_bytes(payload)
        return str(out)
    finally:
        imap.logout()


def delete_message(cfg, folder, uid):
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        imap.select(folder, readonly=False)
        imap.uid("store", uid, "+FLAGS", r"(\Deleted)")
        imap.expunge()
    finally:
        imap.logout()


def move_message(cfg, folder, uid, target):
    """Move a message to another folder (MOVE, fallback COPY+DELETE)."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        imap.select(folder, readonly=False)
        try:
            typ, _ = imap.uid("MOVE", uid, target)
            if typ == "OK":
                return
        except Exception:
            pass
        # fallback for servers without MOVE
        imap.uid("COPY", uid, target)
        imap.uid("store", uid, "+FLAGS", r"(\Deleted)")
        imap.expunge()
    finally:
        imap.logout()


def create_folder(cfg, name):
    """Create an IMAP folder."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        typ, data = imap.create(name)
        if typ != "OK":
            raise RuntimeError(data[-1].decode("utf-8", "replace") if data else "create failed")
    finally:
        imap.logout()


def short_sender(sender):
    """'Instagram <posts-recaps@mail.instagram.com>' -> 'Instagram' (or address)."""
    m = re.search(r"^([^<]+?)\s*<.*>", sender)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return sender or "Unknown"


def sender_domain(sender):
    """'Instagram <posts-recaps@mail.instagram.com>' -> 'instagram.com'."""
    m = re.search(r"<([^>]+)>", sender)
    addr = m.group(1) if m else sender
    if "@" not in addr:
        return None
    host = addr.rsplit("@", 1)[1].lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def learn_rule(cfg, domain, target_folder):
    """Remember: mail from this domain goes to target_folder."""
    rules = cfg.data.setdefault("rules", {})
    rules[domain] = target_folder
    cfg.save()


def remove_rule(cfg, domain):
    rules = cfg.data.get("rules", {})
    if domain in rules:
        del rules[domain]
        cfg.save()
        return True
    return False


def apply_rules(cfg, server_folder, envelopes):
    """Move any envelope whose sender domain has a learned rule targeting a
    different folder. Returns (kept_envelopes, moved_count)."""
    rules = cfg.data.get("rules", {})
    if not rules:
        return envelopes, 0
    moves = {}  # uid -> target folder
    kept = []
    for env in envelopes:
        dom = sender_domain(env["sender"])
        target = rules.get(dom) if dom else None
        if target and target != server_folder:
            moves[env["uid"]] = target
        else:
            kept.append(env)
    for uid, target in moves.items():
        try:
            move_message(cfg, server_folder, uid, target)
        except Exception:
            pass
    return kept, len(moves)


def check_connection(cfg):
    """Returns list of (ok: bool, line: str)."""
    results = []
    try:
        imap, folders, _ = connect_imap(cfg)
        results.append((True, f"IMAP login OK ({cfg['imap_host']}:{cfg['imap_port']})"))
        results.append((True, f"Folders: {', '.join(folders[:8]) or '(none listed)'}"))
        imap.logout()
    except Exception as e:
        results.append((False, f"IMAP failed: {e}"))
    try:
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=30) as s:
            s.ehlo()
            if cfg["smtp_starttls"]:
                s.starttls()
                s.ehlo()
            s.login(cfg["email"], cfg["password"])
            results.append((True, f"SMTP login OK ({cfg['smtp_host']}:{cfg['smtp_port']})"))
    except Exception as e:
        results.append((False, f"SMTP failed: {e}"))
    return results


# ---------------------------------------------------------------------------
# Auto-update (GitHub releases)
# ---------------------------------------------------------------------------

def current_arch():
    """Return 'x64' or 'arm64' for release-asset selection."""
    import platform
    m = platform.machine().lower()
    if "arm" in m or "aarch" in m:
        return "arm64"
    return "x64"


def parse_version(s):
    """'v1.2.3' -> (1, 2, 3). Returns None for junk."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(s or ""))
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def check_for_update(timeout=15):
    """Query GitHub for the latest release. Returns dict or None.

    Fields: version (tuple), tag, url, asset_url, asset_name, notes.
    Raises on network/parse errors so the caller can fail quietly.
    """
    import json as _json
    import urllib.request

    url = f"https://api.github.com/repos/{APP_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8"))

    tag = data.get("tag_name", "")
    version = parse_version(tag)
    if not version:
        return None

    arch = current_arch()
    asset = None
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        if arch == "arm64" and "arm64" in name:
            asset = a
            break
        if arch == "x64" and ("x64" in name or "amd64" in name):
            asset = a
            break

    return {
        "version": version,
        "tag": tag,
        "url": data.get("html_url", ""),
        "asset_url": asset["browser_download_url"] if asset else None,
        "asset_name": asset["name"] if asset else None,
        "notes": data.get("body", "")[:2000],
        "arch": arch,
    }


def download_file(url, dest, timeout=120):
    """Download url to dest with a progress callback (bytes_so_far, total)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)


def _frozen_exe_path():
    """Path of the running .exe when frozen with PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def apply_update(asset_url):
    """Download the new exe next to the running one and restart via updater.

    The running exe cannot overwrite itself, so we write an update.bat that
    waits for this process to exit, replaces the exe, and relaunches it.
    """
    import subprocess

    exe_path = _frozen_exe_path()
    if exe_path is None:
        raise RuntimeError("Updates only work when running the packaged app")

    target = exe_path
    new_exe = target.with_name(target.stem + ".new" + target.suffix)
    updater = target.with_name("_update.bat")

    download_file(asset_url, new_exe)

    # batch: wait for the OLD process to fully exit, replace, restart once.
    # The old instance holds the local port until it dies, so launching
    # immediately races it - poll tasklist until it's gone instead.
    bat = (
        "@echo off\r\n"
        ":wait_old\r\n"
        "tasklist /FI \"IMAGENAME eq SimpleMail.exe\" | findstr /i SimpleMail >nul\r\n"
        "if %errorlevel%==0 (\r\n"
        "  timeout /t 2 /nobreak >nul\r\n"
        "  goto wait_old\r\n"
        ")\r\n"
        f'copy /y "{new_exe}" "{target}" >nul\r\n'
        f'del /q "{new_exe}"\r\n'
        f'start "" "{target}"\r\n'
        f'del /q "%~f0"\r\n'
    )
    updater.write_text(bat, encoding="ascii")

    # detach the updater so it survives this process's exit
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["cmd", "/c", str(updater)],
        close_fds=True, creationflags=creationflags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # give the updater a moment to start, then exit so the file unlocks
    import time
    time.sleep(2)
    os._exit(0)


# ---------------------------------------------------------------------------
# pywebview JS bridge
# ---------------------------------------------------------------------------

_API_WINDOW = None  # set in main(); kept module-level so pywebview's
# attribute walker doesn't expose it to JS (it would recurse into Window)


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self._log_path = _BASE_DIR / "api_debug.log"

    def _log(self, msg):
        if not os.environ.get("SIMPLEMAIL_DEBUG"):
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{msg}\n")
        except Exception:
            pass

    def get_config(self):
        self._log("CALL get_config")
        cfg = self.cfg
        folders = []
        if cfg["email"] and cfg["password"]:
            try:
                imap, server_folders, _ = connect_imap(cfg)
                try:
                    folders = map_folders(server_folders)
                    # unread counts per folder for the sidebar badges
                    for f in folders:
                        try:
                            imap.select(f["server"], readonly=True)
                            typ, data = imap.uid("search", None, "UNSEEN")
                            f["unread"] = len(data[0].split()) if typ == "OK" and data and data[0] else 0
                        except Exception:
                            f["unread"] = 0
                finally:
                    imap.logout()
            except Exception as e:
                folders = [{"key": "inbox", "name": "Inbox", "server": "INBOX", "unread": 0}]
                # surface the login problem to the UI
                self._last_error = str(e)
        return {
            "email": cfg["email"],
            "password": cfg["password"],
            "signature": cfg["signature"],
            "ui_scale": cfg["ui_scale"],
            "folders": folders,
            "rules": cfg.data.get("rules", {}),
            "version": APP_VERSION,
            "arch": current_arch(),
            "error": getattr(self, "_last_error", None),
        }

    def save_config(self, data):
        self._log("CALL save_config")
        self.cfg["email"] = data.get("email", "")
        self.cfg["password"] = data.get("password", "")
        self.cfg["signature"] = data.get("signature", "")
        if data.get("ui_scale") in ("compact", "default", "large"):
            self.cfg["ui_scale"] = data["ui_scale"]
        self.cfg.save()
        return {"ok": True}

    def test_connection(self, data):
        self._log("CALL test_connection")
        test_cfg = dict(self.cfg.data)
        test_cfg["email"] = data.get("email", "")
        test_cfg["password"] = data.get("password", "")
        results = check_connection(test_cfg)
        ok = all(r[0] for r in results)
        return {"ok": ok, "error": None if ok else "; ".join(l for okk, l in results if not okk)}

    def list_messages(self, server_folder):
        self._log(f"CALL list_messages {server_folder}")
        imap, _, _ = connect_imap(self.cfg)
        try:
            envelopes = fetch_envelopes(imap, server_folder, int(self.cfg["max_messages"]))
        finally:
            imap.logout()
        envelopes, moved = apply_rules(self.cfg, server_folder, envelopes)
        if moved:
            # re-open a fresh connection; apply_rules used its own
            imap2, _, _ = connect_imap(self.cfg)
            try:
                envelopes = fetch_envelopes(imap2, server_folder, int(self.cfg["max_messages"]))
                envelopes, _ = apply_rules(self.cfg, server_folder, envelopes)
            finally:
                imap2.logout()
        unread = sum(1 for e in envelopes if not e["seen"])
        return {"envelopes": envelopes, "unread": unread, "auto_moved": moved}

    def get_message(self, server_folder, uid):
        self._log(f"CALL get_message {server_folder} {uid}")
        imap, _, _ = connect_imap(self.cfg)
        try:
            return fetch_message(imap, server_folder, uid)
        finally:
            imap.logout()

    def send_mail(self, to, subject, body, body_html=None):
        self._log(f"CALL send_mail to={to} subject={subject[:40]}")
        send_message(self.cfg, to, subject, body, body_html=body_html)
        return {"ok": True}

    def save_draft(self, to, subject, body, body_html=None):
        self._log(f"CALL save_draft subject={subject[:40]}")
        save_draft_message(self.cfg, to, subject, body, body_html=body_html)
        return {"ok": True}

    def mark_all_read(self, server_folder):
        self._log(f"CALL mark_all_read {server_folder}")
        count = mark_all_read(self.cfg, server_folder)
        return {"count": count}

    def set_seen(self, server_folder, uid, seen):
        self._log(f"CALL set_seen {server_folder} {uid} seen={seen}")
        set_seen(self.cfg, server_folder, uid, bool(seen))
        return {"ok": True}

    def save_attachment(self, server_folder, uid, part_index):
        self._log(f"CALL save_attachment {server_folder} {uid} part={part_index}")
        path = save_attachment(self.cfg, server_folder, uid, int(part_index))
        return {"path": path}

    def pick_image(self):
        """Open a file dialog for an image; return {name, data_uri} or None."""
        self._log("CALL pick_image")
        import base64
        import mimetypes
        global _API_WINDOW
        win = _API_WINDOW
        if win is None:
            raise RuntimeError("No window available")
        result = win.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp)",),
        )
        if not result:
            return None
        path = Path(result[0])
        if not path.exists():
            raise RuntimeError("Selected file not found")
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        data_uri = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return {"name": path.name, "data_uri": data_uri}

    def delete_message(self, server_folder, uid):
        self._log(f"CALL delete_message {server_folder} {uid}")
        delete_message(self.cfg, server_folder, uid)
        return {"ok": True}

    def move_message(self, server_folder, uid, target, sender="", learn=True):
        self._log(f"CALL move_message {server_folder} {uid} -> {target} learn={learn}")
        move_message(self.cfg, server_folder, uid, target)
        learned = None
        if learn:
            dom = sender_domain(sender) if sender else None
            if dom:
                learn_rule(self.cfg, dom, target)
                learned = dom
        return {"ok": True, "learned": learned}

    def create_folder(self, name):
        self._log(f"CALL create_folder {name}")
        create_folder(self.cfg, name)
        return {"ok": True}

    def remove_rule(self, domain):
        self._log(f"CALL remove_rule {domain}")
        removed = remove_rule(self.cfg, domain)
        return {"ok": removed}

    def learn_rule(self, domain, target):
        self._log(f"CALL learn_rule {domain} -> {target}")
        learn_rule(self.cfg, domain, target)
        return {"ok": True}

    def check_update(self):
        """Return {available, version, notes, asset_name, url} or error."""
        self._log("CALL check_update")
        try:
            info = check_for_update()
        except Exception as e:
            return {"available": False, "error": str(e)}
        if not info or not info["asset_url"]:
            return {"available": False, "error": "No matching release asset"}
        local = parse_version(APP_VERSION)
        remote = info["version"]
        available = bool(local and remote and remote > local)
        return {
            "available": available,
            "local_version": APP_VERSION,
            "new_version": info["tag"],
            "notes": info.get("notes", ""),
            "asset_name": info.get("asset_name"),
            "url": info.get("url"),
            "arch": info.get("arch"),
        }

    def apply_update(self):
        """Download the new exe and self-replace (process exits)."""
        self._log("CALL apply_update")
        info = check_for_update()
        if not info or not info["asset_url"]:
            raise RuntimeError("No update available")
        local = parse_version(APP_VERSION)
        if not (local and info["version"] > local):
            raise RuntimeError("Already up to date")
        apply_update(info["asset_url"])
        return {"ok": True}


# ---------------------------------------------------------------------------
# Native Windows notifications (toasts, like Outlook)
# ---------------------------------------------------------------------------

def _xml_escape(s):
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def show_toast(title, body):
    """Fire a native Windows toast notification via the WinRT API.

    Uses PowerShell (always present on Windows, ARM64-native) so we need no
    extra Python deps. The toast appears in the Action Center exactly like
    Outlook's - clickable, respects Do Not Disturb, etc.
    """
    if os.name != "nt":
        return
    title = _xml_escape(title)[:120]
    body = _xml_escape(body)[:300]
    xml = (
        '<toast activationType="foreground">'
        '<visual><binding template="ToastGeneric">'
        f"<text>{title}</text><text>{body}</text>"
        "</binding></visual></toast>"
    )
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$xml.LoadXml('{xml}'); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('SimpleMail.App').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($xml))"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


class MailPoller(threading.Thread):
    """Poll the inbox for new mail and raise native toasts.

    Runs every POLL_SECONDS; tracks the highest UID seen so only *new*
    arrivals notify. Skips senders that learned rules would auto-route
    elsewhere (they're not "new business mail").
    """

    POLL_SECONDS = 30
    LAST_UID_KEY = "poll_last_uid"

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self._last_uid = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # initial pass: just record where we are - don't notify for the
        # existing backlog
        self._snapshot_uid()
        while not self._stop.wait(self.POLL_SECONDS):
            try:
                self._check_once()
            except Exception:
                pass  # transient IMAP/network errors are fine

    def _snapshot_uid(self):
        try:
            imap, _, _ = connect_imap(self.cfg)
            try:
                imap.select("INBOX", readonly=True)
                typ, data = imap.uid("search", None, "ALL")
                if typ == "OK" and data and data[0]:
                    self._last_uid = int(data[0].split()[-1])
            finally:
                imap.logout()
        except Exception:
            pass

    def _check_once(self):
        imap, _, _ = connect_imap(self.cfg)
        try:
            imap.select("INBOX", readonly=True)
            if self._last_uid is None:
                return  # not armed yet
            typ, data = imap.uid("search", None, f"UID {self._last_uid + 1}:*")
            if typ != "OK" or not data or not data[0]:
                return
            new_uids = [int(x) for x in data[0].split()]
            if not new_uids:
                return
            uid_list = ",".join(str(u) for u in new_uids)
            typ, fdata = imap.uid(
                "fetch", uid_list, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)] FLAGS)"
            )
            seen_uids = set()
            for item in fdata:
                if not isinstance(item, tuple):
                    continue
                desc = item[0].decode("utf-8", "replace")
                payload = item[1]
                m = re.search(r"UID (\d+)", desc)
                if not m:
                    continue
                uid = int(m.group(1))
                seen_uids.add(uid)
                if uid <= self._last_uid:
                    continue
                msg = message_from_bytes(payload, policy=email_policy)
                sender = str(msg.get("From", "Unknown"))
                subject = str(msg.get("Subject", "(no subject)"))
                domain = sender_domain(sender)
                rule_target = self.cfg["rules"].get(domain)
                if rule_target and rule_target != "INBOX":
                    continue  # learned rule routes this elsewhere
                show_toast(subject, short_sender(sender))
            if seen_uids:
                self._last_uid = max(seen_uids)
        finally:
            imap.logout()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def main():
    cfg = Config()

    if "--check" in sys.argv:
        print(f"Config: {CONFIG_FILE}")
        print(f"Account: {cfg['email'] or '(not set - run the app and enter credentials)'}")
        results = check_connection(cfg)
        for ok, line in results:
            print(("  OK   " if ok else "  FAIL ") + line)
        return 0 if all(ok for ok, _ in results) else 1

    # Taskbar identity: without an AppUserModelID the icon won't pin/group
    # properly from a PyInstaller onefile build.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SimpleMail.App")
        except Exception:
            pass

    if webview is None:
        print("pywebview is not available:", _WEBVIEW_IMPORT_ERROR)
        print("Install it with:  py -3 -m pip install pywebview==5.3.2 pythonnet==3.0.5")
        return 1

    # Serve the frontend over localhost (avoids file:// quirks in WebView2)
    try:
        import bottle
    except ImportError:
        bottle = None

    index_html = (_BASE_DIR / "web" / "index.html").resolve()
    web_dir = (_BASE_DIR / "web").resolve()

    if bottle is not None:
        @bottle.get("/")
        def _index():
            return bottle.static_file("index.html", root=str(web_dir))

        @bottle.get("/<filename:path>")
        def _static(filename):
            return bottle.static_file(filename, root=str(web_dir))

        server = bottle.Bottle()
        # re-register on the bottle instance (module-level decorators attach to default app)
        server.route("/", "GET", _index)
        server.route("/<filename:path>", "GET", _static)
        port = 17591
        threading.Thread(
            target=lambda: server.run(host="127.0.0.1", port=port, quiet=True),
            daemon=True,
        ).start()
        url = f"http://127.0.0.1:{port}"
    else:
        url = str(index_html)

    api = Api(cfg)
    icon_path = _BASE_DIR / "assets" / "icon.ico"
    window = webview.create_window(
        f"SimpleMail v{APP_VERSION}",
        url=url,
        js_api=api,
        width=1240,
        height=800,
        min_size=(980, 620),
        background_color="#f6f8fb",
    )
    global _API_WINDOW
    _API_WINDOW = window  # lets Api.pick_image open a file dialog
    # Title-bar icon (pywebview 5.x has no icon kwarg; set it on the native form)
    if os.name == "nt" and icon_path.exists():
        try:
            from System.Drawing import Icon as NetIcon  # type: ignore
            window.native.Icon = NetIcon(str(icon_path))
        except Exception:
            pass

    # Native inbox notifications (like Outlook): poll for new mail quietly
    poller = None
    if cfg["email"] and cfg["password"]:
        poller = MailPoller(cfg)
        poller.start()

    try:
        webview.start()
    finally:
        if poller is not None:
            poller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
