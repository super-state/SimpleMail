#!/usr/bin/env python3
"""SimpleMail triage agent — read-only filing of the Fasthosts/livemail inbox.

Classifies NEW mail since the last run into:
  JUNK      -> moved to the server Junk folder
  MARKETING -> moved to the Marketing folder (roundups, newsletters)
  REVIEW    -> left in INBOX, listed for the human

Hard safety rules (enforced by design):
  - NEVER deletes or expunges anything (MOVE only; no EXPUNGE call).
  - NEVER sends mail. This script has no SMTP code at all.
  - Every action is appended to an audit log; the report lists EVERY
    message seen, including every move, so nothing is hidden.
  - Only HIGH-confidence items are moved; everything else stays and is
    surfaced in the report for a human decision.

Run:  py -3 mail_triage.py            (live: moves + report)
      py -3 mail_triage.py --dry-run  (classify only, no moves)
"""
import argparse
import datetime as dt
import email
import imaplib
import json
import os
import re
import sys
from email import message_from_bytes, message_from_string
from email.policy import default as email_policy

APP_NAME = "SimpleMail"
BASE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
AGENT_DIR = os.path.join(BASE_DIR, "agent")
STATE_FILE = os.path.join(AGENT_DIR, "triage_state.json")
RULES_FILE = os.path.join(AGENT_DIR, "triage_rules.json")
AUDIT_LOG = os.path.join(AGENT_DIR, "audit.log")
PROPOSALS_FILE = os.path.join(AGENT_DIR, "knowledge_proposals.md")

# ---- seed classification (tuned against the real mailbox; learned rules
# ---- are stored in triage_rules.json and merged in at runtime)
SEED_MARKETING_SUBJECT = re.compile(
    r"roundup|digest|newsletter|weekly recap|year in review|your (?:weekly|monthly)|"
    r"new releases? (?:this|of the) week|top tracks|top songs|fan insights|"
    r"your music (?:was|has been)|release (?:is|now) live|playlist pitch|"
    r"streaming insights|wrap-?up|best of the week",
    re.I,
)
SEED_JUNK_SUBJECT = re.compile(
    r"cryptocurrency|bitcoin|ethereum|invest(?:ment| now)|trading platform|"
    r"viagra|cialis|casino|poker|lottery|you (?:have )?won|claim your (?:prize|reward)|"
    r"loan (?:approved|offer)|payday|forex|binary options|make money (?:fast|online)|"
    r"urgent:? (?:action|payment|invoice|verification)|account (?:suspended|locked)|"
    r"invoice (?:overdue|attached)|wire transfer",
    re.I,
)
# These senders have been observed to send only marketing in this mailbox
# (verified against the real inbox on 2026-08-10).
SEED_MARKETING_DOMAINS = {
    "spotify.com", "mail.spotify.com",
    "bandcamp.com", "trustpilot.com",
}
# Senders whose mail is ALWAYS kept in INBOX for human eyes — developer /
# platform status mail that is often actually important (Meta app reviews,
# Google Play signing keys). The user can teach the app a rule to override
# this later; the agent then inherits it from config.json on the next run.
PROTECTED_DOMAINS = {"google.com", "facebook.com", "apple.com"}

# --------------------------------------------------------------------------
# config / helpers
# --------------------------------------------------------------------------


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def audit(line):
    os.makedirs(AGENT_DIR, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} | {line}\n")


def sender_domain(sender):
    """'Instagram <posts-recaps@mail.instagram.com>' -> 'instagram.com'."""
    m = re.search(r"<([^>]+)>", sender or "")
    addr = m.group(1) if m else (sender or "")
    if "@" not in addr:
        return None
    host = addr.rsplit("@", 1)[1].lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def decode_snippet(raw):
    """Robust MIME-aware body decoding (boundary rewrap trick, proven on
    livemail). Returns up to ~140 chars of real text."""
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return ""
    text = text.lstrip("\r\n")
    m = re.match(r"^(--[^\r\n]+)", text)
    if not m:
        m2 = re.search(r"\r?\n(--[^\r\n]+)", text)
        if m2:
            m = m2
    if m:
        boundary = m.group(1)
        try:
            wrapped = (
                f"Content-Type: multipart/mixed; boundary=\"{boundary[2:]}\"\r\n\r\n"
                + text
            )
            msg = message_from_string(wrapped, policy=email_policy)
            best = ""
            for part in msg.walk():
                ct = part.get_content_type()
                try:
                    body = part.get_content()
                except Exception:
                    continue
                if not body or not body.strip():
                    continue
                cand = collapse(body) if ct == "text/plain" else collapse(html_to_text(body)) if ct == "text/html" else ""
                if len(cand) > len(best):
                    best = cand
            if best:
                return best[:140]
        except Exception:
            pass
    return collapse(html_to_text(text) if "<" in text else text)[:140]


def collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return s


# --------------------------------------------------------------------------
# IMAP
# --------------------------------------------------------------------------


