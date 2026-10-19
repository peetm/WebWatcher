#!/usr/bin/env python3
"""
WebWatcher - website change monitor
===================================

Polls a list of web pages on a schedule and emails you a readable diff when
their *visible text* changes.  Runs as a one-shot check, a foreground loop, or
a Windows service that starts itself at boot.

How change detection works
--------------------------
The naive approach - hash the raw HTML - fires constantly on any real site,
because adverts rotate, session tokens change and markup churns.  WebWatcher
throws all of that away before it compares anything:

    1. fetch the page                    (3 attempts, backoff between them)
    2. strip the noise                   scripts, styles, forms, svg, iframes
    3. strip the chrome                  nav / header / footer / aside
    4. strip dynamic blocks              class or id token matching advert,
                                         cookie, session, csrf, promo, ...
    5. reduce to visible text lines      collapse whitespace, drop repeats
    6. SHA-256 those lines               different hash => something moved
    7. difflib the two line lists        gives exact added / removed lines
    8. discard moved and short lines     then apply the notify thresholds

Only step 8 decides whether you get an email.  Everything that survives to
step 7 is written to the change log either way, so nothing is ever lost just
because a threshold was not met - see the `history` command.

Where things live
-----------------
    script + config      C:\\utils\\webwatcher
    database, log,       C:\\ProgramData\\WebWatcher
    encrypted password

State is deliberately kept out of OneDrive.  A service running as LocalSystem
cannot hydrate OneDrive Files On-Demand placeholders, so the file may simply
not be there at boot; and a database rewritten every few minutes inside a
synced folder generates endless upload churn.

Override any of it with the WEBWATCHER_HOME, WEBWATCHER_DATA and
WEBWATCHER_CONFIG environment variables.

Commands
--------
    python webwatcher.py check              one pass over the sites now due
    python webwatcher.py check --force      every site, ignoring the schedule
    python webwatcher.py run                foreground loop, Ctrl+C to stop
    python webwatcher.py status             what is watched, and when
    python webwatcher.py history BBC        recent changes, with the lines
    python webwatcher.py deploy             copy script + config to DATA_DIR
    python webwatcher.py set-password       store the SMTP password encrypted
    python webwatcher.py test-email         send a test message
    python webwatcher.py migrate            import old website_data.json state
    python webwatcher.py service install --startup delayed
    python webwatcher.py service start|stop|restart|remove|update

The legacy --once and --continuous flags still work, as aliases for `check`
and `run`.

Installing as a service
-----------------------
See INSTALL.md.  The two traps that cost the most time, both worth repeating:

    * Dependencies must be installed SYSTEM-WIDE, elevated, with
      --ignore-installed.  A LocalSystem service cannot see per-user
      site-packages, and plain `pip install` leaves transitive dependencies
      behind in the user profile even when the top-level packages move.

    * pywin32 wants its options BEFORE the command.  This script reorders
      them for you, so either order works here.
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import getpass
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import smtplib
import sqlite3
import sys
import threading
import zlib
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Iterable, Optional, Sequence

import requests
from bs4 import BeautifulSoup, Comment


# ===========================================================================
#  Identity and locations
# ===========================================================================

APP_NAME     = "WebWatcher"
SERVICE_NAME = "WebWatcher"                 # the name Windows knows it by

# Where the code lives.  Config is looked up here first, so the copy sitting
# next to the script always wins over a forgotten one in the data directory.
BASE_DIR = Path(os.environ.get("WEBWATCHER_HOME") or Path(__file__).resolve().parent)

# Where mutable state lives - see the module docstring for why this is not
# simply BASE_DIR.
DATA_DIR = Path(
    os.environ.get("WEBWATCHER_DATA")
    or Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP_NAME
)

DB_FILE            = DATA_DIR / "webwatcher.db"
LOG_FILE           = DATA_DIR / "webwatcher.log"
SECRET_FILE        = DATA_DIR / "smtp_password.bin"
LEGACY_DATA_FILE   = BASE_DIR / "website_data.json"      # pre-2026 state file

PASSWORD_ENV_VAR = "WEBWATCHER_SMTP_PASSWORD"

log = logging.getLogger("webwatcher")


# ===========================================================================
#  Configuration
# ===========================================================================

DEFAULT_CONFIG: dict = {
    "websites": [
        {"url": "https://example.com", "name": "Example Site", "selector": None},
    ],
    "email": {
        "enabled":         True,
        "smtp_server":     "smtp.example.com",
        "smtp_port":       465,
        "security":        "ssl",           # "ssl" (465) | "starttls" (587) | "none"
        "sender_email":    "webwatcher@example.com",
        "recipient_email": "you@example.com",
    },
    "check_interval":     3600,             # default seconds between checks, per site
    "request_timeout":    30,               # seconds before a fetch is abandoned
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "min_change_chars":   120,              # net text change below this => no email
    "min_line_chars":     40,               # shorter lines ignored when diffing
    "max_reported_lines": 12,               # cap on lines quoted in one email
    "snapshot_retention": 20,               # versions kept per site
    "error_alert_after":  5,                # consecutive failures before alerting; 0 = off
    "log_level":          "INFO",
}


class ConfigError(RuntimeError):
    """Raised for anything the user must fix in config.json before we can run."""


def find_config(explicit: Optional[str]) -> Path:
    """
    Resolve which config.json to use, in descending priority:

        1. --config on the command line
        2. $WEBWATCHER_CONFIG
        3. config.json beside the script          <- normal case
        4. config.json in the data directory

    Config is *configuration*, not state, so the copy next to the script wins.
    If everything was deployed into DATA_DIR the two are the same directory
    anyway; keeping the script's own copy authoritative is what stops a stale
    file in DATA_DIR silently shadowing the one you are actually editing.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    env = os.environ.get("WEBWATCHER_CONFIG")
    if env:
        return Path(env).expanduser().resolve()

    for candidate in (BASE_DIR / "config.json", DATA_DIR / "config.json"):
        if candidate.exists():
            return candidate

    return BASE_DIR / "config.json"          # nothing yet - create it here


