# CSNP v3 — Crime Sight Noir

A 3D police intelligence dashboard for peninsular South India. Flask + SQLite
backend, single-page vanilla-JS frontend, every screen rendered as WebGL
geometry rather than 2D charts.

---

## Quick start

```powershell
pip install -r backend/requirements.txt
python backend/app.py
```

Open **http://127.0.0.1:4000/**. The same process serves the API and the UI, so
there is no second server and no build step.

### Demo accounts

| Role | Username | Password |
| --- | --- | --- |
| Govt Official | `admin` | `govt2026` |
| Police Officer | `officer` | `police123` |
| Public | `public` | `public123` |

> These are seeded demo credentials for a coursework project. They are printed
> nowhere in the running app — see [`docs/SECURITY.md`](docs/SECURITY.md).

---

## Repository layout

```
csnp v3/
├── frontend/            the entire UI — one file, no build step
│   ├── index.html           markup + CSS + vanilla JS + the three.js scenes
│   └── vendor/              three.js r147 + OrbitControls, served at /vendor/
├── backend/             HTTP layer
│   ├── app.py                routes, validation, permission gates, JSON errors
│   ├── database.py           connection, schema execution, column migrations
│   ├── mailer.py             SMTP with a console fallback
│   └── requirements.txt
├── authentication/      login / signup / password reset (Flask blueprint)
│   └── auth.py
├── database/            DDL and seed data — no application logic
│   ├── schema.sql            all tables and indexes
│   ├── seed.sql              25 stations, 20 officers, 11 criminals, 75 FIRs, 3 users
│   └── south_india.sql       generated: 5,981 stations, 200 criminals, 854 links
├── data/                the two source spreadsheets
├── imports/             spreadsheet → SQL compiler (standard library only)
├── tests/
│   └── test_api.py           117 assertions, runs against a throwaway DB
├── docs/                the written documentation, including the PDF
└── .env.example         mail configuration template
```

---

## Documentation

| Document | Covers |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the three tiers fit together, request lifecycle, technology choices |
| [`docs/FRONTEND.md`](docs/FRONTEND.md) | Every module of `index.html`: scene system, pages, renderers, permissions |
| [`docs/BACKEND.md`](docs/BACKEND.md) | Flask app, permission matrix, endpoints, validation, mail |
| [`docs/DATABASE.md`](docs/DATABASE.md) | Every table, column, relationship, index, seeding and migration |
| [`docs/API.md`](docs/API.md) | All 28 endpoints with request/response shapes |
| [`docs/SECURITY.md`](docs/SECURITY.md) | What is enforced, what is **not**, and what must change before production |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Running, configuration, backup, troubleshooting |
| [`docs/CRIME-SIGHT-NOIR.pdf`](docs/CRIME-SIGHT-NOIR.pdf) | All of the above in one printable document — 47 pages, A4 |

The PDF is generated from these same Markdown files, so it never drifts from
them. To rebuild after editing a document:

```powershell
python docs\build_pdf.py
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new `
  --no-pdf-header-footer --print-to-pdf="$PWD\docs\CRIME-SIGHT-NOIR.pdf" `
  "file:///$($PWD -replace '\\','/')/docs/CRIME-SIGHT-NOIR.html"
```

`build_pdf.py` has no dependencies — it is a small Markdown-to-HTML converter,
and Chrome does the printing.

---

## Data at a glance

| Table | Rows |
| --- | ---: |
| `police_stations` | 6,006 |
| `criminal_links` | 854 |
| `cases` | 275 |
| `criminals` | 211 |
| `officers` | 190 |
| `users` | 3 |

Every station, officer, criminal and FIR is real South India source data —
station coordinates, district, state and PS code come from the two workbooks in
`data/`, not from generated placeholders.

---

## Features

- **Command map** — the whole of India rendered in 3D, with South India lifted,
  lit and coloured per state. 6,006 station pillars, 200 tracked suspects, orbit
  and zoom.
- **Case register** — every FIR with state / status / severity filters, optional
  grouping, and a per-state breakdown of solved, handling and open.
- **Criminal network** — a slowly rotating wheel of records joined by shared
  officer, station, city or modus operandi; click any node for a full dossier.
- **Public registry** — police and Govt Officials can see citizen accounts only.
  Department accounts never appear on that route.
- **Analytics volume** — a four-quadrant 3D command board over the live totals.
- **Role-aware everything** — three tiers, enforced in the database-facing
  layer rather than by hiding buttons.

---

## Tests

```powershell
python tests/test_api.py
```

117 assertions across routing, both auth flows, password hashing, the permission
matrix for all three roles, password reset (including token expiry and
single-use), and the relationship graph. The suite builds its own throwaway
database and never touches `backend/crimesight.db`.

---

## License

Educational / coursework project.