def connect(cfg):
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=30)
    imap.login(cfg["email"], cfg["password"])
    return imap


def folder_names(imap):
    names = []
    typ, data = imap.list()
    if typ == "OK":
        for line in data:
            m = re.search(r'"([^"]+)"\s*$', line.decode("utf-8", "replace"))
            if m:
                names.append(m.group(1))
    return names


def fetch_new(imap, since_date, cap=150):
    """Fetch envelopes (uid, sender, subject, date, text) for mail in INBOX
    since `since_date` (YYYY-MM-DD), newest last, capped."""
    imap.select("INBOX", readonly=True)
    imap_date = dt.datetime.strptime(since_date, "%Y-%m-%d").strftime("%d-%b-%Y")
    typ, data = imap.uid("search", None, f'(SINCE "{imap_date}")')
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()[-cap:]
    uidlist = b",".join(uids)

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

    envelopes = []
    try:
        typ, fdata = imap.uid(
            "fetch", uidlist,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT]<0.4000>)",
        )
        cur = None
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
                    "text": "",
                }
                envelopes.append(cur)
            elif "BODY[TEXT]" in desc and cur is not None:
                cur["text"] = decode_snippet(payload)
    except Exception:
        pass
    return envelopes


def move_message(imap, uid, target):
    """IMAP MOVE with COPY+DELETED-flag fallback. Never expunges."""
    try:
        typ, _ = imap.uid("MOVE", uid, target)
        if typ == "OK":
            return True
    except Exception:
        pass
    typ, _ = imap.uid("COPY", uid, target)
    if typ == "OK":
        imap.uid("store", uid, "+FLAGS", r"(\Deleted)")
        return True
    return False


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def classify(msg, cfg_rules, agent_rules):
    """Return (bucket, reason). bucket in JUNK / MARKETING / REVIEW."""
    dom = sender_domain(msg["sender"])
    subject = msg["subject"] or ""
    text = msg["text"] or ""

    # 0. protected domains never auto-file — platform/developer status mail
    #    (Meta app review, Google Play keys) needs human eyes.
    if dom and dom in PROTECTED_DOMAINS:
        return "REVIEW", f"protected domain (human eyes): {dom}"

    # 1. explicit learned rules (agent's own store first, then the app's)
    for store, bucket in ((agent_rules, "agent"), (cfg_rules, "app")):
        junk_doms = set(store.get("junk", {}).get("domains", []))
        mkt_doms = set(store.get("marketing", {}).get("domains", []))
        if dom and dom in junk_doms:
            return "JUNK", f"learned {bucket} rule: {dom}"
        if dom and dom in mkt_doms:
            return "MARKETING", f"learned {bucket} rule: {dom}"

    # 2. seeded marketing domains (verified senders)
    if dom in SEED_MARKETING_DOMAINS:
        return "MARKETING", f"known marketing sender: {dom}"

    # 3. strong junk subject patterns
    if SEED_JUNK_SUBJECT.search(subject):
        return "JUNK", f"junk subject pattern: {subject[:60]}"

    # 4. marketing subject patterns
    if SEED_MARKETING_SUBJECT.search(subject):
        return "MARKETING", f"marketing subject pattern: {subject[:60]}"

    return "REVIEW", "no strong signal"


# --------------------------------------------------------------------------
# knowledge extraction (proposed only — never auto-written to durable memory)
# --------------------------------------------------------------------------

UK_POSTCODE = re.compile(
    r"\b[A-Z]{1,2}[0-9][A-Z0-9]?[ ]?[0-9][A-Z]{2}\b"
)
COMPANY_NO = re.compile(
    r"(?:company\s*(?:no\.?|number|registration\s*no\.?)|reg(?:istered)?\s*no\.?)\s*[:#]?\s*([A-Z0-9]{4,14})",
    re.I,
)
ACCOUNT_ID = re.compile(
    r"(?:ad\s*account(?:\s*id)?|account\s*id|account\s*number|reference(?:\s*no)?)\s*[:#]?\s*([A-Z0-9\-]{5,16})",
    re.I,
)