def load_config(path: Path) -> dict:
    """
    Load config.json, merge it over DEFAULT_CONFIG, and upgrade the two older
    file layouts in passing:

        * email settings nested under "notifications"  (original script)
        * "use_tls": true/false instead of "security"  (both old scripts)

    Sites with "enabled": false are dropped here, so nothing downstream has to
    think about them.  Raises ConfigError with something actionable if the file
    is missing or unusable.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=4), encoding="utf-8")
        raise ConfigError(f"Created a starter config at {path} - edit it, then re-run.")

    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = {**DEFAULT_CONFIG, **raw}

    # --- email: accept the old nested layout, prefer the new flat one -------
    email = dict(DEFAULT_CONFIG["email"])
    email.update(raw.get("notifications", {}).get("email", {}))
    email.update(raw.get("email", {}))

    # Derive "security" when only the legacy "use_tls" is present.  Port 465 is
    # implicit SSL from the first byte; 587 negotiates with STARTTLS.
    if not email.get("security"):
        if "use_tls" in email:
            email["security"] = "starttls" if email["use_tls"] else "ssl"
        else:
            email["security"] = "ssl" if int(email.get("smtp_port", 465)) == 465 else "starttls"

    cfg["email"] = email
    cfg.pop("notifications", None)            # fully superseded by cfg["email"]

    # --- websites: validate, normalise, drop the disabled ones -------------
    sites = []
    for i, site in enumerate(cfg.get("websites") or []):
        if not site.get("url"):
            raise ConfigError(f"websites[{i}] has no url")
        if site.get("enabled") is False:
            continue
        sites.append({
            "url":            site["url"].strip(),
            "name":           site.get("name") or site["url"],
            "selector":       site.get("selector") or None,
            "check_interval": int(site.get("check_interval") or cfg["check_interval"]),
        })

    if not sites:
        raise ConfigError(f"No enabled websites in {path}")

    cfg["websites"] = sites
    cfg["_path"]    = path                    # remembered so status/set-password can report it
    return cfg


# ===========================================================================
#  Password storage - DPAPI through ctypes, so pywin32 is not needed for this
# ===========================================================================
#
# The password is encrypted at MACHINE scope rather than user scope, so the
# service can decrypt it whatever account it ends up running under.  The
# trade-off is explicit: any administrator on this PC can decrypt it too.  It
# protects against the password travelling with a copied config file, not
# against a local admin.

CRYPTPROTECT_LOCAL_MACHINE = 0x04
_ENTROPY = b"WebWatcher/smtp/v1"             # extra entropy, ties the blob to this app


class _Blob(ctypes.Structure):
    """The DATA_BLOB struct that both DPAPI entry points take and return."""
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes):
    """
    Wrap bytes in a DATA_BLOB.  Returns (blob, buffer) - the caller MUST keep a
    reference to the buffer alive for as long as the blob is in use, or Python
    will free it out from under the API call.
    """
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _from_blob(blob: _Blob) -> bytes:
    """Copy a DATA_BLOB's contents back out into ordinary Python bytes."""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi(func_name: str, data: bytes) -> bytes:
    """
    Call CryptProtectData or CryptUnprotectData.  Raises OSError (via WinError)
    if the call fails - most often because the blob was produced on a different
    machine, or with different entropy.
    """
    if os.name != "nt":
        raise RuntimeError("DPAPI password storage is Windows-only")

    crypt32  = ctypes.WinDLL("crypt32",  use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func     = getattr(crypt32, func_name)

    blob_in, _keep_in  = _to_blob(data)       # _keep_* pin the buffers - see _to_blob
    entropy, _keep_ent = _to_blob(_ENTROPY)
    blob_out           = _Blob()

    # The two functions differ only in the second argument: a description
    # string going in, and nothing at all coming back out.
    description = APP_NAME if func_name == "CryptProtectData" else None
    ok = func(ctypes.byref(blob_in), description, ctypes.byref(entropy),
              None, None, CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return _from_blob(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)   # the API allocated it; we free it


def store_password(password: str) -> Path:
    """Encrypt `password` and write it to SECRET_FILE.  Returns the path written."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_bytes(_dpapi("CryptProtectData", password.encode("utf-8")))
    return SECRET_FILE


def resolve_password(cfg: dict) -> str:
    """
    Find the SMTP password, in descending priority:

        1. $WEBWATCHER_SMTP_PASSWORD          handy for testing, never persisted
        2. the DPAPI-encrypted SECRET_FILE    the normal case
        3. sender_password in config.json     works, but warns every time

    This is called on every send rather than cached, so replacing the stored
    password takes effect on the next notification with no service restart.
    """
    env = os.environ.get(PASSWORD_ENV_VAR)
    if env:
        return env

    if SECRET_FILE.exists():
        try:
            return _dpapi("CryptUnprotectData", SECRET_FILE.read_bytes()).decode("utf-8")
        except OSError as exc:
            log.error("Could not decrypt %s: %s", SECRET_FILE, exc)

    plain = cfg["email"].get("sender_password")
    if plain:
        log.warning("Using the plaintext password from config.json. Run "
                    "'python webwatcher.py set-password' to store it encrypted instead.")
        return plain

    raise ConfigError(f"No SMTP password. Set ${PASSWORD_ENV_VAR} or run "
                      f"'python webwatcher.py set-password'.")


# ===========================================================================
#  Storage - SQLite, compressed text only, bounded history
# ===========================================================================
#
# The old scripts kept the full HTML of every page in a JSON file that was
# rewritten in its entirety on every check: ~935 KB for three sites, growing
# without limit.  This keeps only the extracted text, zlib-compressed, and
# prunes snapshots on write - about 100 KB for the same three sites, settling
# near 190 KB, while also giving a real change history the old file never had.

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    url                TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    text_hash          TEXT,               -- SHA-256 of the visible text lines
    body               BLOB,               -- those lines, zlib-compressed
    char_count         INTEGER DEFAULT 0,
    last_check         TEXT,               -- ISO-8601 UTC
    last_change        TEXT,
    next_due           TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    error_alerted      INTEGER NOT NULL DEFAULT 0   -- alert already sent for this outage
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    text_hash   TEXT NOT NULL,
    body        BLOB NOT NULL,
    char_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_url ON snapshots(url, id DESC);

CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    summary     TEXT NOT NULL,
    char_delta  INTEGER NOT NULL,
    added       TEXT,                       -- JSON list of added lines
    removed     TEXT,                       -- JSON list of removed lines
    notified    INTEGER NOT NULL DEFAULT 0  -- 0 when the email could not be sent
);
CREATE INDEX IF NOT EXISTS idx_changes_url ON changes(url, id DESC);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def compress(text: str) -> bytes:
    """Compress text for storage.  Level 6 - the last level worth the CPU here."""
    return zlib.compress(text.encode("utf-8"), 6)


def decompress(blob: Optional[bytes]) -> str:
    """Inverse of compress().  A NULL/empty blob reads back as an empty string."""
    return zlib.decompress(blob).decode("utf-8") if blob else ""


class Store:
    """
    All persistent state, behind a single SQLite connection.

    The connection is shared across threads - the service runs its monitor loop
    on a worker thread while the SCM calls in on another - so it is opened with
    check_same_thread=False and every access is serialised behind an RLock.
    Getting this wrong shows up as "SQLite objects created in a thread can only
    be used in that same thread" the moment the service starts.

    Pass readonly=True for the reporting commands.  Once the service has run,
    the database and its -wal/-shm files belong to whatever account the service
    uses, and an ordinary user account can no longer write them; opening
    read-only is what lets `status` and `history` keep working regardless of who
    owns the files.  WAL is still honoured in this mode, so the view is current
    rather than stale - unlike immutable=1, which ignores the -wal entirely.
    """

    def __init__(self, path: Path = DB_FILE, readonly: bool = False):
        self.path     = path
        self._lock    = threading.RLock()
        self.readonly = readonly and path.exists()   # nothing to read => must create it

        if self.readonly:
            self.conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro",
                                        uri=True, timeout=30, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            return                                   # no PRAGMAs, no schema: all are writes

        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")      # readers never block the writer
        self.conn.execute("PRAGMA synchronous=NORMAL")    # plenty for a monitor's state
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --- reads -------------------------------------------------------------

    def get_site(self, url: str) -> Optional[sqlite3.Row]:
        """The stored row for one site, or None if it has never been seen."""
        with self._lock:
            return self.conn.execute("SELECT * FROM sites WHERE url = ?", (url,)).fetchone()

    def all_sites(self) -> list[sqlite3.Row]:
        """Every stored site, including any no longer in config.json."""
        with self._lock:
            return self.conn.execute("SELECT * FROM sites ORDER BY name").fetchall()

    def recent_changes(self, url: Optional[str] = None, limit: int = 10) -> list[sqlite3.Row]:
        """Most recent changes, newest first, optionally for one site only."""
        with self._lock:
            if url:
                return self.conn.execute(
                    "SELECT * FROM changes WHERE url = ? ORDER BY id DESC LIMIT ?", (url, limit)
                ).fetchall()
            return self.conn.execute(
                "SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # --- writes ------------------------------------------------------------

    def ensure_site(self, url: str, name: str) -> None:
        """Create the row for a newly configured site, or refresh its display name."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO sites (url, name) VALUES (?, ?) "
                "ON CONFLICT(url) DO UPDATE SET name = excluded.name",
                (url, name),
            )
            self.conn.commit()

    def record_success(self, url: str, name: str, text: str, text_hash: str,
                       next_due: datetime, changed: bool, retention: int) -> None:
        """
        Store the result of a successful check: update the site's current text,
        append a snapshot, prune old snapshots, and clear the error counters.

        `changed` only controls whether last_change is stamped - the text is
        stored either way, which is what makes a below-threshold change update
        the baseline quietly instead of re-reporting forever.
        """
        now  = utcnow_iso()
        body = compress(text)

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sites (url, name, text_hash, body, char_count, last_check,
                                   last_change, next_due, consecutive_errors, last_error, error_alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0)
                ON CONFLICT(url) DO UPDATE SET
                    name               = excluded.name,
                    text_hash          = excluded.text_hash,
                    body               = excluded.body,
                    char_count         = excluded.char_count,
                    last_check         = excluded.last_check,
                    last_change        = CASE WHEN ? THEN excluded.last_check ELSE sites.last_change END,
                    next_due           = excluded.next_due,
                    consecutive_errors = 0,
                    last_error         = NULL,
                    error_alerted      = 0
                """,
                (url, name, text_hash, body, len(text), now,
                 now if changed else None, iso(next_due), 1 if changed else 0),
            )
            self.conn.execute(
                "INSERT INTO snapshots (url, captured_at, text_hash, body, char_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (url, now, text_hash, body, len(text)),
            )
            self.conn.execute(                # keep the newest `retention` snapshots
                """
                DELETE FROM snapshots WHERE url = ? AND id NOT IN (
                    SELECT id FROM snapshots WHERE url = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (url, url, max(1, retention)),
            )
            self.conn.commit()

    def record_error(self, url: str, name: str, message: str, next_due: datetime) -> int:
        """
        Record a failed check and return the new consecutive-failure count.  The
        stored text is left alone, so a site that recovers diffs against what it
        last looked like rather than reporting itself wholly new.
        """
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sites (url, name, last_check, next_due, consecutive_errors, last_error)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(url) DO UPDATE SET
                    name               = excluded.name,
                    last_check         = excluded.last_check,
                    next_due           = excluded.next_due,
                    consecutive_errors = sites.consecutive_errors + 1,
                    last_error         = excluded.last_error
                """,
                (url, name, utcnow_iso(), iso(next_due), message),
            )
            self.conn.commit()
            row = self.get_site(url)
            return row["consecutive_errors"] if row else 1

    def mark_error_alerted(self, url: str) -> None:
        """Remember that this outage has been reported, so it is reported only once."""
        with self._lock:
            self.conn.execute("UPDATE sites SET error_alerted = 1 WHERE url = ?", (url,))
            self.conn.commit()

    def log_change(self, url: str, summary: str, char_delta: int,
                   added: Sequence[str], removed: Sequence[str], notified: bool) -> None:
        """
        Append to the change log.  `notified` records whether the email actually
        went out, so a mail outage is visible afterwards through `history`
        rather than silently losing the change.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO changes (url, detected_at, summary, char_delta, added, removed, notified) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, utcnow_iso(), summary, char_delta,
                 json.dumps(list(added)), json.dumps(list(removed)), int(notified)),
            )
            self.conn.commit()

    def set_next_due(self, url: str, next_due: datetime) -> None:
        """Reschedule a site without otherwise touching its state."""
        with self._lock:
            self.conn.execute("UPDATE sites SET next_due = ? WHERE url = ?", (iso(next_due), url))
            self.conn.commit()

    def prune_orphans(self, keep_urls: Iterable[str]) -> int:
        """
        Delete every site not in `keep_urls`, with its snapshots and change log,
        then VACUUM to actually give the space back.  Returns how many went.
        """
        keep    = set(keep_urls)
        removed = 0

        with self._lock:
            for row in self.all_sites():
                if row["url"] not in keep:
                    self.conn.execute("DELETE FROM snapshots WHERE url = ?", (row["url"],))
                    self.conn.execute("DELETE FROM changes   WHERE url = ?", (row["url"],))
                    self.conn.execute("DELETE FROM sites     WHERE url = ?", (row["url"],))
                    removed += 1
            if removed:
                self.conn.commit()
                self.conn.execute("VACUUM")

        return removed


