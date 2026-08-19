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
APP_VERSION = "1.1.4"
APP_REPO = "super-state/SimpleMail"  # owner/repo for auto-updates
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "accounts": [],          # list of account dicts (see ACCOUNT_DEFAULTS)
    "active_account": "",    # id of the last-used account
    "max_messages": 100,
    "ui_scale": "default",  # compact | default | large
}

# One entry per mailbox. IDENTITY ISOLATION IS THE POINT of this schema:
# every account carries its own servers, credentials, From address, signature
# and learned rules, and the backend only ever derives the From address from
# the account that owns the mailbox - never from anything the UI sends.
ACCOUNT_DEFAULTS = {
    "id": "",                # stable slug, never shown
    "label": "",             # human name shown in the sidebar / From display name
    "color": "#2563eb",      # account accent so mailboxes are visually unmistakable
    "email": "",             # IMAP login, and the default identity
    "password": "",          # IMAP login password (and SMTP unless overridden)
    "from_email": "",        # identity override (e.g. hello@playloudr.com while
                             # the mailbox itself lives on another domain)
    "imap_host": "mail.livemail.co.uk",
    "imap_port": 993,
    "smtp_host": "smtp.fasthosts.co.uk",
    "smtp_port": 587,
    "smtp_starttls": True,
    "smtp_user": "",         # SMTP login override (e.g. Resend's "resend")
    "smtp_password": "",     # SMTP password override (e.g. a Resend API key)
    "signature": "",
    "rules": {},             # learned sender-domain -> folder moves, per account
}

# Legacy (v1.0.x) single-account keys, migrated into accounts[0] on first load.
_LEGACY_KEYS = ("email", "password", "signature", "imap_host", "imap_port",
                "smtp_host", "smtp_port", "smtp_starttls", "rules")


def _slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s or "account"


def normalize_account(raw):
    """Fill an account dict with defaults; never mutates the input."""
    acct = dict(ACCOUNT_DEFAULTS)
    acct["rules"] = {}
    for key in ACCOUNT_DEFAULTS:
        if key in (raw or {}):
            acct[key] = raw[key]
    if not acct["id"]:
        acct["id"] = _slugify(acct["email"].split("@")[-1].split(".")[0] or acct["label"])
    if not acct["label"]:
        acct["label"] = acct["email"] or acct["id"]
    return acct


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
        self._migrate_legacy()
        self.data["accounts"] = [normalize_account(a) for a in self.data.get("accounts", [])]

    def _migrate_legacy(self):
        """v1.0.x kept a single account's fields at the top level."""
        if self.data.get("accounts"):
            return
        legacy_email = self.data.get("email", "")
        if not legacy_email:
            return
        acct = normalize_account({k: self.data[k] for k in _LEGACY_KEYS if k in self.data})
        domain = legacy_email.split("@")[-1].split(".")[0]
        acct["label"] = domain.replace("-", " ").title() if domain else legacy_email
        self.data["accounts"] = [acct]
        self.data["active_account"] = acct["id"]
        for k in _LEGACY_KEYS:
            self.data.pop(k, None)
        try:
            self.save()
        except OSError:
            pass  # migration re-runs next boot; nothing is lost

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)

    # -- accounts ----------------------------------------------------------
    def accounts(self):
        return self.data.get("accounts", [])

    def account(self, account_id):
        """The account dict for an id. Raises on an unknown id rather than
        falling back to another account - a silent fallback is exactly how a
        reply would leave from the wrong address."""
        for acct in self.accounts():
            if acct["id"] == account_id:
                return acct
        raise KeyError(f"Unknown account: {account_id!r}")

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value


# ---------------------------------------------------------------------------
# Identity helpers - the single choke points for who a message is "from" and
# how SMTP authenticates. Everything that sends goes through these.
# ---------------------------------------------------------------------------

def from_address(acct):
    return (acct.get("from_email") or acct["email"]).strip()


def smtp_credentials(acct):
    user = (acct.get("smtp_user") or acct["email"]).strip()
    password = acct.get("smtp_password") or acct["password"]
    return user, password


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


def send_message(acct, to, subject, body, body_html=None, save_sent=True):
    """Send via the ACCOUNT'S OWN SMTP as the ACCOUNT'S OWN identity, then
    append a copy to that same account's Sent folder.

    The From address is derived here from the account and nowhere else -
    callers cannot supply one, so a message can never leave a mailbox under
    another account's identity."""
    from email.utils import formataddr

    msg = EmailMessage()
    sender = from_address(acct)
    msg["From"] = formataddr((acct.get("label") or "", sender)) if acct.get("label") else sender
    msg["To"] = to
    msg["Subject"] = subject
    if body_html:
        msg.set_content(body or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body or "")
    smtp_user, smtp_password = smtp_credentials(acct)
    with smtplib.SMTP(acct["smtp_host"], int(acct["smtp_port"]), timeout=30) as smtp:
        smtp.ehlo()
        if acct["smtp_starttls"]:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
    if save_sent:
        try:
            imap, folders, sent_flag = connect_imap(acct)
            try:
                sent = pick_sent_folder(folders, sent_flag)
                imap.append(sent, r"(\Seen)", None, msg.as_bytes())
            finally:
                imap.logout()
        except Exception:
            pass  # sent copy is best-effort; the mail itself went out


