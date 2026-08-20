# SimpleMail companion scripts

Standalone Python scripts that run **on the Pi** (`openclaw-pi.local`, hostname
`collector-runner`) as Hermes cron jobs — NOT inside the desktop app. They are
deployed to `~/.hermes/scripts/` and registered in the Pi's Hermes cron store
(`hermes cron create ...`); Windows keeps mirror copies under
`~/AppData/Local/hermes/scripts/` (via the hermes CLI).

Credentials are NEVER embedded — both scripts read the account settings at
runtime from `~/SimpleMail/config.json` (Pi, flat schema) or
`%APPDATA%\SimpleMail\config.json` (Windows, v1.1 `accounts[]` schema +
a `playloudr` object for the DMARC watcher). That config is never committed.

## mail_triage.py — inbox triage agent

Daily job `04afff60cd3e` (08:00, agent mode — the report feeds an LLM digest
delivered to Telegram). Classifies NEW mail since the last run into
JUNK / MARKETING / REVIEW:

- **Hard safety rules (by design):** never deletes or expunges (MOVE only),
  never sends (no SMTP code), every action appended to `agent/audit.log`,
  only HIGH-confidence items are moved, everything else stays in INBOX and
  is surfaced in the report for a human.
- **PROTECTED_DOMAINS** (google.com, facebook.com, apple.com): platform mail
  is always kept in INBOX — developer/status mail that is often important.
- Learns rules into `agent/triage_rules.json`, state in
  `agent/triage_state.json`, extraction candidates appended to
  `agent/knowledge_proposals.md`.
- Run: `python3 mail_triage.py` (live) or `--dry-run` (classify only, no
  moves, no state/rule changes).

## dmarc-watch.py — DMARC watchdog

Daily job `a32abdcefa3d` (08:30, `--no-agent` — stdout delivered verbatim to
Telegram, empty stdout = silent). playloudr.com's DMARC record sends aggregate
reports (Microsoft/Google/Yahoo) to hello@playloudr.com; this script parses
every attachment and prints an alert **only** when a message failed SPF/DKIM
(possible spoofing) — with per-source dedup so a persistent problem doesn't
spam daily. First run after wiring prints a one-time "watcher live" heartbeat.
Script crashes (IMAP down, config missing) exit non-zero and the cron engine
alerts automatically — a broken watchdog can't fail silently.

- Run: `python3 dmarc-watch.py` (scan) or `--self-test` (synthetic alert).
- State: `scripts/data/dmarc-state.json` (first-run flag + alerted keys).

## Verification

```bash
python3 -m py_compile scripts/mail_triage.py scripts/dmarc-watch.py
python3 scripts/dmarc-watch.py --self-test
python3 scripts/mail_triage.py --dry-run   # read-only, real inbox
```