# ===========================================================================
#  Time helpers
# ===========================================================================
#
# Everything is stored as timezone-aware ISO-8601 UTC and converted to local
# time only for display.  That keeps the schedule sane across a BST/GMT change,
# which a service running for months will certainly meet.

def utcnow() -> datetime:
    """Now, timezone-aware, in UTC."""
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Format a datetime for storage: ISO-8601, UTC, whole seconds."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def utcnow_iso() -> str:
    """Shorthand for iso(utcnow())."""
    return iso(utcnow())


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """
    Parse a stored timestamp back to an aware datetime, tolerating both NULLs
    and anything unparseable by returning None.  Naive values are assumed UTC,
    which is what older rows contain.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_str(value: Optional[str]) -> str:
    """Render a stored timestamp in local time for humans; "never" if absent."""
    dt = parse_iso(value)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "never"


# ===========================================================================
#  Fetching and text extraction
# ===========================================================================

# Tags that never carry page content worth watching.
NOISE_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "template", "canvas",
    "form", "button", "input", "select", "option", "picture", "source", "head",
)

# Tags that are site chrome rather than content.  Skipped only when no selector
# was given - if you point a selector at a <nav>, you presumably meant it.
CHROME_TAGS = ("nav", "header", "footer", "aside")

# Matched against WHOLE class/id tokens, which is the entire point.  The
# original script did a substring replace over the extracted text with "ad" as
# one of the alternatives, so it deleted those two letters from inside ordinary
# words - "already" arrived in notifications as "alrey".
DYNAMIC_TOKEN_RE = re.compile(
    r"^(?:.*[-_])?(?:timestamp|datetime|session|csrf|nonce|token|counter|"
    r"ad|ads|advert|advertisement|promo|sponsored|cookie|cookies|consent|"
    r"newsletter|paywall|social|share|related|trending|recommended)"
    r"(?:[-_].*)?$",
    re.IGNORECASE,
)

WHITESPACE_RE = re.compile(r"\s+")           # \s covers &nbsp; in Unicode mode


def make_soup(markup: str) -> BeautifulSoup:
    """Parse with lxml when it is installed, falling back to the stdlib parser."""
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:
        return BeautifulSoup(markup, "html.parser")


def _is_dynamic(tag) -> bool:
    """True when a tag's class or id marks it as advert/session/cookie furniture."""
    classes = tag.get("class") or []
    if any(DYNAMIC_TOKEN_RE.match(c) for c in classes if isinstance(c, str)):
        return True
    tag_id = tag.get("id")
    return bool(tag_id and DYNAMIC_TOKEN_RE.match(tag_id))