def save_draft_message(acct, to, subject, body, body_html=None):
    import imaplib
    msg = EmailMessage()
    msg["From"] = from_address(acct)
    msg["To"] = to
    msg["Subject"] = subject
    if body_html:
        msg.set_content(body or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body or "")
    imap, folders, _ = connect_imap(acct)
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


def learn_rule(cfg, account_id, domain, target_folder):
    """Remember: mail from this domain goes to target_folder - for ONE account.
    Rules are per-mailbox so a routing habit on one account can never move
    another account's mail."""
    acct = cfg.account(account_id)
    acct.setdefault("rules", {})[domain] = target_folder
    cfg.save()


def remove_rule(cfg, account_id, domain):
    acct = cfg.account(account_id)
    rules = acct.get("rules", {})
    if domain in rules:
        del rules[domain]
        cfg.save()
        return True
    return False


def apply_rules(cfg, account_id, server_folder, envelopes):
    """Move any envelope whose sender domain has a learned rule targeting a
    different folder. Returns (kept_envelopes, moved_count)."""
    acct = cfg.account(account_id)
    rules = acct.get("rules", {})
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
            move_message(acct, server_folder, uid, target)
        except Exception:
            pass
    return kept, len(moves)


def check_connection(acct):
    """Returns list of (ok: bool, line: str)."""
    results = []
    try:
        imap, folders, _ = connect_imap(acct)
        results.append((True, f"IMAP login OK ({acct['imap_host']}:{acct['imap_port']})"))
        results.append((True, f"Folders: {', '.join(folders[:8]) or '(none listed)'}"))
        imap.logout()
    except Exception as e:
        results.append((False, f"IMAP failed: {e}"))
    try:
        smtp_user, smtp_password = smtp_credentials(acct)
        with smtplib.SMTP(acct["smtp_host"], int(acct["smtp_port"]), timeout=30) as s:
            s.ehlo()
            if acct["smtp_starttls"]:
                s.starttls()
                s.ehlo()
            s.login(smtp_user, smtp_password)
            results.append((True, f"SMTP login OK ({acct['smtp_host']}:{acct['smtp_port']}) as {smtp_user}"))
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


