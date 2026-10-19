# WebWatcher

Watches web pages and emails you a readable diff when their visible text
changes. One script, `webwatcher.py`, which runs as a one-shot check, a
foreground loop, or a Windows service that starts itself at boot.

**Installing it?** See **[INSTALL.md](INSTALL.md)** — the full process, with
troubleshooting for every failure met in practice. This file is the reference.

---

## What is in this folder

This is the **master copy**. The running installation lives elsewhere — see
[Deployment layout](#deployment-layout).

| File | Keep? | |
|---|---|---|
| `webwatcher.py` | **yes** | The entire application. Nothing else is imported. |
| `config.json` | **yes** | Reference copy of the settings. Not the live one. |
| `requirements.txt` | **yes** | Dependencies, for reinstalling elsewhere. |
| `INSTALL.md` | **yes** | Installation and troubleshooting. |
| `README.md` | **yes** | This file. |

Deleted as no longer needed: `website_monitor.py` and `website_monitor1.py`
(the two divergent predecessors), `email_test.py` (superseded by `test-email`,
and it held a plaintext password), `website_data.json` (the old JSON state
file, replaced by SQLite), and any `__pycache__`.

> The `config.json` here is a **reference copy**. The file the service actually
> reads sits next to the installed script. Editing this one changes nothing —
> see [Which config is live](#which-config-is-live).

---

## Deployment layout

| | Path |
|---|---|
| Master copy | `C:\Users\pm\OneDrive\Projects\WebWatcher` |
| Installed script + live config | `C:\utils\webwatcher` |
| Database, log, encrypted password | `C:\ProgramData\WebWatcher` |

State is deliberately kept out of OneDrive: a service running as LocalSystem
cannot hydrate Files On-Demand placeholders, and a database rewritten every few
minutes inside a synced folder causes constant upload churn.

### Which config is live

Looked up in this order:

1. `--config` on the command line
2. `$WEBWATCHER_CONFIG`
3. `config.json` **beside the script** ← normal case
4. `config.json` in the data directory

Config is configuration, not state, so the copy next to the script wins. That
ordering is deliberate: it stops a forgotten file in the data directory from
silently shadowing the one you are editing. `status` always prints which file
was actually loaded — check it after any edit.

---

## Commands

| Command | |
|---|---|
| `check` | One pass over the sites now due. `--force` does all of them. |
| `run` | Foreground loop until Ctrl+C. |
| `status` | Where everything lives, plus a row per site. |
| `history [site]` | Recent changes with their added and removed lines. |
| `deploy` | Copy script and config into the data directory. |
| `set-password` | Prompt for the SMTP password, store it encrypted. |
| `test-email` | Send a test message. |
| `migrate` | Import baselines from a legacy `website_data.json`. |
| `service ...` | `install`, `start`, `stop`, `restart`, `remove`, `update`. |

`--once` and `--continuous` still work as aliases for `check` and `run`. A bare
invocation with no command runs `check`.

---

## How change detection works

Hashing raw HTML fires constantly on any real site — adverts rotate, session
tokens change, markup churns. WebWatcher discards all of that first:

```
1. fetch the page                 3 attempts, 2s then 4s backoff
2. strip the noise                script, style, form, svg, iframe, ...
3. strip the chrome               nav / header / footer / aside
4. strip dynamic blocks           class or id token matching advert,
                                  cookie, session, csrf, promo, ...
5. reduce to visible text lines   collapse whitespace, drop repeats
6. SHA-256 those lines            different hash => something moved
7. difflib the two line lists     exact added / removed lines
8. filter and threshold           decides whether you get an email
```

Step 8 applies two filters that do the real work of keeping mail readable: a
line present on **both** sides was merely moved rather than added, so it is
dropped; and lines shorter than `min_line_chars` are dropped as furniture.

A change is emailed when something survives that filtering **and** either the
net size change clears `min_change_chars`, or at least two lines changed. The
second clause is what catches a headline swap, which nets near zero characters
while being exactly what you wanted to hear about.

Anything below the threshold updates the baseline quietly, so it is reported
once at most, never repeatedly.

**Nothing is ever lost to a threshold or a mail failure.** Every change that
reaches step 7 is written to the change log with a flag recording whether the
email went out. `history` shows them either way.

---

## Configuration

See [INSTALL.md § 5](INSTALL.md#5-write-configjson) for the full field
reference. In brief:

```json
{
    "websites": [
        {"url": "https://example.com/news", "name": "Example", "check_interval": 3600, "selector": null}
    ],
    "email": {
        "enabled": true,
        "smtp_server": "smtp.example.com",
        "smtp_port": 465,
        "security": "ssl",
        "sender_email": "monitor@example.com",
        "recipient_email": "you@example.com"
    },
    "check_interval": 3600,
    "min_change_chars": 120,
    "min_line_chars": 40,
    "max_reported_lines": 12,
    "snapshot_retention": 20,
    "error_alert_after": 5,
    "log_level": "INFO"
}
```

Per-site `check_interval` is honoured — the original scripts documented that
and then ignored it. `"enabled": false` parks a site without deleting it.

Config is read **once at service start**, so edits need
`webwatcher.py service restart`. The password is the exception: it is re-read
on every send.

Two keys sometimes found in older files, `ignore_dynamic_content` and
`similarity_threshold`, are leftovers from the predecessors. Nothing reads
them; delete them or ignore them.

---

## Storage

SQLite, holding **compressed visible text only** — never the full HTML the
predecessors stored.

| Table | |
|---|---|
| `sites` | Current text, hash, schedule, failure counters. |
| `snapshots` | Last N versions per site (default 20), pruned on write. |
| `changes` | Every detected change, and whether it was emailed. |

For five sites that is roughly 100 KB, against ~935 KB for the old
`website_data.json` covering three — and unlike that file it is bounded, since
snapshots are pruned on every write.

Once the service has run, the database files belong to the service account.
`status` and `history` open it **read-only** so they keep working from an
ordinary prompt; commands that write need elevation, or a one-off `icacls`
grant — see [INSTALL.md](INSTALL.md#attempt-to-write-a-readonly-database).

Inspect it with any SQLite browser, or:

```
"C:\Program Files\Python314\python.exe" -c "import sqlite3;print(sqlite3.connect(r'C:\ProgramData\WebWatcher\webwatcher.db').execute('select name,char_count,last_change from sites').fetchall())"
```

---

## The mail password

Resolved at send time, in order:

1. `WEBWATCHER_SMTP_PASSWORD` environment variable
2. `C:\ProgramData\WebWatcher\smtp_password.bin`, encrypted with Windows DPAPI
3. `sender_password` in `config.json` — works, but warns every time

`set-password` writes the encrypted file and strips any plaintext value out of
`config.json`, leaving a `.bak` that **still contains it** — delete that once
you are satisfied.

DPAPI is used at *machine* scope so the service can decrypt it whatever account
it runs under, which also means any administrator on this PC can decrypt it. It
protects against the password travelling inside a copied config file, not
against a local admin.

---

## Known limitations

**JavaScript-rendered pages.** `requests` fetches HTML; it does not run a
browser. A page that builds its content client-side yields no text, and
WebWatcher treats that as a failed fetch rather than pretending the site
emptied. Use a `selector` on a server-rendered region, watch a different URL,
or use a feed.

**Bot-protected sites.** Pages behind a Cloudflare challenge return 403 with a
`Just a moment...` body to any plain HTTP client, regardless of user agent.
Working around that is not supported. Disable the site or check it by hand.
As of July 2026 both `setu.ie` and the Oxford Encaenia page are in this
category.

**Busy pages.** News homepages genuinely change every few minutes, so they will
notify almost every interval. Raise the thresholds or narrow with a `selector`.

---

## Requirements

- Windows, Python 3.9+ (developed against 3.14)
- `requests`, `beautifulsoup4`, `lxml`
- `pywin32` — only for the service; every other command works without it

Installed **system-wide and elevated**, with `--ignore-installed`. A service
cannot see per-user site-packages, and that flag is what stops transitive
dependencies being left behind in your user profile. This is the single most
common installation failure — [INSTALL.md § 2](INSTALL.md#2-install-the-python-dependencies)
explains it fully.