def extract_lines(markup: str, selector: Optional[str] = None) -> list[str]:
    """
    Reduce a page to the list of visible text lines used for every comparison.

    Steps 2-5 of the pipeline in the module docstring.  Comparing this rather
    than the HTML is what makes detection stable: markup churn, rotating
    adverts and fresh session tokens all vanish before anything is hashed.

    A selector that matches nothing logs a warning and falls back to the whole
    page, which is far friendlier than silently reporting "no change" forever
    after a site quietly renames a div.
    """
    soup = make_soup(markup)

    for tag in soup(list(NOISE_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    root = soup
    if selector:
        selected = soup.select(selector)
        if selected:
            root = make_soup("".join(str(el) for el in selected))
        else:
            log.warning("Selector %r matched nothing; falling back to the whole page", selector)
            selector = None                   # fall through to the chrome strip below

    if not selector:
        for tag in root(list(CHROME_TAGS)):
            tag.decompose()

    for tag in list(root.find_all(True)):
        if tag.parent is not None and _is_dynamic(tag):   # parent check: already decomposed?
            tag.decompose()

    lines    = []
    previous = None
    for raw in root.get_text("\n").splitlines():
        line = WHITESPACE_RE.sub(" ", raw).strip()
        if len(line) < 2 or line == previous:             # drop blanks and runs of repeats
            continue
        previous = line
        lines.append(line)

    return lines


def fetch(url: str, cfg: dict, attempts: int = 3) -> str:
    """
    Fetch a page, retrying with a linear backoff.  Raises the last exception if
    every attempt fails; the caller turns that into a recorded error rather than
    letting one unreachable site abandon the whole pass.

    requests guesses ISO-8859-1 for any text/* response that does not declare a
    charset, which mangles every curly quote on the page, so that guess is
    overridden with the one derived from the content itself.
    """
    headers = {
        "User-Agent":      cfg["user_agent"],
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    timeout = int(cfg.get("request_timeout", 30))
    last: Exception = RuntimeError("no attempt made")

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except Exception as exc:              # reported to the caller after the last try
            last = exc
            if attempt < attempts:
                log.debug("Attempt %d/%d for %s failed: %s", attempt, attempts, url, exc)
                threading.Event().wait(2 * attempt)       # 2s, then 4s

    raise last


def text_digest(lines: Sequence[str]) -> str:
    """SHA-256 over the joined lines - the cheap "did anything move?" test."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ===========================================================================
#  Diffing
# ===========================================================================

class Diff:
    """The outcome of comparing two versions of a page, ready to summarise."""

    def __init__(self, added: list[str], removed: list[str],
                 old_chars: int, new_chars: int, ratio: float):
        self.added     = added
        self.removed   = removed
        self.old_chars = old_chars
        self.new_chars = new_chars
        self.ratio     = ratio                # difflib similarity, 0.0 - 1.0

    @property
    def char_delta(self) -> int:
        """Net change in characters; negative when the page shrank."""
        return self.new_chars - self.old_chars

    def summary(self) -> str:
        """One line for the email subject area, the log and the change history."""
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} line(s) added")
        if self.removed:
            parts.append(f"{len(self.removed)} line(s) removed")
        if self.char_delta:
            parts.append(f"{self.char_delta:+,} characters")
        parts.append(f"{self.ratio:.1%} similar to the previous version")
        return "; ".join(parts)


def diff_lines(old: Sequence[str], new: Sequence[str], min_line_chars: int) -> Diff:
    """
    Compare two line lists and return what genuinely arrived and left.

    Two filters do the real work of keeping notifications readable:

        * a line present on BOTH sides was merely moved, not added or removed,
          so it is dropped - otherwise any reordered menu reads as a rewrite
        * lines shorter than min_line_chars are dropped as furniture

    autojunk is disabled because difflib's heuristic treats lines recurring in
    more than 1% of a long list as noise, which on a news page is exactly the
    repeated headline text that matters.
    """
    matcher = difflib.SequenceMatcher(a=list(old), b=list(new), autojunk=False)
    old_set, new_set = set(old), set(new)

    added:   list[str] = []
    removed: list[str] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(old[i1:i2])

    added   = [ln for ln in added   if len(ln) >= min_line_chars and ln not in old_set]
    removed = [ln for ln in removed if len(ln) >= min_line_chars and ln not in new_set]

    return Diff(
        added     = added,
        removed   = removed,
        old_chars = sum(len(ln) for ln in old),
        new_chars = sum(len(ln) for ln in new),
        ratio     = matcher.ratio(),
    )


# ===========================================================================
#  Email
# ===========================================================================

class Mailer:
    """
    Composes and sends the notifications.

    Every send resolves the password afresh (see resolve_password), so swapping
    the stored credential needs no restart.  Failures are caught by the callers
    and recorded rather than raised - a mail outage must never stop monitoring.
    """

    def __init__(self, cfg: dict):
        self.cfg   = cfg
        self.email = cfg["email"]

    @property
    def enabled(self) -> bool:
        return bool(self.email.get("enabled", True))

    def _send(self, msg: EmailMessage) -> None:
        """Open the right kind of SMTP connection, authenticate, send, hang up."""
        server   = self.email["smtp_server"]
        port     = int(self.email["smtp_port"])
        security = (self.email.get("security") or "ssl").lower()
        password = resolve_password(self.cfg)

        smtp = (smtplib.SMTP_SSL(server, port, timeout=30) if security == "ssl"
                else smtplib.SMTP(server, port, timeout=30))
        try:
            if security == "starttls":
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()                   # re-introduce ourselves over TLS
            smtp.login(self.email["sender_email"], password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:                 # already disconnected - nothing to salvage
                pass

    def _compose(self, subject: str, plain: str, html_body: str) -> EmailMessage:
        """Build a multipart/alternative message: plain text first, then HTML."""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = self.email["sender_email"]
        msg["To"]      = self.email["recipient_email"]
        msg.set_content(plain)
        msg.add_alternative(html_body, subtype="html")
        return msg

    def send_change(self, name: str, url: str, diff: Diff, when: datetime) -> bool:
        """Send a change notification.  Returns True only if it actually went out."""
        if not self.enabled:
            log.info("Email disabled; not notifying about %s", name)
            return False

        cap     = int(self.cfg.get("max_reported_lines", 12))
        added   = diff.added[:cap]
        removed = diff.removed[:cap]
        stamp   = when.astimezone().strftime("%Y-%m-%d %H:%M:%S")

        plain = [f"{name} changed", "", f"URL:      {url}", f"Detected: {stamp}", "",
                 diff.summary(), ""]
        if added:
            plain.append(f"Added ({len(diff.added)} total):")
            plain += [f"  + {ln}" for ln in added]
            plain.append("")
        if removed:
            plain.append(f"Removed ({len(diff.removed)} total):")
            plain += [f"  - {ln}" for ln in removed]

        html_body = render_change_email(name, url, stamp, diff, added, removed)

        try:
            self._send(self._compose(f"Changed: {name}", "\n".join(plain), html_body))
            log.info("Notification sent for %s", name)
            return True
        except Exception as exc:              # a failed email must not stop the loop
            log.error("Could not send notification for %s: %s", name, exc)
            return False

    def send_error(self, name: str, url: str, failures: int, message: str) -> bool:
        """Send the one-off "this site keeps failing" alert.  True if it went out."""
        if not self.enabled:
            return False

        plain = (f"{name} has failed {failures} checks in a row.\n\n"
                 f"URL: {url}\nLast error: {message}\n")
        html_body = (
            '<div style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px">'
            f'<h2 style="color:#b3261e">{esc(name)} is not responding</h2>'
            f"<p>{failures} consecutive failed checks.</p>"
            f'<p><strong>URL:</strong> <a href="{esc(url)}">{esc(url)}</a></p>'
            f'<pre style="background:#f6f6f6;padding:12px;border-radius:6px;'
            f'white-space:pre-wrap">{esc(message)}</pre></div>'
        )

        try:
            self._send(self._compose(f"{APP_NAME}: {name} unreachable", plain, html_body))
            log.info("Unreachable alert sent for %s (%d consecutive failures)", name, failures)
            return True
        except Exception as exc:
            log.error("Could not send error alert for %s: %s", name, exc)
            return False

    def send_test(self) -> None:
        """Send a test message.  Deliberately lets exceptions out - see cmd_test_email."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._send(self._compose(
            f"{APP_NAME} test message",
            f"{APP_NAME} SMTP test sent {stamp}.",
            f'<p style="font-family:Segoe UI,Arial,sans-serif">{APP_NAME} SMTP test sent {stamp}.</p>',
        ))


def esc(value: str) -> str:
    """HTML-escape a value for interpolation into the email body."""
    return escape(str(value), quote=True)


def render_change_email(name: str, url: str, stamp: str, diff: Diff,
                        added: Sequence[str], removed: Sequence[str]) -> str:
    """
    Build the HTML notification.

    Styling is inline throughout because mail clients - Outlook especially -
    discard <style> blocks, and there are no literal braces anywhere for the
    same reason there is no CSS block: it all has to survive an f-string.
    """
    def block(title: str, lines: Sequence[str], total: int,
              colour: str, bg: str, marker: str) -> str:
        """One "Added"/"Removed" panel, or nothing at all when there is nothing to show."""
        if not lines:
            return ""
        items = "".join(f'<li style="margin:6px 0">{esc(marker)} {esc(line)}</li>' for line in lines)
        more  = (f'<p style="margin:8px 0 0;color:#666;font-size:13px">'
                 f"...and {total - len(lines)} more</p>" if total > len(lines) else "")
        return (f'<h3 style="color:{colour};margin:24px 0 8px">{esc(title)} ({total})</h3>'
                f'<ul style="background:{bg};border-left:4px solid {colour};padding:12px 12px 12px 32px;'
                f'border-radius:4px;margin:0;list-style:none">{items}</ul>{more}')

    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#222;line-height:1.5;max-width:680px;margin:0 auto;padding:8px">
  <h2 style="color:#1a73e8;border-bottom:3px solid #1a73e8;padding-bottom:8px;margin-top:0">{esc(name)} changed</h2>
  <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
    <tr><td style="padding:2px 12px 2px 0;color:#666">URL</td><td><a href="{esc(url)}">{esc(url)}</a></td></tr>
    <tr><td style="padding:2px 12px 2px 0;color:#666">Detected</td><td>{esc(stamp)}</td></tr>
  </table>
  <div style="background:#e8f0fe;padding:12px 16px;border-radius:6px;font-size:14px">{esc(diff.summary())}</div>
  {block("Added", added, len(diff.added), "#188038", "#e8f5e9", "+")}
  {block("Removed", removed, len(diff.removed), "#b3261e", "#fdecea", "-")}
  <p style="margin:32px 0 8px">
    <a href="{esc(url)}" style="background:#1a73e8;color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;font-weight:600">Open the page</a>
  </p>
  <p style="color:#888;font-size:12px;margin-top:24px">Sent by {APP_NAME} on {esc(os.environ.get("COMPUTERNAME", "this PC"))}.</p>
</div>"""


# ===========================================================================
#  Monitor - the orchestration
# ===========================================================================

class Monitor:
    """
    Ties config, storage and mail together, and owns the scheduling.

    Each site carries its own next_due in the database, so per-site intervals
    are honoured (the original scripts documented that and then ignored it),
    and a restart resumes the existing schedule instead of re-checking
    everything from scratch.
    """

    def __init__(self, cfg: dict, store: Store, ensure: bool = True):
        self.cfg    = cfg
        self.store  = store
        self.mailer = Mailer(cfg)
        if ensure:                            # skipped by the read-only commands
            for site in cfg["websites"]:
                store.ensure_site(site["url"], site["name"])

    def due_sites(self, now: Optional[datetime] = None) -> list[dict]:
        """The configured sites whose next_due has passed (never-checked ones count)."""
        now = now or utcnow()
        due = []
        for site in self.cfg["websites"]:
            row      = self.store.get_site(site["url"])
            next_due = parse_iso(row["next_due"]) if row else None
            if next_due is None or next_due <= now:
                due.append(site)
        return due

    def next_wakeup(self, now: Optional[datetime] = None) -> datetime:
        """
        When the loop should next wake: the earliest next_due across all sites,
        never earlier than now.  Returning `now` is the correct answer when
        something is already overdue.
        """
        now     = now or utcnow()
        soonest = None
        for site in self.cfg["websites"]:
            row = self.store.get_site(site["url"])
            due = (parse_iso(row["next_due"]) if row else now) or now
            if soonest is None or due < soonest:
                soonest = due
        return max(soonest or now, now)

    def check_site(self, site: dict) -> bool:
        """
        Check one site end to end.  Returns True only when a change was both
        detected and considered significant enough to report.

        Never raises: a site that cannot be fetched is recorded as an error and
        the pass carries on to the next one.
        """
        url, name = site["url"], site["name"]
        next_due  = utcnow() + timedelta(seconds=site["check_interval"])
        log.info("Checking %s", name)

        # --- fetch and reduce to text -----------------------------------------
        try:
            markup = fetch(url, self.cfg)
            lines  = extract_lines(markup, site["selector"])
        except Exception as exc:              # one bad site must not stop the pass
            message  = f"{type(exc).__name__}: {exc}"
            failures = self.store.record_error(url, name, message, next_due)
            log.error("%s failed (%d in a row): %s", name, failures, message)

            threshold = int(self.cfg.get("error_alert_after", 5))
            row       = self.store.get_site(url)
            if threshold and failures >= threshold and row and not row["error_alerted"]:
                if self.mailer.send_error(name, url, failures, message):
                    self.store.mark_error_alerted(url)
            return False

        # A page that yields no text at all is a failure dressed as a success -
        # usually a JS-rendered shell, or a block page.  Treating it as a change
        # would report the entire site as deleted.
        if not lines:
            log.warning("%s produced no text - treating as a failed fetch", name)
            self.store.record_error(url, name, "no visible text extracted", next_due)
            return False

        digest        = text_digest(lines)
        row           = self.store.get_site(url)
        previous_hash = row["text_hash"] if row else None
        retention     = int(self.cfg.get("snapshot_retention", 20))
        text          = "\n".join(lines)

        # --- first sighting: record a baseline, say nothing --------------------
        if previous_hash is None:
            self.store.record_success(url, name, text, digest, next_due, False, retention)
            log.info("  baseline stored (%d lines, %d chars)", len(lines), sum(map(len, lines)))
            return False

        # --- unchanged --------------------------------------------------------
        if digest == previous_hash:
            self.store.record_success(url, name, text, digest, next_due, False, retention)
            log.info("  no change")
            return False

        # --- changed: work out whether it is worth an email -------------------
        old_lines = decompress(row["body"]).split("\n") if row["body"] else []
        diff      = diff_lines(old_lines, lines, int(self.cfg.get("min_line_chars", 40)))

        # Either a decent net change in size, or at least two surviving lines.
        # The second clause catches a headline swap, which nets near zero
        # characters while being exactly what you wanted to hear about.
        significant = bool(diff.added or diff.removed) and (
            abs(diff.char_delta) >= int(self.cfg.get("min_change_chars", 120))
            or len(diff.added) + len(diff.removed) >= 2
        )

        self.store.record_success(url, name, text, digest, next_due, significant, retention)

        if not significant:
            log.info("  changed below threshold (%s) - baseline updated quietly", diff.summary())
            return False

        log.info("  CHANGED: %s", diff.summary())
        notified = self.mailer.send_change(name, url, diff, utcnow())
        self.store.log_change(url, diff.summary(), diff.char_delta, diff.added, diff.removed, notified)
        return True

    def run_once(self, force: bool = False) -> int:
        """One pass over the due sites (or all of them with force).  Returns the change count."""
        sites = self.cfg["websites"] if force else self.due_sites()
        if not sites:
            log.info("Nothing due yet; next check at %s", local_str(iso(self.next_wakeup())))
            return 0

        changed = sum(1 for site in sites if self.check_site(site))
        log.info("Pass complete: %d of %d site(s) changed", changed, len(sites))
        return changed

    def run_forever(self, stop: threading.Event) -> None:
        """
        The service's main loop.  Sleeps on the stop Event rather than time.sleep
        so a service stop is acted on immediately instead of after the interval.

        Any exception from a pass is logged and swallowed: an unattended monitor
        that dies on one bad night is worse than useless, because you would go on
        believing it was watching.
        """
        log.info("%s started (%d site(s), database %s)", APP_NAME, len(self.cfg["websites"]), DB_FILE)

        while not stop.is_set():
            try:
                self.run_once()
            except Exception:                 # the loop must survive anything
                log.exception("Unexpected error during check pass")

            wake    = self.next_wakeup()
            seconds = max(5.0, (wake - utcnow()).total_seconds())
            log.info("Sleeping until %s", local_str(iso(wake)))
            stop.wait(seconds)

        log.info("%s stopped", APP_NAME)


# ===========================================================================
#  Migration from the pre-2026 JSON state file
# ===========================================================================

def migrate_legacy(store: Store, cfg: dict, source: Path = LEGACY_DATA_FILE) -> tuple[int, int]:
    """
    Import baselines from an old website_data.json, so the first run after an
    upgrade does not report every page as brand new.  Returns (imported, skipped).

    Only worth running when the old file is recent.  If it is months stale the
    honest move is to skip this entirely and let the first check lay down fresh
    baselines - the resulting diff would be enormous and tell you nothing.
    """
    if not source.exists():
        return (0, 0)

    data     = json.loads(source.read_text(encoding="utf-8"))
    wanted   = {s["url"]: s for s in cfg["websites"]}
    imported = skipped = 0

    for url, entry in data.items():
        site = wanted.get(url)
        if site is None:
            log.info("Skipping %s (not in the current config)", url)
            skipped += 1
            continue

        existing = store.get_site(url)
        if existing and existing["text_hash"]:
            log.info("Skipping %s (already has a baseline)", url)
            skipped += 1
            continue

        # Prefer re-deriving the text from the stored HTML, so the baseline is
        # what THIS extractor would have produced.  Fall back to the old
        # clean_text only when no HTML was kept.
        lines: list[str] = []
        if entry.get("full_content"):
            lines = extract_lines(entry["full_content"], site["selector"])
        if not lines and entry.get("clean_text"):
            lines = [ln for ln in (l.strip() for l in entry["clean_text"].split(". ")) if ln]
        if not lines:
            log.warning("No usable baseline for %s", url)
            skipped += 1
            continue

        store.record_success(url, site["name"], "\n".join(lines), text_digest(lines),
                             utcnow(), False, int(cfg.get("snapshot_retention", 20)))
        log.info("Imported baseline for %s (%d lines)", site["name"], len(lines))
        imported += 1

    return (imported, skipped)


# ===========================================================================
#  Logging and console
# ===========================================================================

def use_utf8_console() -> None:
    """
    Force stdout/stderr to UTF-8.

    Web pages are full of curly quotes, dashes and accents, and the default
    cp1252 console raises UnicodeEncodeError on all of them - which would
    otherwise crash `history` half way through printing a headline.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass                              # no console (service), or not reconfigurable


def setup_logging(level: str = "INFO", console: bool = True) -> None:
    """
    Configure logging: always to a rotating file, optionally to the console.

    The service passes console=False - it has no stdout, and the log file is the
    only place its progress is ever visible.  5 x 1 MB is a few months of normal
    operation.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    log.handlers.clear()                      # re-runnable, e.g. under test

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(stream)


# ===========================================================================
#  Windows service
# ===========================================================================
#
# pywin32 is imported lazily throughout this section, so every other command
# works on a machine that has never had it installed.

_SERVICE_CLASS = None                         # cached; see build_service_class


def build_service_class():
    """
    Define (once) and return the ServiceFramework subclass.

    It is built lazily so pywin32 stays optional, and cached so the class has a
    stable identity - pywin32 registers the service as
    "webwatcher.WebWatcherService" and pythonservice.exe resolves that by
    importing this module and doing a getattr.  A class defined only inside a
    function would never be found at boot, which is what the module-level
    __getattr__ below exists to prevent.
    """
    global _SERVICE_CLASS
    if _SERVICE_CLASS is not None:
        return _SERVICE_CLASS

    import win32event
    import win32service
    import win32serviceutil

    class WebWatcherService(win32serviceutil.ServiceFramework):
        _svc_name_         = SERVICE_NAME
        _svc_display_name_ = f"{APP_NAME} website change monitor"
        _svc_description_  = ("Polls configured web pages and emails a diff when their "
                              "visible text changes.")

        def __init__(self, args):
            super().__init__(args)
            self.stop_event  = threading.Event()
            self.wait_handle = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):                    # noqa: N802 - name fixed by the framework
            """Called by the SCM on another thread; sets the flag the loop waits on."""
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_event.set()
            win32event.SetEvent(self.wait_handle)

        def SvcDoRun(self):                   # noqa: N802 - name fixed by the framework
            """The service body.  Everything it does lands in the log file."""
            store = None
            try:
                cfg = load_config(find_config(None))
                setup_logging(cfg.get("log_level", "INFO"), console=False)
                store = Store()
                Monitor(cfg, store).run_forever(self.stop_event)
            except Exception:                 # must reach the log, there is no console
                setup_logging("INFO", console=False)
                log.exception("Service terminated with an error")
                raise
            finally:
                if store is not None:
                    store.close()

    _SERVICE_CLASS = WebWatcherService
    return _SERVICE_CLASS


def __getattr__(name: str):
    """
    PEP 562 module hook, so `webwatcher.WebWatcherService` resolves on demand.

    This is what lets pythonservice.exe find the class at boot without this
    module importing pywin32 at the top.  ImportError is converted to
    AttributeError so that anything walking module attributes - pickle's
    whichmodule, for one - degrades quietly on a machine without pywin32.
    """
    if name == "WebWatcherService":
        try:
            return build_service_class()
        except ImportError as exc:
            raise AttributeError(name) from exc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# pywin32 parses its own command line with getopt, which stops at the first
# non-option argument.  "install --startup delayed" therefore leaves three
# positionals, and it prints its usage banner and exits without saying why.
# Options have to come first, so accept either order and rearrange.
_SERVICE_VALUE_OPTS = {"--username", "--password", "--startup",
                       "--perfmonini", "--perfmondll", "--wait"}
_SERVICE_FLAG_OPTS  = {"--interactive"}


def reorder_service_args(argv: Sequence[str]) -> list[str]:
    """Move any option flags (and their values) ahead of the command word."""
    options: list[str] = []
    rest:    list[str] = []
    i = 0

    while i < len(argv):
        token = argv[i]
        if token in _SERVICE_VALUE_OPTS and i + 1 < len(argv):
            options.extend(argv[i:i + 2])     # "--startup delayed"
            i += 2
        elif token in _SERVICE_FLAG_OPTS or (token.startswith("--") and "=" in token):
            options.append(token)             # "--interactive", "--startup=delayed"
            i += 1
        else:
            rest.append(token)
            i += 1

    return options + rest


def service_command(argv: list[str]) -> int:
    """Hand a service subcommand to pywin32, or explain how to install pywin32."""
    try:
        import win32serviceutil
    except ImportError:
        exe = sys.executable
        print("pywin32 is required for service management.\n"
              "Install it from an elevated prompt:\n"
              f'    "{exe}" -m pip install pywin32\n'
              f'    "{exe}" "{Path(exe).parent / "Scripts" / "pywin32_postinstall.py"}" -install',
              file=sys.stderr)
        return 2

    cls = build_service_class()
    win32serviceutil.HandleCommandLine(cls, argv=[sys.argv[0], *reorder_service_args(argv)])
    return 0


# ===========================================================================
#  CLI commands
# ===========================================================================
#
# Every command has the same signature - (args, cfg, store) - so main() can
# dispatch to them uniformly through argparse's set_defaults(func=...).

def cmd_check(args, cfg: dict, store: Store) -> int:
    """One pass over the sites that are due, or all of them with --force."""
    Monitor(cfg, store).run_once(force=args.force)
    return 0


def cmd_run(args, cfg: dict, store: Store) -> int:
    """Run the monitor loop in the foreground until Ctrl+C."""
    stop = threading.Event()
    try:
        Monitor(cfg, store).run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
        print("\nStopped.")
    return 0


def cmd_status(args, cfg: dict, store: Store) -> int:
    """Show where everything lives, then a row per site and the recent changes."""
    monitor = Monitor(cfg, store, ensure=False)   # reporting only - must not write
    db_size = DB_FILE.stat().st_size if DB_FILE.exists() else 0

    print(f"Config:   {cfg['_path']}")
    print(f"Database: {DB_FILE} ({db_size / 1024:.1f} KB)"
          + ("  [opened read-only]" if store.readonly else ""))
    print(f"Log:      {LOG_FILE}")
    secret = ("environment variable" if os.environ.get(PASSWORD_ENV_VAR)
              else "encrypted file" if SECRET_FILE.exists()
              else "PLAINTEXT in config.json")
    print(f"Password: {secret}")
    print()

    header = f"{'Site':<22} {'Last check':<20} {'Last change':<20} {'Next due':<20} {'Chars':>8}  State"
    print(header)
    print("-" * len(header))

    for site in cfg["websites"]:
        row = store.get_site(site["url"])
        if row is None:
            print(f"{site['name'][:22]:<22} {'never':<20} {'-':<20} {'now':<20} {0:>8}  new")
            continue
        state = ("FAILING x{}".format(row["consecutive_errors"]) if row["consecutive_errors"]
                 else "ok" if row["text_hash"] else "new")
        print(f"{row['name'][:22]:<22} {local_str(row['last_check']):<20} "
              f"{local_str(row['last_change']):<20} {local_str(row['next_due']):<20} "
              f"{row['char_count']:>8}  {state}")

    print()
    print(f"Next wakeup: {local_str(iso(monitor.next_wakeup()))}")

    changes = store.recent_changes(limit=5)
    if changes:
        print("\nRecent changes:")
        for change in changes:
            flag = "" if change["notified"] else "  (not emailed)"
            print(f"  {local_str(change['detected_at'])}  {change['summary']}{flag}")
    return 0


def cmd_history(args, cfg: dict, store: Store) -> int:
    """
    Show recent changes with their added and removed lines.

    The site argument is matched case-insensitively against name and URL, as a
    substring, so `history bbc` and `history bbc.co.uk` both work.  With no
    argument, shows changes across every site.
    """
    url   = None
    needle = (args.site or "").strip().lower()
    if needle:
        for site in cfg["websites"]:
            if needle in site["name"].lower() or needle in site["url"].lower():
                url = site["url"]
                break
        if url is None:
            print(f"No configured site matches {args.site!r}; showing all sites.")

    changes = store.recent_changes(url, args.limit)
    if not changes:
        print("No changes recorded yet.")
        return 0

    for change in changes:
        print(f"\n{local_str(change['detected_at'])} - {change['summary']}")
        for line in json.loads(change["added"] or "[]")[:5]:
            print(f"  + {line[:150]}")
        for line in json.loads(change["removed"] or "[]")[:5]:
            print(f"  - {line[:150]}")
    return 0


def cmd_migrate(args, cfg: dict, store: Store) -> int:
    """Import baselines from an old website_data.json and drop unconfigured sites."""
    imported, skipped = migrate_legacy(store, cfg)
    print(f"Imported {imported} baseline(s), skipped {skipped}.")

    removed = store.prune_orphans(s["url"] for s in cfg["websites"])
    if removed:
        print(f"Removed {removed} site(s) no longer in the config.")
    return 0


def cmd_deploy(args, cfg: dict, store: Store) -> int:
    """
    Copy the script and config into DATA_DIR, ready to install as a service.

    Never run a service from OneDrive: a service account cannot reliably hydrate
    Files On-Demand placeholders, so the script may simply be missing when
    Windows tries to start it at boot.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    target = DATA_DIR / source.name

    if target == source:
        print(f"Already running from {DATA_DIR}; nothing to copy.")
    else:
        shutil.copy2(source, target)
        print(f"Copied {source.name} -> {target}")

    config_target = DATA_DIR / "config.json"
    config_source: Path = cfg["_path"]
    if config_source.resolve() != config_target.resolve():
        if config_target.exists() and not args.overwrite:
            print(f"Kept the existing {config_target} (use --overwrite to replace it)")
        else:
            shutil.copy2(config_source, config_target)
            print(f"Copied config -> {config_target}")

    print("\nNext steps (from an elevated prompt):")
    print(f'    "{sys.executable}" "{target}" set-password')
    print(f'    "{sys.executable}" "{target}" service install --startup delayed')
    print(f'    "{sys.executable}" "{target}" service start')
    return 0


def cmd_set_password(args, cfg: dict, store: Store) -> int:
    """
    Prompt for the SMTP password, store it encrypted, and strip any plaintext
    copy out of config.json (keeping a .bak).

    Needs write access to SECRET_FILE - if that file was created by an elevated
    process, this must be run elevated too.
    """
    password = getpass.getpass("SMTP password: ")
    if not password:
        print("Nothing entered; aborted.")
        return 1
    if password != getpass.getpass("Confirm: "):
        print("Passwords did not match.")
        return 1

    path = store_password(password)
    print(f"Stored, encrypted with DPAPI, at {path}")

    config_path: Path = cfg["_path"]
    raw     = json.loads(config_path.read_text(encoding="utf-8"))
    touched = False
    for section in (raw.get("email"), raw.get("notifications", {}).get("email")):
        if isinstance(section, dict) and section.pop("sender_password", None) is not None:
            touched = True

    if touched:
        backup = config_path.with_suffix(".json.bak")
        shutil.copy2(config_path, backup)
        config_path.write_text(json.dumps(raw, indent=4), encoding="utf-8")
        print(f"Removed the plaintext password from {config_path} (backup: {backup})")
        print("The old password is still in that backup, and in your OneDrive version")
        print("history - rotate it at the mail server if that matters to you.")
    return 0


def cmd_test_email(args, cfg: dict, store: Store) -> int:
    """Send a test message, reporting the SMTP error verbatim if it fails."""
    try:
        Mailer(cfg).send_test()
    except Exception as exc:                  # the whole point of this command
        print(f"Failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"Test message sent to {cfg['email']['recipient_email']}.")
    return 0


# ===========================================================================
#  Entry point
# ===========================================================================

COMMANDS = {"check", "run", "status", "history", "migrate", "deploy",
            "set-password", "test-email", "service"}


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser.  Each subcommand binds its cmd_* function."""
    parser = argparse.ArgumentParser(prog="webwatcher",
                                     description=f"{APP_NAME} website change monitor")
    parser.add_argument("--config",    help="path to config.json")
    parser.add_argument("--log-level", help="override the configured log level")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="run one pass over sites that are due")
    check.add_argument("--force", action="store_true", help="check every site regardless of schedule")
    check.set_defaults(func=cmd_check)

    sub.add_parser("run", help="run continuously in the foreground").set_defaults(func=cmd_run)

    # readonly=True: these two only report, so they open the database read-only
    # and keep working even when the service owns the files.
    sub.add_parser("status", help="show what is watched and when").set_defaults(
        func=cmd_status, readonly=True)

    history = sub.add_parser("history", help="show recent changes")
    history.add_argument("site", nargs="?", default="", help="site name or URL (substring match)")
    history.add_argument("--limit", type=int, default=10)
    history.set_defaults(func=cmd_history, readonly=True)

    sub.add_parser("migrate",      help="import baselines from website_data.json").set_defaults(func=cmd_migrate)
    sub.add_parser("set-password", help="store the SMTP password encrypted").set_defaults(func=cmd_set_password)
    sub.add_parser("test-email",   help="send a test message").set_defaults(func=cmd_test_email)

    deploy = sub.add_parser("deploy", help="copy script and config to the data directory")
    deploy.add_argument("--overwrite", action="store_true", help="replace an existing config there")
    deploy.set_defaults(func=cmd_deploy)

    service = sub.add_parser(
        "service",
        help="manage the Windows service (install | start | stop | restart | remove | update)",
    )
    service.add_argument("args", nargs=argparse.REMAINDER)
    service.set_defaults(func=None)           # handled before the config is loaded

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line and dispatch.

    `service` is handled before any config is loaded, because installing or
    removing the service must work even when config.json is missing or broken.
    """
    use_utf8_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Back-compat with the flags the two original scripts took.
    argv = ["check" if a == "--once" else "run" if a == "--continuous" else a for a in argv]
    if not any(a in COMMANDS for a in argv):
        argv.append("check")                  # bare invocation means "check now"

    args = build_parser().parse_args(argv)

    if args.command == "service":
        return service_command(args.args)

    try:
        cfg = load_config(find_config(args.config))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Config is not valid JSON: {exc}", file=sys.stderr)
        return 1

    setup_logging(args.log_level or cfg.get("log_level", "INFO"), console=True)

    store = Store(readonly=getattr(args, "readonly", False))
    try:
        return args.func(args, cfg, store)
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc):
            raise
        print(f"\nCannot write {DB_FILE}: {exc}.\n"
              f"The service account owns these files. Either run this command from an "
              f"elevated prompt, or grant your account access once, elevated:\n"
              f'    icacls "{DATA_DIR}" /grant "Users:(OI)(CI)M" /T',
              file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
