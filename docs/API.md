# API reference

34 endpoints. All responses are JSON except `GET /` (HTML) and
`GET /vendor/<file>` (JavaScript).

Base URL when served by Flask: `http://127.0.0.1:4000`

---

## Authentication

Write routes need the session token from login, as a bearer header:

```
Authorization: Bearer csn-admin-<hex>
```

Missing → `401`. Malformed or wrong role → `403`.

| Code | Meaning |
| --- | --- |
| 200 | read or update |
| 201 | created |
| 400 | validation failed — the message names the field |
| 401 | no or unparseable token |
| 403 | authenticated, but the role may not do this |
| 404 | no such record |
| 409 | refused by a guard (open cases, last admin) |

---

## Permission matrix

| Action | public | police | admin |
| --- | :-: | :-: | :-: |
| read any record | ✅ | ✅ | ✅ |
| file an FIR (`add_case`) | | ✅ | ✅ |
| create officer / criminal (`add`) | | ✅ | ✅ |
| edit records (`edit`) | | ✅ | ✅ |
| delete records (`delete`) | | | ✅ |
| user directory (`admin`) | | | ✅ |
| public registry | | ✅ | ✅ |
| raise a citizen request | ✅ | | |
| reply on a request thread | ✅ | ✅ | ✅ |
| file the FIR for a request | | ✅ | ✅ |
| close a request | | ✅ | ✅ |

---

## Auth — `/api/auth`

### `POST /api/auth/signup` → 201

```json
{ "name": "Asha Menon", "username": "asham", "password": "secret123",
  "role": "public", "email": "asha@example.com", "phone": "+91 98111 22333" }
```

`email` **or** `phone` is required — it is the only route to a password reset.

Police additionally require `badge`, `rank`, `stationCode`. Govt Officials
require `officialId`, `designation`, `department`.

`portal` is optional. When it is present it must equal `role`, because each
role has its own front door and a portal registers its own role and no other:

```json
{ "name": "Asha Menon", "username": "asham", "password": "secret123",
  "role": "public", "portal": "public", "email": "asha@example.com" }
```

`400` if `portal` names another role, so the Public portal cannot be used to
mint a police or government account.

```json
{ "username": "asham", "role": "public", "token": "csn-public-9f2c…",
  "display": "Asha Menon", "email": "asha@example.com" }
```

`409` if the username is taken.

### `POST /api/auth/login` → 200

```json
{ "username": "admin", "password": "govt2026" }
```

Returns the same shape as signup. `401` on bad credentials.

### `POST /api/auth/forgot-password` → 200

```json
{ "identifier": "asha@example.com" }
```

Accepts an email address **or** a phone number — never a username. Phone
matching is on digits alone, so `+91 98111 22333`, `919811122333` and
`+91-98111-22333` are equivalent.

| Status | Meaning |
| --- | --- |
| 200 | token issued |
| 403 | the account is police or admin — **not self-service** |
| 404 | no account for that contact |

```json
{ "ok": true, "expiresInMinutes": 30, "via": "email",
  "devLink": "http://127.0.0.1:4000/?reset=…",
  "hint": "Mail delivery is not configured…" }
```

`devLink` is present **only** when SMTP is unconfigured or no email is on file,
so the flow is testable without a mail server. With SMTP configured it is
omitted and the link is emailed.

### `POST /api/auth/reset-password` → 200

```json
{ "token": "…", "password": "brandnew1" }
```

`400` for an unknown, expired or already-used token. `403` if the account
stopped being Public between request and completion. All other outstanding
tokens for that account are burned.

---

## Read — no token required

### `GET /api/stations`
All 6,006 stations. `?state=`, `?q=`, `?lite=1`.

### `GET /api/stations/summary`
Per-state counts, used by the map's state chips.

```json
[ { "state": "Tamil Nadu", "stations": 2433, "cases": 61, "critical": 10 } ]
```

### `GET /api/stations/<id>/cases`
### `GET /api/officers` · `GET /api/officers/<id>/cases`
### `GET /api/criminals` · `GET /api/criminals/<id>`
### `GET /api/cases` — `?state=`, `?status=`, `?severity=`, `?q=`, `?limit=`
### `GET /api/districts`
### `GET /api/relations` — `?limit=` (default 1500) → the wheel graph

```json
{ "a": "CRIM-SI-1002", "b": "CRIM-SI-1148", "kind": "officer",
  "label": "Shared officer: Inspector Vijay Kumar", "strength": 3 }
```

### `GET /api/stats`
```json
{ "total_cases": 275, "open_cases": 132, "closed_cases": 52,
  "critical_cases": 47, "total_stations": 6006 }
```

### `GET /api/analytics`
Aggregates for the 3D command board: by state, by crime type, by severity, by
status, top districts and officer workload.

---

## Protected reads

### `GET /api/users` — **admin only**
Every account. `403` for police and public. Password hashes are never selected.

```json
{ "username": "officer", "role": "police", "display": "Police Officer",
  "badge": "…", "rank": "Inspector", "permissions": ["view","add","edit"] }
```

### `GET /api/public-registry` — **police or admin**
Citizen accounts only.

```json
{ "total": 2,
  "byState": [ { "state": "Kerala", "count": 1 } ],
  "users": [ { "username": "public", "display": "Public",
               "email": "…", "state": null, "district": null } ] }
```

The query filters `WHERE role = 'public'`, so police and admin rows can never
appear. **Phone numbers are stripped** — contact detail, not intelligence.
`403` for public.

---

## Citizen requests — `/api/requests`

A public user cannot file an FIR. What they can do is send a **request** to the
police station for their region. The request is delivered to that station, the
two sides settle the matter over the message thread, and the station files the
FIR itself once the matter is agreed. The FIR links back to the request, so
the citizen keeps one reference and can follow the case through the analytics.

