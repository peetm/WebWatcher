# Installing WebWatcher as a Windows service

Everything needed to go from a bare copy of `webwatcher.py` to a service that
starts itself at boot. Written for Windows 11 with Python 3.14 at
`C:\Program Files\Python314`; adjust the paths if yours differ.

Allow about fifteen minutes. Several steps need an **elevated** Command Prompt
(Start → type `cmd` → right-click → *Run as administrator*) and the guide says
so each time. The [Troubleshooting](#troubleshooting) section at the end covers
every failure actually encountered doing this for real — if something goes
wrong, the answer is almost certainly there.

---

## Contents

1. [Decide where things will live](#1-decide-where-things-will-live)
2. [Install the Python dependencies](#2-install-the-python-dependencies) — *elevated*
3. [Install pywin32 and its host executable](#3-install-pywin32-and-its-host-executable) — *elevated*
4. [Put the files in place](#4-put-the-files-in-place)
5. [Write config.json](#5-write-configjson)
6. [Store the mail password](#6-store-the-mail-password) — *elevated*
7. [Test it by hand](#7-test-it-by-hand)
8. [Install and start the service](#8-install-and-start-the-service) — *elevated*
9. [Verify it is really running](#9-verify-it-is-really-running)
10. [Day-to-day operation](#10-day-to-day-operation)
11. [Troubleshooting](#troubleshooting)
12. [Uninstalling](#uninstalling)

---

## 1. Decide where things will live

Two directories, deliberately separate:

| | Path | Why |
|---|---|---|
| Script + config | `C:\utils\webwatcher` | Any local folder. **Not** OneDrive. |
| Database, log, password | `C:\ProgramData\WebWatcher` | Created automatically. |

**Never run the service from OneDrive.** Two independent reasons:

- A service running as LocalSystem has no user profile and cannot hydrate
  OneDrive Files On-Demand placeholders. The script can appear simply missing
  when Windows tries to start it at boot.
- A database rewritten every few minutes inside a synced folder generates
  endless upload churn.

Keep your master copy in OneDrive by all means — just deploy a copy to a local
folder and run the service from there.

All three locations can be overridden with environment variables if you need
something different:

| Variable | Overrides |
|---|---|
| `WEBWATCHER_HOME` | where the script considers itself to live |
| `WEBWATCHER_DATA` | database, log, encrypted password |
| `WEBWATCHER_CONFIG` | the config file path |

---

## 2. Install the Python dependencies

**This must be elevated, and it must use `--ignore-installed`.** This is the
single most likely thing to go wrong.

```
"C:\Program Files\Python314\python.exe" -m pip install --ignore-installed requests beautifulsoup4 lxml
```

### Why `--ignore-installed` matters

A service runs as LocalSystem, which has **no user profile** and therefore
cannot see per-user site-packages at
`C:\Users\<you>\AppData\Roaming\Python\Python314\site-packages`. Every
dependency must be in the system location,
`C:\Program Files\Python314\Lib\site-packages`.

The trap: if you have already installed these as your own user, a plain
elevated `pip install requests` moves `requests` system-wide but leaves
`urllib3`, `certifi`, `idna` and `charset_normalizer` where they were — pip
sees them as "already satisfied" from your user folder and skips them. The
service then fails one import deeper than before. `--ignore-installed` tells
pip to stop consulting your user profile and install the complete tree.

### Verify

This simulates exactly what the service sees — `-s` disables user
site-packages:

```
"C:\Program Files\Python314\python.exe" -s -c "import requests, bs4, lxml, certifi, urllib3, idna, charset_normalizer, soupsieve; print('all system-wide OK')"
```

If that prints `all system-wide OK`, this step is done. Any `ModuleNotFoundError`
means something is still only in your user profile — re-run the install command
above and check it was elevated.

---

## 3. Install pywin32 and its host executable

Elevated:

```
"C:\Program Files\Python314\python.exe" -m pip install pywin32
```

Then the post-install step, which is **not optional** — it moves
`pythonservice.exe` (the process Windows actually launches) out of
site-packages and into the Python root, and registers the required DLLs:

```
"C:\Program Files\Python314\python.exe" "C:\Program Files\Python314\Scripts\pywin32_postinstall.py" -install
```

### Verify

```
dir "C:\Program Files\Python314\pythonservice.exe"
```

That file must exist. If it is missing, the post-install did not run or was not
elevated, and step 8 will fail with `Can't find 'C:\Program Files\Python314\pythonservice.exe'`.

---

## 4. Put the files in place

Create the folder and copy in `webwatcher.py`:

```
mkdir C:\utils\webwatcher
copy webwatcher.py C:\utils\webwatcher\
```

Alternatively, from wherever your master copy lives, let the script place
itself and its config into the data directory:

```
"C:\Program Files\Python314\python.exe" webwatcher.py deploy
```

`deploy` copies into `C:\ProgramData\WebWatcher` and prints the exact commands
for the remaining steps. Note that if you run it elevated, the copied files end
up owned by Administrators, and every later `deploy` will need elevation too.

---

## 5. Write config.json

Create `C:\utils\webwatcher\config.json`. Minimum viable version:

```json
{
    "websites": [
        {
            "url": "https://www.bbc.co.uk/news",
            "name": "BBC",
            "check_interval": 3600,
            "selector": null
        }
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

Run the script with no config present and it writes a starter file for you,
then exits so you can edit it.

### Per-site fields

| Field | Required | Meaning |
|---|---|---|
| `url` | yes | The page to watch. |
| `name` | no | Shown in the email subject. Defaults to the URL. |
| `check_interval` | no | Seconds between checks. Inherits the global value. |
| `selector` | no | CSS selector to watch part of a page. `null` = whole page. |
| `enabled` | no | `false` parks a site without deleting it. |

### Global settings

| Key | Default | Meaning |
|---|---|---|
| `check_interval` | 3600 | Default seconds between checks. |
| `request_timeout` | 30 | Seconds before a fetch is abandoned. |
| `user_agent` | Chrome 131 | Sent with every request. |
| `min_change_chars` | 120 | Net character change below which nothing is emailed. |
| `min_line_chars` | 40 | Lines shorter than this are ignored when diffing. |
| `max_reported_lines` | 12 | Cap on lines quoted in one email. |
| `snapshot_retention` | 20 | Versions kept per site. |
| `error_alert_after` | 5 | Consecutive failures before one alert. `0` disables. |
| `log_level` | `INFO` | `DEBUG` for much more detail. |

### Email security modes

| `security` | Port | Notes |
|---|---|---|
| `ssl` | 465 | Encrypted from the first byte. The usual choice. |
| `starttls` | 587 | Plain connection upgraded to TLS. |
| `none` | 25 | Unencrypted. Avoid. |

The older `"use_tls": true/false` key is still understood, and the older layout
that nested email settings under `"notifications"` still loads.

---

## 6. Store the mail password

Never leave the password in `config.json`. Elevated:

```
cd /d C:\utils\webwatcher
"C:\Program Files\Python314\python.exe" webwatcher.py set-password
```

You are prompted twice; nothing echoes. The password is encrypted with Windows
DPAPI to `C:\ProgramData\WebWatcher\smtp_password.bin`, and any plaintext
`sender_password` is stripped out of `config.json` — with a `.bak` left behind
that **still contains it**, so delete that once you are happy.

The password is looked up in this order at send time:

1. `WEBWATCHER_SMTP_PASSWORD` environment variable
2. the encrypted `smtp_password.bin`
3. `sender_password` in `config.json` (works, but warns every time)

It is read fresh on **every send**, so replacing it later needs no service
restart.

### A note on DPAPI scope

Encryption uses *machine* scope, not user scope, so the service can decrypt it
whatever account it runs under. The trade-off is explicit: any administrator on
this PC can decrypt it too. It protects against the password travelling inside
a copied config file — not against a local admin.

Because `set-password` writes into `C:\ProgramData`, if that file was first
created by an elevated process then later runs must also be elevated.

---

## 7. Test it by hand

Before involving the service, prove the plumbing works. These do not need
elevation.

```
cd /d C:\utils\webwatcher
"C:\Program Files\Python314\python.exe" webwatcher.py test-email
```

Expect `Test message sent to you@example.com.` — and check it arrives.

Then lay down the initial baselines:

```
"C:\Program Files\Python314\python.exe" webwatcher.py check --force
```

Every site should report `baseline stored (N lines, M chars)`. **No email is
sent on a first check** — there is nothing to compare against yet. Changes are
reported from the second check onwards.

```
"C:\Program Files\Python314\python.exe" webwatcher.py status
```

Confirms which config was loaded, where the database is, how the password is
being supplied, and the state of every site.

---

## 8. Install and start the service

Elevated, **from the directory holding the script** — the install path is
recorded in the registry at install time:

```
cd /d C:\utils\webwatcher
"C:\Program Files\Python314\python.exe" webwatcher.py service install --startup delayed
```

Expected:

```
Installing service WebWatcher
Service installed
```

`--startup delayed` means *Automatic (Delayed Start)*: it comes up at boot but
after the network is ready, so the first check does not fail. Use `auto` for
immediate start, `manual` to start it yourself.

> **Argument order.** pywin32 parses its command line with `getopt`, which stops
> at the first non-option word, and it wants options *before* the command
> (`--startup delayed install`). Given them the other way round it prints a bare
> usage banner and exits without explanation. `webwatcher.py` reorders the
> arguments for you, so either order works here — but remember this if you ever
> drive `win32serviceutil` directly.

Then start it:

```
"C:\Program Files\Python314\python.exe" webwatcher.py service start
```

---

## 9. Verify it is really running

`Starting service WebWatcher` is printed optimistically, before Windows
confirms anything. A service that dies on startup prints exactly the same
thing, so always check:

```
sc query WebWatcher
```

You want `STATE : 4  RUNNING`. Anything else, especially
`WIN32_EXIT_CODE : 1066`, means it started and immediately died — go to
[Troubleshooting](#troubleshooting).

Then read the log, which is where the service says everything it has to say:

```
type C:\ProgramData\WebWatcher\webwatcher.log
```

A healthy start looks like:

```
17:00:28 INFO    WebWatcher started (5 site(s), database C:\ProgramData\WebWatcher\webwatcher.db)
17:00:28 INFO    Checking BBC
17:00:29 INFO      CHANGED: 15 line(s) added; 15 line(s) removed; -207 characters; 81.2% similar
17:00:52 INFO    Pass complete: 2 of 5 site(s) changed
17:00:52 INFO    Sleeping until 2026-07-29 17:30:35
```

It also appears in `services.msc` as **WebWatcher website change monitor**.

---

## 10. Day-to-day operation

### Adding a site

Edit the `websites` array in `C:\utils\webwatcher\config.json`, then restart —
config is read once, at service start:

```
"C:\Program Files\Python314\python.exe" webwatcher.py service restart
```

A new site gets a silent baseline on its first check and reports from the
second onwards.

### Changing the password

No restart needed — it is re-read from disk on every send.

### Updating the script

Copy the new `webwatcher.py` over the installed one and restart the service.
Only run `service update` if the service *name* or *display name* changed.

### Useful commands

| Command | |
|---|---|
| `status` | Sites, schedule, failures, where everything lives. |
| `history` | Recent changes with the actual added/removed lines. |
| `history BBC` | Just that site (substring match on name or URL). |
| `check` | One pass over what is due, in the foreground. |
| `check --force` | Every site, ignoring the schedule. |
| `run` | Foreground loop, Ctrl+C to stop — handy for watching it work. |

`history` is worth knowing about: if mail is broken, changes are still detected
and logged with a `(not emailed)` marker, so nothing is lost.

---

## Troubleshooting

### "This app can't run on your PC"

A Windows dialog, usually meaning the executable is corrupt or the wrong
architecture. Check the interpreter itself is intact:

```
dir "C:\Program Files\Python314\python.exe"
```

If it is **0 bytes**, that is the problem. Repair it: Settings → Apps →
Installed apps → *Python 3.14.x (64-bit)* → **⋯** → Modify → **Repair**. Then
confirm:

```
"C:\Program Files\Python314\python.exe" -V
```

### pywin32 prints its usage banner and does nothing

```
Usage: 'webwatcher.py [options] install|update|remove|start [...]'
```

Options were given after the command. Put them first:

```
"C:\Program Files\Python314\python.exe" webwatcher.py service --startup delayed install
```

Current versions of `webwatcher.py` reorder arguments automatically, so this
only bites an older copy.

### `Can't find 'C:\Program Files\Python314\pythonservice.exe'`

The pywin32 post-install never ran, or ran unelevated. Redo step 3 from an
elevated prompt.

### Service starts then immediately stops (exit code 1066)

It crashed during startup. The reason is in the Windows Event Log, not the
WebWatcher log — nothing gets that far:

```
powershell "Get-EventLog -LogName Application -Newest 20 | Where-Object { $_.Source -match 'Python Service' } | Select-Object -First 1 -ExpandProperty Message"
```

By far the most common cause is:

```
Python could not import the service's module
ModuleNotFoundError: No module named 'requests'
```

That is the user-versus-system site-packages problem. Redo step 2, including
`--ignore-installed`, and re-run the `-s` verification.

Other possibilities: the script is on a path the service account cannot read
(OneDrive), or `config.json` has a JSON syntax error — run `status` by hand,
which reports the exact line and column.

### `403 Client Error: Forbidden`

The site is behind a bot challenge. If the response body contains
`Just a moment...` and the server header says `cloudflare`, the page is gated
behind a JavaScript challenge that no plain HTTP client can pass, whatever user
agent it sends. **Working around it is not supported.**

Options: set `"enabled": false` on that site, watch a different page on the same
site that is not gated, use an RSS feed if one exists, or check it manually.
WebWatcher records the failures and sends one alert after `error_alert_after`
tries, then stays quiet until it recovers.

### `535 Incorrect authentication data`

The mail server rejected the credentials. Check the mailbox actually exists and
that `sender_email` is the full login address the server expects. Test the
credential without storing it:

```
set WEBWATCHER_SMTP_PASSWORD=thepassword
"C:\Program Files\Python314\python.exe" webwatcher.py test-email
```

If that works, re-run `set-password` — the stored copy is wrong. Note that
repeated failed logins can trip a lockout on some servers, so do not sit there
retrying.

### `attempt to write a readonly database`

Expected, and mostly harmless. Once the service has run, the database and its
`-wal`/`-shm` companions belong to the service account, and your own account can
no longer write them.

`status` and `history` open the database read-only and keep working regardless.
Anything that writes — `check`, `migrate`, `set-password` — needs either an
elevated prompt, or a one-off permission grant, elevated:

```
icacls "C:\ProgramData\WebWatcher" /grant "Users:(OI)(CI)M" /T
```

That gives your account modify rights on the data directory and everything in
it, so all commands work unelevated from then on. Skip it if you would rather
keep the service's state locked down to administrators.

### `Access is denied` copying files, or `OpenSCManager FAILED 5`

The prompt is not elevated, or the files were created by an elevated process
and now belong to Administrators. Check ownership:

```
powershell "(Get-Acl 'C:\ProgramData\WebWatcher\config.json').Owner"
```

### No text extracted / "treating as a failed fetch"

The page renders its content with JavaScript, so `requests` only ever sees an
empty shell. Point a `selector` at a server-rendered part of the page, watch a
different URL, or use a feed. WebWatcher does not run a browser engine.

### Too many notifications

News homepages genuinely change every few minutes. Raise `min_change_chars`,
raise `min_line_chars`, lengthen that site's `check_interval`, or add a
`selector` targeting the one region you care about. Restart afterwards.

---

## Uninstalling

Elevated:

```
cd /d C:\utils\webwatcher
"C:\Program Files\Python314\python.exe" webwatcher.py service stop
"C:\Program Files\Python314\python.exe" webwatcher.py service remove
```

Then delete `C:\utils\webwatcher` and `C:\ProgramData\WebWatcher` if you want
the history and stored password gone too.

---

## If you would rather not use a service

Task Scheduler does the same job with no pywin32 dependency. Create a task
triggered **At startup**, running:

```
"C:\Program Files\Python314\pythonw.exe" C:\utils\webwatcher\webwatcher.py run
```

Set it to *Run whether user is logged on or not*. You lose `services.msc`
integration and automatic restart-on-failure, and `pythonw.exe` is used so no
console window appears.
