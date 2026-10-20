# Deployment & operations

Running, configuring, backing up and troubleshooting the project.

---

## 1. Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.10+ | developed on 3.12 |
| Flask | ≥ 3.0 | the only hard dependency |
| flask-cors | ≥ 4.0 | permissive in this build |
| Browser | any WebGL-capable | Chrome, Edge, Firefox, Safari |
| Disk | ~5 MB | database is 4 MB, of which most is station coordinates |

No Node.js, no bundler, no build step, no external database server.

---

## 2. Install

```powershell
cd "csnp v3"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
```

The whole of `requirements.txt`:

```
Flask>=3.0
flask-cors>=4.0
```

Standard library supplies everything else — `sqlite3`, `smtplib`, `secrets`,
`hashlib`, and the `.xlsx` reader in `imports/`.

---

## 3. Run

```powershell
python backend\app.py
```

```
  Crime Sight Noir — CSNP v3
  ─────────────────────────────────────────────
  6,006 stations · 211 criminals · 190 officers · 275 FIRs
  http://127.0.0.1:4000
```

Open the URL. First start takes a second or two longer while the schema runs;
subsequent starts are instant.

Stop with `Ctrl+C`.

---

## 4. Configuration

Everything configurable lives in `.env` at the project root. Copy the template
first:

```powershell
Copy-Item .env.example .env
```

| Variable | Purpose | Default |
| --- | --- | --- |
| `CSN_SMTP_HOST` | SMTP server | — |
| `CSN_SMTP_PORT` | SMTP port | `587` |
| `CSN_SMTP_USER` | SMTP username | — |
| `CSN_SMTP_PASS` | SMTP password or app password | — |
| `CSN_SMTP_FROM` | envelope sender | the SMTP user |
| `CSN_SMTP_USE_TLS` | STARTTLS | `1` |
| `CSN_NOTIFY_OFFICIALS` | comma-separated recipients for new-FIR alerts | — |

Port and host have defaults, so only credentials are required.

### Gmail setup

1. Enable 2-Step Verification on the account.
2. Generate an **App Password** at <https://myaccount.google.com/apppasswords>.
   Gmail rejects the normal account password.
3. Put the 16-character app password in `CSN_SMTP_PASS`.

### Without SMTP

The app is fully functional. `backend/mailer.py` prints the message to stderr
and the API response includes a `devLink` so the reset flow can be completed
from the console. `devLink` disappears automatically once SMTP is configured —
that is the signal it worked.

```json
{ "ok": true, "via": "email", "devLink": "http://127.0.0.1:4000/?reset=…" }
```

> Mail is best-effort by design. A send failure never loses an FIR; the handler
> commits the record first and logs the error.

---

## 5. Database lifecycle

The database is a single file: `backend/crimesight.db`. It is **created
automatically** on first start — there is no install step for it.

At startup, in order:

1. `database/schema.sql` runs (idempotent — all statements are `CREATE … IF NOT EXISTS`)
2. missing columns are added by migration
3. `database/seed.sql` runs **only if `police_stations` is empty**
4. `database/south_india.sql` runs **only while the station table still holds the 25-row demo seed**

Step 4's floor of 1,000 rows is why restarting never re-imports 6,006 stations
over your data.

### Start clean

```powershell
Remove-Item backend\crimesight.db
python backend\app.py
```

Both seed files re-run automatically. This destroys all local changes.

### Backup

```powershell
Copy-Item backend\crimesight.db backend\crimesight.db.bak
```

Or use SQLite's online backup, which is safe while the server is running:

```powershell
python -c "import sqlite3; s=sqlite3.connect('backend/crimesight.db'); d=sqlite3.connect('backup.db'); s.backup(d)"
```

---

## 6. Re-importing datasets

```powershell
python imports\import_datasets.py           # rewrite database/south_india.sql
python imports\import_datasets.py --apply   # …and load it
```

The importer parses `.xlsx` with the standard library alone — a workbook is a
ZIP of XML, so no pandas or openpyxl is needed. 5,981 rows parse in a couple of
seconds.

Re-run after editing either file in `data/`. `--apply` only loads when the
station table is still the demo seed, so it cannot silently overwrite real
data.

---

## 7. Tests

```powershell
python tests\test_api.py
```

151 assertions. The suite builds a **throwaway database** in a temporary
directory and never touches `backend/crimesight.db`, so it is safe to run
against a live install.

Coverage: routing, signup and login, password hashing, the full permission
matrix for all three roles, password reset including expiry and single-use
enforcement, and the relationship graph.

The Public/admin boundary is pinned deliberately — widening `add` to include
Public is the most likely regression, and this is what catches it.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: flask` | dependencies not installed | `pip install -r backend\requirements.txt` |
| `Address already in use` | previous instance still running | kill it, or change the port in `main()` |
| `sqlite3.OperationalError: no such column: email` | a database predates the contact columns | delete the database and restart, or let the migration run |
| Page is blank, console shows a 404 for `three.min.js` | `frontend/vendor/` incomplete | restore it; the CDN fallback needs internet |
| Map renders but South India is flat | projection constants changed | `LON0=82.5`, `LAT0=22.5`, `SCALE=13` in the projection helper |
| 3D objects render black | `vertexColors: true` on geometry with no `color` attribute | drop the flag — instance colours arrive via `instanceColor` |
| Reset email not arriving | SMTP unconfigured or wrong credentials | check `CSN_SMTP_*`; confirm `devLink` is absent from the response |
| Reset says "account not found" | looked up by username | the route accepts **email or phone only** |
| Reset returns 403 | the account is police or admin | self-service reset is Public-only, by design |
| `handling` always shows "Direct" | source data | every seeded row has that literal value — a data property, not a bug |
| Login works but the page stays on the auth screen | JS error in console | check DevTools; usually a stale cached `index.html` |

---

## 9. Serving to other machines

The default bind is `127.0.0.1`, which is loopback-only and unreachable from
another device. To expose it on the LAN, edit the `app.run()` call in
`backend/app.py`:

```python
app.run(host="0.0.0.0", port=4000, debug=False)
```

Then allow the port through Windows Firewall.

**Do this only on a trusted network.** The token scheme is self-asserting —
see [`SECURITY.md`](SECURITY.md) — and traffic is plain HTTP. For anything
beyond a lab, put a reverse proxy with TLS in front, switch to `waitress` or
`gunicorn`, and replace the session scheme first.

---

## 10. What to do before calling this production-ready

1. Replace the self-asserting token with server-side sessions
2. Terminate TLS
3. Add rate limiting on the auth routes
4. Remove the seeded demo accounts
5. Serve through a real WSGI server
6. Add an audit log for writes and deletes
7. Configure SMTP and verify `devLink` no longer appears

The reasoning for each, and the gaps that remain, are in
[`SECURITY.md`](SECURITY.md).