def _launch_updater(updater_path):
    """Launch the updater script so it survives this process's exit.

    NOTE: DETACHED_PROCESS breaks powershell -File (the script silently never
    runs - verified empirically Aug 2026). CREATE_NEW_PROCESS_GROUP alone is
    enough to survive the app's os._exit(); -WindowStyle Hidden keeps the
    console out of sight.
    """
    import subprocess

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-File", str(updater_path)],
        close_fds=True, creationflags=creationflags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def apply_update(asset_url):
    """Download the new exe next to the running one and restart via updater.

    The running exe cannot overwrite itself, so we write an _update.ps1 that
    waits for this process to exit, replaces the exe, and relaunches it.
    """
    import subprocess

    exe_path = _frozen_exe_path()
    if exe_path is None:
        raise RuntimeError("Updates only work when running the packaged app")

    target = exe_path
    new_exe = target.with_name(target.stem + ".new" + target.suffix)
    updater = target.with_name("_update.ps1")

    download_file(asset_url, new_exe)

    # PowerShell updater: wait for the OLD process to fully exit, replace,
    # restart once. The old instance holds the local port until it dies, so
    # launching immediately races it - poll Get-Process until it's gone.
    # (cmd's `timeout` fails in detached/redirected contexts; PowerShell's
    # Start-Sleep does not, so we use a .ps1 instead of a .bat.)
    # NOTE: build the script with .replace(), NOT str.format() - PowerShell's
    # literal braces (while {...}, Start-Sleep) get parsed by format() as
    # replacement fields and raise KeyError ('\n  Start-Sleep -Seconds 2\n').
    ps = (
        "$target = '__TARGET__'\n"
        "$new = '__NEW_EXE__'\n"
        "while (Get-Process -Name SimpleMail -ErrorAction SilentlyContinue) {\n"
        "  Start-Sleep -Seconds 2\n"
        "}\n"
        "Copy-Item -Force $new $target\n"
        "Remove-Item -Force $new\n"
        "Start-Process -FilePath $target\n"
        "Remove-Item -Force $PSCommandPath -ErrorAction SilentlyContinue\n"
    ).replace("__TARGET__", str(target)).replace("__NEW_EXE__", str(new_exe))
    updater.write_text(ps, encoding="utf-8")

    # launch the updater, then exit so the file unlocks
    _launch_updater(updater)
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

    def _acct(self, account_id):
        """Resolve an account id to its config. Raises on unknown ids -
        NO fallback account, ever. A fallback is how mail crosses accounts."""
        return self.cfg.account(account_id)

    def get_config(self):
        self._log("CALL get_config")
        cfg = self.cfg
        accounts = []
        for acct in cfg.accounts():
            accounts.append({
                "id": acct["id"],
                "label": acct["label"],
                "color": acct.get("color") or "#2563eb",
                "email": acct["email"],
                "password": acct["password"],
                "from_email": acct.get("from_email", ""),
                "identity": from_address(acct),
                "imap_host": acct["imap_host"],
                "imap_port": acct["imap_port"],
                "smtp_host": acct["smtp_host"],
                "smtp_port": acct["smtp_port"],
                "smtp_starttls": acct["smtp_starttls"],
                "smtp_user": acct.get("smtp_user", ""),
                "smtp_password": acct.get("smtp_password", ""),
                "signature": acct.get("signature", ""),
                "rules": acct.get("rules", {}),
            })
        return {
            "accounts": accounts,
            "active_account": cfg.data.get("active_account", ""),
            "ui_scale": cfg["ui_scale"],
            "version": APP_VERSION,
            "arch": current_arch(),
        }

    def get_folders(self, account_id):
        """Folder list + unread badges for ONE account."""
        self._log(f"CALL get_folders {account_id}")
        acct = self._acct(account_id)
        folders = []
        error = None
        if acct["email"] and acct["password"]:
            try:
                imap, server_folders, _ = connect_imap(acct)
                try:
                    folders = map_folders(server_folders)
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
                error = str(e)
        return {"folders": folders, "error": error}

    def set_active_account(self, account_id):
        self._log(f"CALL set_active_account {account_id}")
        self._acct(account_id)  # validate
        self.cfg["active_account"] = account_id
        self.cfg.save()
        return {"ok": True}

    def save_config(self, data):
        self._log("CALL save_config")
        incoming = data.get("accounts", [])
        # Preserve learned rules across saves: the settings UI doesn't edit
        # them, so merge each account's stored rules back in by id.
        existing_rules = {a["id"]: a.get("rules", {}) for a in self.cfg.accounts()}
        accounts = []
        seen_ids = set()
        for raw in incoming:
            acct = normalize_account(raw)
            while acct["id"] in seen_ids:  # keep ids unique
                acct["id"] += "-2"
            seen_ids.add(acct["id"])
            if "rules" not in raw or not raw.get("rules"):
                acct["rules"] = existing_rules.get(acct["id"], {})
            accounts.append(acct)
        self.cfg["accounts"] = accounts
        active = data.get("active_account", "")
        if not any(a["id"] == active for a in accounts):
            active = accounts[0]["id"] if accounts else ""
        self.cfg["active_account"] = active
        if data.get("ui_scale") in ("compact", "default", "large"):
            self.cfg["ui_scale"] = data["ui_scale"]
        self.cfg.save()
        return {"ok": True, "accounts": [a["id"] for a in accounts], "active_account": active}

    def test_connection(self, data):
        self._log("CALL test_connection")
        acct = normalize_account(data or {})
        results = check_connection(acct)
        ok = all(r[0] for r in results)
        return {"ok": ok, "error": None if ok else "; ".join(l for okk, l in results if not okk)}

    def list_messages(self, account_id, server_folder):
        self._log(f"CALL list_messages {account_id} {server_folder}")
        acct = self._acct(account_id)
        imap, _, _ = connect_imap(acct)
        try:
            envelopes = fetch_envelopes(imap, server_folder, int(self.cfg["max_messages"]))
        finally:
            imap.logout()
        envelopes, moved = apply_rules(self.cfg, account_id, server_folder, envelopes)
        if moved:
            # re-open a fresh connection; apply_rules used its own
            imap2, _, _ = connect_imap(acct)
            try:
                envelopes = fetch_envelopes(imap2, server_folder, int(self.cfg["max_messages"]))
                envelopes, _ = apply_rules(self.cfg, account_id, server_folder, envelopes)
            finally:
                imap2.logout()
        unread = sum(1 for e in envelopes if not e["seen"])
        return {"envelopes": envelopes, "unread": unread, "auto_moved": moved}

    def get_message(self, account_id, server_folder, uid):
        self._log(f"CALL get_message {account_id} {server_folder} {uid}")
        acct = self._acct(account_id)
        imap, _, _ = connect_imap(acct)
        try:
            return fetch_message(imap, server_folder, uid)
        finally:
            imap.logout()

    def send_mail(self, account_id, to, subject, body, body_html=None):
        """Send as the given account. The From identity comes from the
        account config alone - there is deliberately no from parameter."""
        self._log(f"CALL send_mail {account_id} to={to} subject={subject[:40]}")
        acct = self._acct(account_id)
        send_message(acct, to, subject, body, body_html=body_html)
        return {"ok": True, "sent_as": from_address(acct)}

    def save_draft(self, account_id, to, subject, body, body_html=None):
        self._log(f"CALL save_draft {account_id} subject={subject[:40]}")
        acct = self._acct(account_id)
        save_draft_message(acct, to, subject, body, body_html=body_html)
        return {"ok": True}

    def mark_all_read(self, account_id, server_folder):
        self._log(f"CALL mark_all_read {account_id} {server_folder}")
        count = mark_all_read(self._acct(account_id), server_folder)
        return {"count": count}

    def set_seen(self, account_id, server_folder, uid, seen):
        self._log(f"CALL set_seen {account_id} {server_folder} {uid} seen={seen}")
        set_seen(self._acct(account_id), server_folder, uid, bool(seen))
        return {"ok": True}

    def save_attachment(self, account_id, server_folder, uid, part_index):
        self._log(f"CALL save_attachment {account_id} {server_folder} {uid} part={part_index}")
        path = save_attachment(self._acct(account_id), server_folder, uid, int(part_index))
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

    def delete_message(self, account_id, server_folder, uid):
        self._log(f"CALL delete_message {account_id} {server_folder} {uid}")
        delete_message(self._acct(account_id), server_folder, uid)
        return {"ok": True}

    def move_message(self, account_id, server_folder, uid, target, sender="", learn=True):
        self._log(f"CALL move_message {account_id} {server_folder} {uid} -> {target} learn={learn}")
        move_message(self._acct(account_id), server_folder, uid, target)
        learned = None
        if learn:
            dom = sender_domain(sender) if sender else None
            if dom:
                learn_rule(self.cfg, account_id, dom, target)
                learned = dom
        return {"ok": True, "learned": learned}

    def create_folder(self, account_id, name):
        self._log(f"CALL create_folder {account_id} {name}")
        create_folder(self._acct(account_id), name)
        return {"ok": True}

    def remove_rule(self, account_id, domain):
        self._log(f"CALL remove_rule {account_id} {domain}")
        removed = remove_rule(self.cfg, account_id, domain)
        return {"ok": removed}

    def learn_rule(self, account_id, domain, target):
        self._log(f"CALL learn_rule {account_id} {domain} -> {target}")
        learn_rule(self.cfg, account_id, domain, target)
        return {"ok": True}

    def check_update(self):
        """Return {available, version, notes, asset_name, url} or error."""
        self._log("CALL check_update")
        try:
            info = check_for_update()
        except Exception as e:
            return {"available": False, "error": str(e)}
        if not info or not info["asset_url"]:
            arch = info["arch"] if info else current_arch()
            return {"available": False,
                    "error": f"No {arch} build published for this release yet"}
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

    def __init__(self, acct):
        super().__init__(daemon=True)
        self.acct = acct
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
            imap, _, _ = connect_imap(self.acct)
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
        imap, _, _ = connect_imap(self.acct)
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
                rule_target = self.acct.get("rules", {}).get(domain)
                if rule_target and rule_target != "INBOX":
                    continue  # learned rule routes this elsewhere
                label = self.acct.get("label") or self.acct.get("email") or ""
                show_toast(f"[{label}] {subject}" if label else subject, short_sender(sender))
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
        if not cfg.accounts():
            print("Account: (not set - run the app and enter credentials)")
            return 1
        all_ok = True
        for acct in cfg.accounts():
            print(f"Account: {acct['label']} <{from_address(acct)}> (mailbox {acct['email']})")
            results = check_connection(acct)
            for ok, line in results:
                print(("  OK   " if ok else "  FAIL ") + line)
            all_ok = all_ok and all(ok for ok, _ in results)
        return 0 if all_ok else 1

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

    # Native inbox notifications (like Outlook): one quiet poller per account,
    # each toast labelled with its mailbox so arrivals are never ambiguous.
    pollers = []
    for acct in cfg.accounts():
        if acct["email"] and acct["password"]:
            poller = MailPoller(acct)
            poller.start()
            pollers.append(poller)

    try:
        webview.start()
    finally:
        for poller in pollers:
            poller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
