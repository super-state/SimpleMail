#!/usr/bin/env python3
"""DMARC watchdog for playloudr.com - silent unless something fails.

Scans the playloudr mailbox for DMARC aggregate reports (Microsoft / Google /
Yahoo send them daily because the DMARC record has rua=mailto:hello@playloudr.com),
parses every attachment, and prints an alert ONLY when a message failed
authentication (possible spoofing or a broken legit sender).

Behaviour (designed for --no-agent cron: stdout is delivered verbatim):
  - first run after wiring: one short heartbeat so the watcher proves it is live
  - a NEW failing record: concise alert with source IPs + auth results
  - all pass / nothing new: prints NOTHING (silent tick)
  - script crashes (IMAP down, config missing): non-zero exit -> cron alerts

Credentials: ~/SimpleMail/config.json -> cfg["playloudr"] object
State: ~/.hermes/scripts/data/dmarc-state.json (first-run flag + alert dedup)
"""
import argparse
import datetime as dt
import email
import gzip
import imaplib
import io
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "dmarc-state.json")
CONFIG_FILE = os.path.join(os.path.expanduser("~"), "SimpleMail", "config.json")
LOOKBACK_DAYS = 5
ALERT_DEDUP_MAX = 300

FAIL_NOTES = (
    "DMARC policy for playloudr.com is p=none (monitor only) - nothing was "
    "blocked, but this mail FAILED authentication while claiming to be from "
    "playloudr.com. If it is a sender you recently set up, fix its SPF/DKIM. "
    "If it is unexpected, this looks like spoofing."
)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {"first_run_done": False, "alerted": []}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def load_playloudr_cfg():
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if "playloudr" in cfg:
        return cfg["playloudr"]
    raise RuntimeError(
        "no 'playloudr' object in ~/SimpleMail/config.json - add "
        "{imap_host, imap_port, email, password} for the playloudr mailbox"
    )


def fetch_report_emails(acct):
    imap = imaplib.IMAP4_SSL(acct["imap_host"], int(acct.get("imap_port", 993)), timeout=30)
    imap.login(acct["email"], acct["password"])
    imap.select("INBOX")
    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = imap.search(None, f'(SINCE "{since}")')
    uids = data[0].split()
    hits = []
    for uid in uids:
        typ, msg_data = imap.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
        if not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subj = (msg.get("Subject") or "").lower()
        frm = (msg.get("From") or "").lower()
        if any(k in subj for k in ("dmarc", "aggregate report", "report domain")) or \
           any(k in frm for k in ("dmarcreport", "dmarc-support")):
            hits.append(uid)
    messages = []
    for uid in hits:
        typ, msg_data = imap.fetch(uid, "(RFC822)")
        if msg_data and msg_data[0]:
            messages.append(email.message_from_bytes(msg_data[0][1]))
    imap.logout()
    return messages


def extract_xml(msg):
    """Return the first XML blob found in a report email's attachments."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            fn = part.get_filename()
            ct = part.get_content_type()
            if fn or ct in ("application/xml", "application/zip",
                            "application/gzip", "application/octet-stream"):
                parts.append((fn, ct, part.get_payload(decode=True)))
    else:
        parts.append((msg.get_filename(), msg.get_content_type(), msg.get_payload(decode=True)))
    for fn, ct, blob in parts:
        if not blob:
            continue
        if zipfile.is_zipfile(io.BytesIO(blob)):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                return z.read(z.namelist()[0])
        if blob[:2] == b"\x1f\x8b":
            return gzip.decompress(blob)
        if ct in ("application/xml", "text/xml") or blob.lstrip()[:1] == b"<":
            return blob
    return None


def parse_report(xml_text):
    """Return (report_id, org, begin, end, records). records: list of
    {ip, count, spf, dkim, disposition, auth}"""
    root = ET.fromstring(xml_text)
    meta = root.find(".//{*}report_metadata")
    rid = meta.find("{*}report_id").text if meta is not None and meta.find("{*}report_id") is not None else "?"
    org = meta.find("{*}org_name").text if meta is not None else "?"
    begin = meta.find("{*}date_range/{*}begin").text if meta is not None else "?"
    end = meta.find("{*}date_range/{*}end").text if meta is not None else "?"
    records = []
    for r in root.findall(".//{*}record"):
        row = r.find("{*}row")
        pe = row.find("{*}policy_evaluated")
        auths = []
        for ar in r.findall("{*}auth_results/*"):
            name = ar.tag.split("}")[-1]
            res = ar.find("{*}result").text if ar.find("{*}result") is not None else "?"
            auths.append(f"{name}={res}")
        records.append({
            "ip": row.find("{*}source_ip").text,
            "count": int(row.find("{*}count").text or 0),
            "spf": pe.find("{*}spf").text if pe is not None else "?",
            "dkim": pe.find("{*}dkim").text if pe is not None else "?",
            "disp": pe.find("{*}disposition").text if pe is not None else "?",
            "auth": ", ".join(auths) or "-",
        })
    return rid, org, begin, end, records


def ts(epoch):
    try:
        return dt.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(epoch)


def format_alert(fails):
    """fails: list of (org, begin, end, rec). Build the Telegram alert text."""
    lines = ["⚠️ DMARC alert — playloudr.com"]
    for org, begin, end, rec in fails:
        lines.append(
            f"• {org}: {rec['ip']} sent {rec['count']} msg(s) failing auth "
            f"(spf={rec['spf']}, dkim={rec['dkim']}) [{ts(begin)}]"
        )
    lines.append("")
    lines.append(FAIL_NOTES)
    return "\n".join(lines)


def scan():
    acct = load_playloudr_cfg()
    messages = fetch_report_emails(acct)
    fails = []
    total_reports = 0
    seen_ids = []
    for msg in messages:
        xml_text = extract_xml(msg)
        if not xml_text:
            continue
        try:
            rid, org, begin, end, records = parse_report(xml_text)
        except ET.ParseError:
            continue
        total_reports += 1
        seen_ids.append(rid)
        for rec in records:
            if rec["spf"] != "pass" or rec["dkim"] != "pass":
                fails.append((org, begin, end, rec, rid))

    state = load_state()
    if not state["first_run_done"]:
        state["first_run_done"] = True
        state["alerted"] = (state.get("alerted") or [])[:ALERT_DEDUP_MAX]
        save_state(state)
        print(
            f"✅ DMARC watcher live — scanned {total_reports} report(s) for "
            f"playloudr.com (last {LOOKBACK_DAYS} days), all clear. "
            "You will only hear from me again if mail fails authentication."
        )
        return

    new_fails = []
    alerted = set(state.get("alerted") or [])
    for org, begin, end, rec, rid in fails:
        key = f"{rid}:{rec['ip']}"
        if key not in alerted:
            new_fails.append((org, begin, end, rec))
            alerted.add(key)
    if new_fails:
        state["alerted"] = list(alerted)[-ALERT_DEDUP_MAX:]
        save_state(state)
        print(format_alert(new_fails))
    # else: all quiet - print nothing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="run the alert path with a synthetic failing report")
    args = ap.parse_args()
    if args.self_test:
        fake = [("outlook.com", "1787011200", "1787097600",
                 {"ip": "203.0.113.77", "count": 3, "spf": "fail", "dkim": "fail",
                  "disp": "none", "auth": "spf=fail dkim=fail"})]
        print(format_alert(fake))
        return
    scan()


if __name__ == "__main__":
    main()
