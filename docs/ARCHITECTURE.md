# Architecture

How the three tiers of this project fit together, what happens to a request,
and why each technology was chosen.

---

## 1. The shape of the system

Three tiers, deliberately unequal in size.

```
┌──────────────────────────────────────────────────────────────────┐
│  BROWSER                                                        │
│  frontend/index.html — 4,911 lines                             │
│  ├─ HTML   8 page sections, modals, panels                     │
│  ├─ CSS    one stylesheet, design tokens as CSS variables      │
│  ├─ JS     app logic, renderers, permission gates              │
│  └─ WebGL  three.js scenes (command map, wheel, analytics…)     │
└───────────────┬──────────────────────────────────────────────────┘
                │  fetch()  — JSON over HTTP
                │  Authorization: Bearer csn-<role>-<random>
┌───────────────▼──────────────────────────────────────────────────┐
│  FLASK  (Python 3.12)                                           │
│  ├─ authentication/auth.py   login · signup · password reset    │
│  │    └─ Blueprint mounted at /api/auth                        │
│  └─ backend/app.py           28 routes, validation, permissions │
│       ├─ _require(action)    the single permission gate        │
│       └─ backend/mailer.py   SMTP, falls back to console       │
└───────────────┬──────────────────────────────────────────────────┘
                │  sqlite3 (standard library)
┌───────────────▼──────────────────────────────────────────────────┐
│  SQLITE   backend/crimesight.db                                 │
│  6,006 stations · 211 criminals · 190 officers · 275 cases     │
│  854 relation edges · 3 users                                    │
└──────────────────────────────────────────────────────────────────┘
```

The frontend is served **by** Flask, from the same origin as the API. There is
no CORS negotiation in normal use, no proxy, and no second port.

---

## 2. Request lifecycle

Taking "load the criminal network" as the worked example:

```
1.  showSec('criminals')
        hides the other 7 sections, syncs the nav tabs
2.  renderCriminals(query)
        reads the cached CRL / CASES / RELATIONS arrays
3.  computes per-criminal facts, filters, groups by state/district/…
4.  CSN3D.updateCriminals({list, relations, linkKinds, headline})
5.  pushStage('criminals', …)
        reuses the existing WebGL world if already built, else creates one
6.  buildNetwork(world, list, payload)
        lays nodes on a circle, batches every link kind into 2 ribbons,
        starts one requestAnimationFrame loop
7.  one canvas on screen; OrbitControls handles orbit and zoom
```

Steps 1–4 are pure data work — microseconds. Everything after that is GPU
rendering, so switching tabs never waits on the network.

**First paint is the exception.** `fetchInitialData()` runs six parallel
requests (`stations`, `officers`, `criminals`, `cases`, `districts`,
`relations`) before the app is usable. At full dataset size the `stations`
payload is 6,006 rows — the largest response in the app — and is the main cost
of a cold start.

---

## 3. Technology choices

### Why SQLite

The dataset is 275 FIRs and 6,006 stations. SQLite is a single file, needs no
server, ships inside Python, and survives being copied. `sqlite3` is in the
standard library, so the only third-party runtime dependencies in the whole
project are **Flask** and **flask-cors**.

A server database would add an install step and a process to supervise for no
benefit at this scale. The migration path, if the data ever outgrows a file, is
contained in `backend/database.py` — every query in `app.py` goes through
`get_db()` and plain SQL, with no ORM in the way.

### Why no build step

`frontend/index.html` is served as-is. There is no bundler, no transpiler, no
`node_modules`. The browser loads one HTML file and two vendored scripts.

This is a deliberate trade. A single 4,911-line file is genuinely unpleasant to
work in, and a build step would let us split it into modules. In exchange,
anyone can open the project, run two commands, and see it work — no Node
toolchain, no version drift, no "works on my machine". For a teaching project
that readability-at-a-glance is worth more than a tidy directory.

`three.js` is **vendored**, not CDN-loaded. A demo that needs internet to render
is a demo that fails in a room with bad wifi. `ensure()` still falls back to
jsDelivr if the vendored files are missing.

### Why 3D for everything

Every screen is WebGL geometry: the map is extruded state polygons and instanced
pillars, the analytics board is bars and sectors, the criminal wheel is
instanced octahedra with batched ribbon geometry. The motivation is that a
police-intelligence console should look like one, and because instancing makes
211 nodes plus 854 links cheap — about 40 draw calls instead of ~1,200.

The cost is real: WebGL is harder to debug than DOM, hit-testing needs a
custom picker, and text is drawn on canvas sprites rather than laid out by the
browser. Every one of those costs was paid and documented in
[`FRONTEND.md`](FRONTEND.md).

---

## 4. Permission architecture

Permissions are enforced in **one function**, and every write route calls it.

```python
def _require(action):
    role = _role_of()
    table = {
        "add_case": ("police", "admin"),
        "add":       ("police", "admin"),
        "edit":      ("police", "admin"),
        "delete":    ("admin",),
        "admin":     ("admin",),
    }[action]
```

| action | public | police | admin |
| --- | :-: | :-: | :-: |
| `add_case` | | ✅ | ✅ |
| `add` | | ✅ | ✅ |
| `edit` | | ✅ | ✅ |
| `delete` | | | ✅ |
| `admin` | | | ✅ |

`add_case` exists as a separate key from `add` so the two write tiers stay
explicit rather than one key widening silently. A Public account may only read:
every write route answers `403` for that role. The test suite covers this
specific boundary.

The frontend mirrors the same table in `can()` so buttons can be hidden, but the
frontend is never the enforcement point: hiding a button is cosmetic, and the
server returns 403 regardless.

---

## 5. Data flow at startup

```
python backend/app.py
  │
  ├─ create_app()
  ├─ init_db()
  │    ├─ executes database/schema.sql        (tables + indexes)
  │    ├─ applies column migrations          (older databases gain new columns)
  │    ├─ executes database/seed.sql         (only if police_stations is empty)
  │    └─ executes database/south_india.sql   (only while still the 25-row seed)
  ├─ prints the route banner
  └─ app.run() on 127.0.0.1:4000
```

The schema is idempotent — every statement is `CREATE TABLE IF NOT EXISTS` — so
restarting never destroys data. The index creation for `users.email` and
`users.phone` deliberately happens in Python *after* the column migration,
because an existing database does not have those columns when the schema script
first runs.

---

## 6. Directory responsibilities

| Directory | Owns | Must never contain |
| --- | --- | --- |
| `frontend/` | presentation, scene rendering, client permission checks | business rules, SQL |
| `backend/` | HTTP, validation, permission enforcement, mail | rendering logic |
| `authentication/` | credentials, sessions, password reset | domain data |
| `database/` | DDL and seed data only | any application logic |
| `data/` | the source spreadsheets | anything hand-edited |
| `imports/` | spreadsheet → SQL compilation | runtime code |
| `tests/` | assertions against a throwaway database | production data |

---

## 7. Known architectural limits

- **Single-process, single-threaded.** The Flask dev server handles one request
  at a time. Fine for a demo; not a deployment target.
- **No caching layer.** `fetchInitialData()` refetches everything on every
  reload. A cache keyed on the SQLite mtime would remove most of that.
- **The whole station table ships to the browser** — about 1.4 MB of JSON — even
  though only South India is displayed. A `/api/stations?bbox=` endpoint would
  cut it by roughly an order of magnitude.
- **Token scheme is self-asserting.** Detailed in [`SECURITY.md`](SECURITY.md),
  and the single most important thing to change before real use.