# Backend

Two Python modules plus a mailer. The only third-party runtime dependencies in
the entire project are Flask and flask-cors.

---

## 1. Files

| File | Lines | Responsibility |
| --- | ---: | --- |
| `backend/app.py` | 1,075 | Flask app, 28 routes, validation, permission gates |
| `backend/database.py` | 170 | Connection, schema execution, column migrations |
| `backend/mailer.py` | 119 | SMTP with a console fallback |
| `authentication/auth.py` | 317 | Login, signup, password reset — a Blueprint at `/api/auth` |
| `backend/requirements.txt` | 2 lines | `Flask>=3.0`, `flask-cors>=4.0` |

---

## 2. Application assembly

```python
app = Flask(__name__)
CORS(app)
app.register_blueprint(auth_bp)          # /api/auth/*
```

`auth.py` is a separate Blueprint rather than part of `app.py` because
credentials are a genuinely different concern: `auth.py` never touches
`police_stations`, `cases` or any domain table.

At startup `main()` calls `init_db()`, prints a route banner, then `app.run()`.

---

## 3. Database access

```python
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row        # name-based column access
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

Every handler opens its own connection in a `try/finally` and closes it. There
is no connection pool and no global connection — SQLite in WAL mode handles
concurrent readers cheaply, and the alternative (a module-level connection
shared across threads) is a well-known source of subtle bugs.

`sqlite3.Row` is why queries read `row["status"]` rather than `row[1]`. It also
means positional indexing fails — an early version of the delete guard used
`.fetchone()[0]` and raised `KeyError: 0`.

---

## 4. Schema execution and migrations

`init_db()` does four things in order:

1. `executescript(schema.sql)` — all `CREATE TABLE IF NOT EXISTS`, idempotent
2. column migrations — `ALTER TABLE … ADD COLUMN` for anything an older
   database lacks
3. `seed.sql` — **only** when `police_stations` is empty
4. `south_india.sql` — **only** while the table still holds the 25-row demo seed

Step 4's guard is why restarting never re-imports 6,006 stations over your data.

Migrations are driven by an allow-list of `(column, type)` tuples. Column names
come from that tuple, never from request data, so the `f"ALTER TABLE {col}"`
interpolation is safe.

Index creation for `users.email` / `users.phone` deliberately happens in Python
**after** the migration. Creating them inside `schema.sql` breaks startup on an
existing database, because that database does not have the columns yet when the
schema script runs — `sqlite3.OperationalError: no such column: email`.

---

## 5. The permission gate

One function, called by every write route.

```python
def _role_of():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else None
    token = token or request.headers.get("X-Session-Token")
    …  return role if role in ("public","police","admin") else None

def _require(action):
    role = _role_of()
    table = {"add_case": ("police","admin"),
             "add":       ("police","admin"),
             "edit":      ("police","admin"),
             "delete":    ("admin",),
             "admin":     ("admin",)}[action]
    if role is None:   return jsonify({"error": "Authentication required"}), 401
    if role not in table: return jsonify({"error": f"Role '{role}' may not {action}…"}), 403
    return None
```

Handlers use it as an early return:

```python
denied = _require("delete")
if denied:
    return denied
```

A Public account may only read. Officers, criminals, FIRs and user records are
closed to it — every write route answers `403` for the Public role. `add_case`
is kept as a separate key from `add` so the two write tiers stay explicit, and
the test suite pins this boundary.

### Guards beyond the gate

- **Deleting an officer or criminal with open cases** → `409`, listing the count.
  Closed cases are detached first so the record can still be removed once the
  work is done.
- **Deleting a criminal** also removes its edges from `criminal_links`, so the
  network never keeps dangling links.
- **Demoting or deleting the last admin** → `409`. Otherwise the only page that
  manages accounts could lock everyone out of it.
- **Deleting an FIR** is unconditional — the FIR *is* the record.

---

## 6. Validation

`_body()` returns `request.get_json(silent=True) or {}`, so a malformed body is
an empty dict rather than a 500. `_int()` coerces and clamps:

```python
completion_pct = _int(data.get("completionPct"), 0, minimum=0, maximum=100)
```

Every write builds an explicit column list rather than interpolating request
keys into SQL. Unknown fields are ignored; a wrong type is coerced or rejected
with a 400 naming the offending field.

---

## 7. Error shape

Uniform JSON, always:

```json
{ "error": "Unknown stationId 99999" }
```

Status codes in use: `200` read/update, `201` create, `400` validation, `401` no
or malformed token, `403` wrong role, `404` no such record, `409` refused by a
guard, `500` unexpected (logged with a traceback, never a stack trace in the
response body).

---

## 8. Authentication

`auth.py` owns credentials. Passwords use `werkzeug.security` — scrypt by
default, salted, never returned in any response.

```python
POST /api/auth/signup   name, username, password, role,
                        email|phone (one required),
                        + badge/rank/station for police,
                        + officialId/designation/department for admin
POST /api/auth/login    → { username, role, token, …profile }
```

The returned `token` is `f"csn-{role}-{secrets.token_hex(16)}"`. **It is not a
session.** See [`SECURITY.md`](SECURITY.md).

### Password reset

Public accounts only, and identified by **email or phone — never username**.

| Step | Endpoint | Notes |
| --- | --- | --- |
| request | `POST /api/auth/forgot-password` | 30-minute token; **403** if the account is police or admin |
| complete | `POST /api/auth/reset-password` | token + new password |

Properties that matter:

- Only a **SHA-256 hash** of the token is stored, so a leaked database cannot be
  used to complete a reset.
- Single use — `used_at` is stamped, and replay returns 400.
- Expiry is checked and the row deleted when expired.
- All other outstanding tokens for that account are burned on success.
- The account's role is **re-checked at reset time**, so a demotion mid-flow
  blocks it.
- Phone numbers are matched on digits alone, so `+91 98765 43210`,
  `919876543210` and `+91-98765-43210` all reach the same account.

---

## 9. Mail

`backend/mailer.py` reads `.env` with a small built-in parser (no dependency for
four settings) and sends three kinds of message: password reset, FIR
confirmation to the reporter, and a new-FIR alert to `CSN_NOTIFY_OFFICIALS`.

If SMTP is unconfigured, it prints the message to stderr and returns
`False`. The API still succeeds:

```json
{ "firNumber": "FIR-2026-9790", "mailed": { "confirmation": false, "officerAlert": false } }
```

**Mail is best-effort by design** — a mail failure must never lose an FIR. The
`create_case` handler wraps both sends in `try/except` and logs, so the case is
committed first.

Gmail requires an **app password**, not the account password.

---

## 10. Performance notes

| Endpoint | Cost | Note |
| --- | --- | --- |
| `GET /api/stations` | ~1.4 MB | 6,006 rows; the largest response in the app |
| `GET /api/relations?limit=1500` | ~230 KB | the criminal graph |
| `GET /api/cases` | ~300 KB | 275 rows |
| `GET /api/stats` | trivial | one aggregate |

Indexes cover the foreign keys (`idx_cases_station`, `idx_cases_criminal`) and
the graph (`idx_links_a`, `idx_links_b`), which are the columns every
relationship query joins on. Without them the wheel renderer degrades visibly.

`/api/stations` returning the whole country when only South India is displayed is
the clearest optimisation available: a bounding-box filter would cut it by
roughly an order of magnitude.