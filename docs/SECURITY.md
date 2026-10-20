# Security

What this project actually enforces, what it does not, and what must change
before it handles real data. Written as an honest assessment rather than a
checklist of things that were done.

---

## 1. Summary

The permission model is genuinely enforced server-side, and the password reset
flow is built the way a reset flow should be built: hashed single-use tokens,
expiry, re-checked role.

The **session token is not a session.** That is the one thing that makes this
project unsuitable for real use as it stands, and it is stated first because it
should not be discovered later.

---

## 2. What is properly enforced

### Password storage

Passwords are hashed with `werkzeug.security`, which uses **scrypt** by default
— salted, deliberately slow, memory-hard. No password is ever selected into a
response; `GET /api/users` does not include the column at all.

```sql
SELECT username, role, display, email FROM users   -- password is not in the list
```

### Password reset

Implemented correctly, and the details are the point:

| Property | Implementation |
| --- | --- |
| tokens are not secrets at rest | only a SHA-256 **hash** is stored |
| links expire | 30-minute window, checked server-side, row deleted when expired |
| links are single-use | `used_at` is stamped; replay returns `400` |
| old sessions die | every other outstanding token for that account is burned on success |
| privilege cannot be smuggled | the account's role is **re-checked at reset time**, so a demotion mid-flow blocks it |
| no username enumeration for police | the request route returns `403` for police/admin |
| no username reset at all | lookup is by email or phone only; a username returns `404` |

Phone matching normalises to digits alone, so `+91 98765 43210`,
`919876543210` and `+91-98765-43210` reach the same account. A **partial unique
index** on `users.email` and `users.phone` prevents two Public accounts sharing
one reset address, which would otherwise let an attacker reset someone else's
password.

### Server-side authorisation

Every write route calls `_require()` before touching data. A Public account
calling `POST /api/criminals` receives `403` regardless of what the UI shows.
The frontend's `can()` only decides which buttons to render — hiding a button is
cosmetic, and the tests confirm the server refuses independently.

### Referential integrity

`PRAGMA foreign_keys = ON` on every connection, so `cases` cannot point at a
station, officer or criminal that does not exist.

### Data minimisation

`GET /api/public-registry` strips phone numbers server-side. Contact detail is
not intelligence, and the registry does not need it.

---

## 3. What is **not** enforced — read this part

### The token is self-asserting

```python
token = f"csn-{role}-{secrets.token_hex(16)}"
```

and it is parsed back by:

```python
def _role_of():
    parts = token.split("-")
    role = parts[1]
    return role if role in ("public", "police", "admin") else None
```

**The role is read out of the token string.** There is no server-side session
store, no signature, no expiry, no revocation. Consequences:

- Any client can mint `csn-admin-0000000000000000` by typing it, and the server
  will treat the request as a Govt Official.
- Tokens never expire. A leaked token is valid forever.
- Tokens cannot be revoked. Deleting a user account does not invalidate a token
  already issued.
- There is no audit trail — nothing records who did what.

The 128 bits of `token_hex(16)` are real randomness and do prevent *guessing* a
token that was issued. They do nothing against a client that simply writes its
own. This is fine for a classroom demo with three seeded logins and
inappropriate for anything else.

**One step has been taken already.** The token issued at login is now recorded
against the account (`users.session_token`), so the server can resolve *which*
user a token belongs to. The citizen requests rely on it: a request is owned
by the account that raised it, an officer only sees the queue of their own
station, and the thread records who wrote each message. The **role** is still
read out of the token string, so the self-asserting weakness above still
stands for the role; only the identity is now server-side.

### What fixing it requires

The smallest correct change is a server-side session table plus a signed or
opaque lookup — roughly 40 lines:

```sql
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,   -- SHA-256 of the token, same trick as resets
  username   TEXT NOT NULL,
  role       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
```

`_role_of()` becomes a lookup against that table. Add an expiry and delete on
logout, and every one of the four consequences above disappears. Password reset
already hashes tokens correctly, so the pattern is already in the codebase —
it just was not applied to login.

### Other gaps

| Gap | Detail |
| --- | --- |
| **No rate limiting** | `/api/auth/login` accepts unlimited attempts. Passwords are hashed with scrypt, which makes each guess slow — that is real cost, but it is not a limit. A lockout counter or per-IP throttle is the fix. |
| **No CSRF defence** | irrelevant while the API is token-only and there is no cookie session; it returns the moment cookies are introduced |
| **No security headers** | no `CSP`, `X-Content-Type-Options`, `X-Frame-Options` |
| **No HTTPS** | tokens travel in cleartext over HTTP. `127.0.0.1` makes this moot locally and fatal in production |
| **No input sanitisation on display** | values are inserted with `textContent`/template literals, not `innerHTML`, so stored XSS is not currently reachable — but the pattern is one careless edit away from being exploitable |
| **Dev server in production** | `app.run()` is single-threaded and not hardened |
| **Demo credentials in seed** | `admin/govt2026` etc. are in `seed.sql` — fine for a demo, must not reach a real deployment |
| **No audit log** | deletions of officers, criminals, FIRs and accounts leave no trace |
| **Severity from the client for edits** | creation derives severity server-side; `PUT /api/cases/<id>` accepts an explicit severity, which is intentional for pinning but means a police account can relabel a homicide as medium |
| **Console mail fallback** | when SMTP is unconfigured the reset link is returned in the API response as `devLink`. That is deliberate and gated on configuration, but it must never be enabled on a real deployment |

---

## 4. Guards that are about integrity, not secrecy

Distinct from authorisation — these protect the data model rather than
confidentiality:

- **Deleting an officer or criminal with open cases** → `409` with the count.
  Closed cases are detached first, so the record becomes removable once the
  work is done.
- **Deleting a criminal** removes its edges from `criminal_links`, so the
  network never keeps dangling links.
- **Demoting or deleting the last admin** → `409`. Otherwise the only page that
  manages accounts could lock everyone out of it.

---

## 5. Checklist before any real deployment

Ordered by how much damage the gap does.

1. **Replace the self-asserting token** with a server-side session table.
2. **Serve over HTTPS** and add `Strict-Transport-Security`.
3. **Add rate limiting** to `/api/auth/login`, `/api/auth/signup` and
   `/api/auth/forgot-password`.
4. **Remove the seeded demo accounts**, or force a password change on first
   login.
5. **Configure SMTP** and confirm `devLink` no longer appears in responses.
6. **Add security headers**: CSP, `X-Content-Type-Options: nosniff`,
   `X-Frame-Options: DENY`.
7. **Serve through a real WSGI server** — `waitress` or `gunicorn` — not
   `app.run()`.
8. **Add an audit log** for every write and every delete.
9. **Move secrets to the environment**, not `.env` beside the source.
10. **Review data exposure** — the API currently publishes all 6,006 stations
    with coordinates. Real station locations are sensitive.

---

## 6. Honest assessment

For a coursework submission this is a defensible design: the permission model
is real, enforced in the right layer, tested at the boundary, and the reset flow
follows current practice closely.

The gap between "works as a demo" and "safe with real data" is concentrated in
one place — sessions. Fixing that single item removes most of section 3, and
because the password-reset code already demonstrates the hashed-token pattern,
the change is small enough to complete in an afternoon.