| Route | Who | Does |
| --- | --- | --- |
| `POST /api/requests` | public | raise a request for a district or a specific station |
| `GET /api/requests` | all | own requests (public) · the station's queue (police) · every request (official) |
| `GET /api/requests/<id>` | all | one request with its thread and its case tracking |
| `POST /api/requests/<id>/messages` | all | add a message to the negotiation thread |
| `POST /api/requests/<id>/file-fir` | police, official | file the FIR the negotiation agreed on |
| `POST /api/requests/<id>/close` | police, official | close the request |

### `POST /api/requests` → 201

```json
{ "subject": "Mobile phone stolen near the park",
  "details": "My phone was taken from the bench near the park gate.",
  "category": "VEHICLE", "district": "Hyderabad", "state": "Telangana" }
```

`stationId` may be sent instead of `district`; otherwise the district is
resolved to the station that covers it. The response carries the reference and
the station the request was delivered to:

```json
{ "id": 2, "reference": "CR-2026-2", "status": "Open",
  "station": { "name": "Hyderabad Central PS", "district": "Hyderabad",
               "state": "Telangana" } }
```

A request starts as `Open` and becomes `Under Discussion` when either side
replies. `400` when the subject or the details are too short, `403` for any
account that is not a public one.

### `GET /api/requests/<id>` → 200

The request, its messages and — once the FIR is filed — the tracking block:

```json
{ "reference": "CR-2026-2", "status": "FIR Filed", "case_id": 277,
  "messages": [ { "author": "public", "author_role": "public",
                  "body": "…", "created_at": "2026-10-20 20:11" } ],
  "tracking": { "firNumber": "FIR-2026-9790", "status": "Open",
                "severity": "Medium", "completion": 0,
                "district": "Hyderabad", "districtTotal": 5,
                "districtClosed": 0 } }
```

### `POST /api/requests/<id>/file-fir` → 201

Body: `crimeType`, optional `severity` and `remarks`. Creates the FIR at the
station the request was routed to and links it back:

```json
{ "ok": true, "firNumber": "FIR-2026-9790", "caseId": 277 }
```

`403` for a public account — only the station or an official files the FIR.
`409` if an FIR has already been filed on the request.

---

## Writes

### `POST /api/cases` — `add_case`
```json
{ "stationId": 2565, "crimeType": "THEFT", "status": "Open",
  "officerId": "OFF-SI-001", "criminalId": null,
  "reporterEmail": "citizen@example.com" }
```

`severity` is **derived** from `crimeType` server-side. `fir_number` is
generated.

```json
{ "firNumber": "FIR-2026-9790", "crimeType": "THEFT", "severity": "Medium",
  "status": "Open",
  "mailed": { "confirmation": false, "officerAlert": false } }
```

`mailed` is informational — **a mail failure never loses the case.**

### `PUT /api/cases/<id>` — `edit`
Any subset of `crimeType`, `status`, `severity`, `completionPct`, `crimeCount`,
`stationId`, `officerId`, `criminalId`, `investigationType`, `handling`,
`remarks`, `arrestDate`, `transfers`. Changing the crime type re-derives
severity unless one was pinned.

### `DELETE /api/cases/<id>` — `delete`, admin only
```json
{ "deleted": 275, "firNumber": "FIR-2026-1" }
```

### `POST /api/criminals` — `add`
`name` required; the rest of the profile optional. **Previously had no
permission check at all** — now gated.

### `PUT` / `DELETE /api/criminals/<id>` — `edit` / `delete`

Delete is refused with `409` if the record is named in open cases, and always
removes the record's edges from `criminal_links`.

### `POST /api/officers` — `add`
```json
{ "name": "New Officer", "rank": "DSP", "squad": "QRT" }
→ { "id": "OFF-ADM-0001", "name": "New Officer", "rank": "DSP", "squad": "QRT" }
```

### `PUT` / `DELETE /api/officers/<id>` — `edit` / `delete`

Delete detaches the officer's closed cases and is refused with `409` while they
hold open ones.

### `PUT /api/users/<username>` — **admin only**
Display name, role, badge, rank, squad, official ID, designation, station,
district, state, department, zone, email, phone.

### `DELETE /api/users/<username>` — **admin only**
`409` if this is the last remaining admin.

---

## Static

| Route | Serves |
| --- | --- |
| `GET /` | `frontend/index.html` |
| `GET /login` | `302` to `/login/public` |
| `GET /login/public` | `frontend/index.html` — the citizen portal |
| `GET /login/police` | `frontend/index.html` — the police officer portal |
| `GET /login/admin` | `frontend/index.html` — the government official portal |
| `GET /vendor/<file>` | `three.min.js`, `OrbitControls.js` |

The three portal routes serve the same document. The frontend reads the path,
titles the form for that role and refuses an account belonging to a different
one; the role check itself lives in the browser, the server hands out the same
session token either way.

---

## Full inventory

**Auth (4)** login · signup · forgot-password · reset-password
**Public read (11)** stations · stations/summary · stations/`<id>`/cases ·
officers · officers/`<id>`/cases · criminals · criminals/`<id>` · cases ·
districts · relations · stats · analytics
**Protected read (2)** users · public-registry
**Write (11)** cases POST/PUT/DELETE · criminals POST/PUT/DELETE ·
officers POST/PUT/DELETE · users PUT/DELETE
**Citizen requests (6)** requests POST/GET · requests/`<id>` GET ·
requests/`<id>`/messages · requests/`<id>`/file-fir · requests/`<id>`/close
**Static (5)** `/` · `/login` · `/login/<role>` · `/vendor/<file>`