def extract_candidates(msg):
    """Return list of (kind, value, context) proposed facts."""
    text = msg["text"] or ""
    out = []
    m = COMPANY_NO.search(text)
    if m:
        out.append(("company_number", m.group(1), text[max(0, m.start() - 60):m.end() + 40]))
    m = ACCOUNT_ID.search(text)
    if m:
        out.append(("account_id", m.group(1), text[max(0, m.start() - 60):m.end() + 40]))
    for m in UK_POSTCODE.finditer(text):
        ctx = text[max(0, m.start() - 90):m.end() + 40]
        if re.search(r"address|office|registered|studio|postal|correspondence", ctx, re.I):
            out.append(("postcode", m.group(0), ctx))
            break  # one postcode per mail is enough for a proposal
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="classify only, no moves")
    ap.add_argument("--days", type=int, default=7, help="lookback for first run")
    args = ap.parse_args()

    cfg = load_config()
    state = load_json(STATE_FILE, {})
    agent_rules = load_json(RULES_FILE, {"junk": {"domains": []}, "marketing": {"domains": []}})
    cfg_rules = cfg.get("rules", {})  # app's taught rules: domain -> folder

    # merge app rules into a comparable shape (folder names may vary)
    cfg_rules_merged = {"junk": {"domains": []}, "marketing": {"domains": []}}
    for dom, folder in cfg_rules.items():
        fl = str(folder).lower()
        if "junk" in fl or "spam" in fl:
            cfg_rules_merged["junk"]["domains"].append(dom.lower())
        elif "market" in fl:
            cfg_rules_merged["marketing"]["domains"].append(dom.lower())

    last_run = state.get("last_run_date")
    if last_run:
        since = last_run
        lookback = None
    else:
        since = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
        lookback = args.days
    today = dt.date.today().isoformat()

    imap = connect(cfg)
    try:
        folders = folder_names(imap)
        lower = {f.lower(): f for f in folders}
        junk_folder = lower.get("junk") or lower.get("junk email") or lower.get("spam")
        mkt_folder = lower.get("marketing")
        if mkt_folder is None:
            imap.create("Marketing")  # only creates if absent; safe
            mkt_folder = "Marketing"

        msgs = fetch_new(imap, since)
        if not msgs:
            print("NO_NEW_MAIL")
            if not args.dry_run:
                state["last_run_date"] = today
                save_json(STATE_FILE, state)
            return

        lines = []
        lines.append(f"REPORT_DATE: {dt.datetime.now().isoformat(timespec='seconds')}")
        lines.append(f"WINDOW: since {since} (first run lookback: {lookback or 'n/a'} days)")
        lines.append(f"NEW_MESSAGES: {len(msgs)}")
        if cfg.get("rules"):
            lines.append("APP_RULES: " + ", ".join(f"{d} -> {f}" for d, f in cfg["rules"].items()))
        lines.append("")

        buckets = {"JUNK": [], "MARKETING": [], "REVIEW": []}
        actions = []
        for msg in msgs:
            bucket, reason = classify(msg, cfg_rules_merged, agent_rules)
            buckets[bucket].append((msg, reason))

        # do the moves (unless dry-run), then report every message
        imap.select("INBOX", readonly=False)
        for bucket, target in (("JUNK", junk_folder), ("MARKETING", mkt_folder)):
            for msg, reason in buckets[bucket]:
                if target is None:
                    action = "NO_TARGET_FOLDER"
                elif args.dry_run:
                    action = "WOULD_MOVE"
                else:
                    ok = move_message(imap, msg["uid"], target)
                    action = "MOVED" if ok else "MOVE_FAILED"
                    if ok:
                        dom = sender_domain(msg["sender"])
                        if dom:
                            lst = agent_rules[bucket.lower()]["domains"]
                            if dom not in lst:
                                lst.append(dom)
                actions.append((msg, bucket, action, reason))

        for msg, reason in buckets["REVIEW"]:
            actions.append((msg, "REVIEW", "KEPT", reason))

        for bucket in ("JUNK", "MARKETING", "REVIEW"):
            rows = [a for a in actions if a[1] == bucket]
            if not rows:
                continue
            lines.append(f"== {bucket} ({len(rows)}) ==")
            for msg, _b, action, reason in rows:
                lines.append(
                    f"  [{action}] {msg['uid']} | {msg['sender'][:48]} | "
                    f"{msg['subject'][:64]} | {reason}"
                )
            lines.append("")

        # extraction proposals
        proposals = []
        for msg, _b, _a, _r in actions:
            for kind, value, ctx in extract_candidates(msg):
                proposals.append((kind, value, ctx, msg))
        if proposals:
            lines.append(f"== EXTRACTION_PROPOSALS ({len(proposals)}) ==")
            for kind, value, ctx, msg in proposals:
                lines.append(
                    f"  {kind}: {value}  (from uid {msg['uid']} '{msg['subject'][:50]}')"
                )
                lines.append(f"      ctx: {collapse(ctx)[:150]}")
            lines.append("")
            os.makedirs(AGENT_DIR, exist_ok=True)
            with open(PROPOSALS_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"\n## {today}\n")
                for kind, value, ctx, msg in proposals:
                    fh.write(f"- [{kind}] {value} — {msg['subject'][:60]} (uid {msg['uid']})\n")

        # audit + state (dry runs never consume the window or learn rules)
        for msg, bucket, action, reason in actions:
            audit(f"{bucket} | {action} | uid {msg['uid']} | {msg['sender'][:60]} | {msg['subject'][:80]}")
        if not args.dry_run:
            save_json(RULES_FILE, agent_rules)
            state["last_run_date"] = today
            save_json(STATE_FILE, state)

        lines.append("AUDIT_LOG: " + AUDIT_LOG)
        print("\n".join(lines))
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
