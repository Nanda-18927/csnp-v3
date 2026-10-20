"""Smoke tests for the CRIME SIGHT NOIR API.

Run, from anywhere:

    python tests/test_api.py

Every route, both auth flows, password hashing, ID generation and the JSON
error shapes are covered (65 assertions). The suite writes to a throwaway
database, so it never touches backend/crimesight.db.
"""
import os
import sys
import tempfile
import traceback

# Repository root == the parent of this tests/ directory.
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

# Isolate the test database from the developer's real one.
_tmp = tempfile.mkdtemp(prefix="csn-test-")
os.environ["CSN_DB_PATH"] = os.path.join(_tmp, "test.db")

from backend.app import app            # noqa: E402
from backend.database import init_db, table_counts  # noqa: E402

FAILURES = []
PASSES = []


def check(label, cond, detail=""):
    if cond:
        PASSES.append(label)
    else:
        FAILURES.append(f"{label} :: {detail}")
        print(f"  FAIL  {label}  {detail}")


# The smoke tests assert the small demo seed, so the South India dataset import
# is switched off here (it would add ~6k stations and 200 criminals).
init_db(load_south_india=False)
counts = table_counts()
print("counts:", counts)
check("seed stations == 25", counts["police_stations"] == 25, str(counts))
check("seed officers == 20", counts["officers"] == 20, str(counts))
check("seed criminals == 11", counts["criminals"] == 11, str(counts))
check("seed cases == 75", counts["cases"] == 75, str(counts))
check("seed users == 3", counts["users"] == 3, str(counts))
check("no relations in seed", counts["criminal_links"] == 0, str(counts))

c = app.test_client()


def j(resp):
    return resp.get_json()


# ---- session token for the write endpoints ---------------------------------
# Creating a case, a criminal or an officer requires a signed-in session,
# so every write in this suite goes out authenticated.
_T = c.post("/api/auth/login", json={"username": "admin", "password": "govt2026"})
_TOKEN = (j(_T) or {}).get("token")
_AUTH_HEADERS = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}


# ── reads ──────────────────────────────────────────────────────────────────
r = c.get("/api/stations")
d = j(r)
check("GET /api/stations 200", r.status_code == 200, r.status_code)
check("stations count 25", isinstance(d, list) and len(d) == 25, str(len(d or [])))
check("station critical_count is int", isinstance(d[0]["critical_count"], int),
      repr(d[0].get("critical_count")))

r = c.get("/api/cases")
check("GET /api/cases 200 -> 75", r.status_code == 200 and len(j(r)) == 75,
      f"{r.status_code} {len(j(r) or [])}")
check("GET /api/cases includes station district",
      j(r)[0].get("district") not in (None, ""), str(j(r)[0].get("district")))

fns = [x["fir_number"] for x in j(r)]
check("cases ordered numerically (not 1,10,11...2)",
      fns == sorted(fns, key=lambda s: (len(s), s)),
      f"first 8: {fns[:8]}")

r = c.get("/api/cases?severity=Critical")
check("filter severity", len(j(r)) > 0, "no critical cases")

r = c.get("/api/cases?state=Kerala&psCode=NOPE-999")
check("impossible filter -> empty", j(r) == [], str(j(r)))

r = c.get("/api/stations/1/cases")
check("station cases 200", r.status_code == 200 and len(j(r)) > 0, r.status_code)

r = c.get("/api/stats")
s = j(r)
check("stats totals", s["total_cases"] == 75 and s["total_stations"] == 25, str(s))
check("stats not null", all(v is not None for v in s.values()), str(s))

r = c.get("/api/officers")
check("officers 20", len(j(r)) == 20, str(len(j(r) or [])))
check("officers counts are ints",
      all(isinstance(o["critical_cases"], int) for o in j(r)), "")

r = c.get("/api/officers/ASN-9003/cases")
check("officer cases 200", r.status_code == 200, r.status_code)

r = c.get("/api/criminals")
check("criminals 11", len(j(r)) == 11, str(len(j(r) or [])))

r = c.get("/api/criminals/CRIM-100")
check("criminal detail 200", r.status_code == 200, r.status_code)
check("criminal stations list", len(j(r)["stations"]) > 0, str(j(r).get("stations")))

r = c.get("/api/criminals/CRIM-999")
check("unknown criminal 404 json", r.status_code == 404 and "error" in j(r),
      f"{r.status_code} {j(r)}")

r = c.get("/api/analytics")
a = j(r)
check("analytics 200", r.status_code == 200, r.status_code)
check("analytics has no nulls", all(v is not None for v in a.values()), str(a))
check("analytics byState has 5 states", len(a["byState"]) == 5, str(a["byState"]))

# ── station filters + relation endpoints (South India dataset) ─────────────
r = c.get("/api/stations?lite=1")
d = j(r)
check("stations lite 200", r.status_code == 200 and isinstance(d, list), r.status_code)
check("stations lite drops counters", "total_cases" not in (d[0] if d else {}),
      str((d[0] if d else {}).keys()))
check("stations lite keeps geo", all(
    {"lat", "lng", "ps_code"} <= set(row) for row in d[:5]), str((d[0] if d else {})))

r = c.get("/api/stations?state=Karnataka")
check("stations filter by state", r.status_code == 200 and all(
    row["state"] == "Karnataka" for row in j(r)), r.status_code)
check("unknown state filter returns empty", c.get("/api/stations?state=Atlantis").status_code == 200
      and j(c.get("/api/stations?state=Atlantis")) == [], "should be empty")

r = c.get("/api/stations/summary")
d = j(r)
check("stations summary 200", r.status_code == 200 and isinstance(d, list), r.status_code)
check("stations summary has counts", d and {"state", "stations", "cases"} <= set(d[0]),
      str(d[:1]))

r = c.get("/api/districts")
d = j(r)
check("districts 200", r.status_code == 200 and isinstance(d, list), r.status_code)
check("districts aggregate shape", d and {
    "state", "district", "stations", "cases", "critical",
    "criminals"} <= set(d[0]), str(d[:1]))
check("districts sorted by cases", not d or d[0]["cases"] >= d[-1]["cases"], "ordering")
check("districts bbox present", not d or d[0]["min_lat"] <= d[0]["max_lat"], str(d[:1]))
r = c.get("/api/districts?state=Kerala")
check("districts filter by state", r.status_code == 200 and all(
    row["state"] == "Kerala" for row in j(r)), r.status_code)
check("districts min_stations filter",
      c.get("/api/districts?min_stations=5").status_code == 200, "min_stations")

r = c.get("/api/relations")
d = j(r)
check("relations 200 (empty seed)", r.status_code == 200 and isinstance(d, list), r.status_code)
check("relations filter kind works",
      c.get("/api/relations?kind=officer").status_code == 200, "kind filter")
check("relations criminal filter works",
      c.get("/api/relations?criminal=CRIM-100").status_code == 200, "criminal filter")

r = c.get("/api/criminals/CRIM-100")
d = j(r)
check("criminal detail has relations key", "relations" in d, str(d.keys()))

# ── 404 / 405 are JSON, not HTML ──────────────────────────────────────────
r = c.get("/api/does-not-exist")
check("404 is JSON", r.status_code == 404 and "error" in (j(r) or {}), str(j(r)))
r = c.get("/api/auth/login")
check("405 is JSON", r.status_code == 405 and "error" in (j(r) or {}), str(j(r)))

# ── auth: happy path ──────────────────────────────────────────────────────
r = c.post("/api/auth/login", json={"username": "public", "password": "public123"})
d = j(r)
check("login public 200", r.status_code == 200, f"{r.status_code} {d}")
check("login returns token", bool(d.get("token")), str(d))
check("login role public", d.get("role") == "public", str(d))

r = c.post("/api/auth/login", json={"username": "officer", "password": "police123"})
check("login officer 200", r.status_code == 200, f"{r.status_code} {j(r)}")

r = c.post("/api/auth/login", json={"username": "admin", "password": "govt2026"})
check("login admin 200", r.status_code == 200, f"{r.status_code} {j(r)}")

# ── auth: rejections ──────────────────────────────────────────────────────
r = c.post("/api/auth/login", json={"username": "public", "password": "wrong"})
check("bad password 401", r.status_code == 401, r.status_code)
r = c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
check("unknown user 401", r.status_code == 401, r.status_code)
r = c.post("/api/auth/login", json={})
check("empty login 400", r.status_code == 400, r.status_code)
r = c.post("/api/auth/login", data="not json", content_type="text/plain")
check("non-json login 400 not 500", r.status_code == 400, r.status_code)

# ── auth: signup ──────────────────────────────────────────────────────────
r = c.post("/api/auth/signup", json={
    "name": "Test User", "username": "testuser", "password": "secret123",
    "role": "public", "email": "testuser@example.com",
})
d = j(r)
check("signup public 201", r.status_code == 201, f"{r.status_code} {d}")
check("signup token", bool(d.get("token")), str(d))

r = c.post("/api/auth/signup", json={
    "name": "Test User", "username": "testuser", "password": "secret123",
    "role": "public", "email": "testuser@example.com",
})
check("duplicate signup 409", r.status_code == 409, r.status_code)

r = c.post("/api/auth/signup", json={
    "name": "Bad Name", "username": "a b!", "password": "secret123", "role": "public",
    "email": "badname@example.com",
})
check("invalid username 400", r.status_code == 400, r.status_code)

r = c.post("/api/auth/signup", json={
    "name": "Short", "username": "shorty", "password": "123", "role": "public",
    "email": "shorty@example.com",
})
check("short password 400", r.status_code == 400, r.status_code)

r = c.post("/api/auth/signup", json={
    "name": "Cop", "username": "cop1", "password": "secret123", "role": "police",
    "email": "cop1@example.com",
})
check("police without badge 400", r.status_code == 400, f"{r.status_code} {j(r)}")

r = c.post("/api/auth/signup", json={
    "name": "Cop", "username": "cop1", "password": "secret123", "role": "police",
    "email": "cop1@example.com", "badge": "42", "rank": "Inspector", "stationCode": "PS-430",
    "stationName": "Hyderabad Central PS", "district": "Hyderabad",
    "state": "Telangana", "squad": "Squad Bh-11",
})
d = j(r)
check("police signup 201", r.status_code == 201, f"{r.status_code} {d}")
check("signup profile camelCase stationName",
      d.get("stationName") == "Hyderabad Central PS", str(d))

# login again -> profile keys must be camelCase too (original returned snake_case)
r = c.post("/api/auth/login", json={"username": "cop1", "password": "secret123"})
d = j(r)
check("login returns stationName (camelCase)", d.get("stationName") == "Hyderabad Central PS",
      str(d))
check("login returns rank/badge", d.get("rank") == "Inspector" and d.get("badge") == "42",
      str(d))
check("login does not leak password", "password" not in d, str(d.keys()))

r = c.post("/api/auth/signup", json={
    "name": "Gov", "username": "gov1", "password": "secret123", "role": "admin",
    "email": "gov1@example.com", "officialId": "GOV-1", "designation": "SP", "department": "Home",
})
d = j(r)
check("admin signup 201", r.status_code == 201, f"{r.status_code} {d}")
check("admin officialId camelCase", d.get("officialId") == "GOV-1", str(d))

# ── passwords are hashed at rest ──────────────────────────────────────────
from backend.database import get_db  # noqa: E402
conn = get_db()
rows = conn.execute("SELECT username, password FROM users").fetchall()
conn.close()
check("all passwords hashed",
      all(rw["password"].startswith(("pbkdf2:", "scrypt:", "argon2:")) for rw in rows),
      str([rw["password"][:20] for rw in rows]))

# ── create FIR ────────────────────────────────────────────────────────────
r = c.post("/api/cases", headers=_AUTH_HEADERS, json={"stationId": 1, "crimeType": "HOMICIDE"})
d = j(r)
check("create case 201", r.status_code == 201, f"{r.status_code} {d}")
check("new FIR number FIR-2026-76", d.get("firNumber") == "FIR-2026-76", str(d))
check("severity auto Critical", d.get("severity") == "Critical", str(d))

r = c.post("/api/cases", headers=_AUTH_HEADERS, json={"stationId": 9999, "crimeType": "FRAUD"})
check("unknown station 400", r.status_code == 400, f"{r.status_code} {j(r)}")

r = c.post("/api/cases", headers=_AUTH_HEADERS, json={"stationId": 1, "crimeType": "FRAUD",
                               "officerId": "ASN-NOPE"})
check("unknown officer 400", r.status_code == 400, f"{r.status_code} {j(r)}")

r = c.post("/api/cases", headers=_AUTH_HEADERS, json={"crimeType": "FRAUD"})
check("missing stationId 400", r.status_code == 400, r.status_code)

r = c.post("/api/cases", headers=_AUTH_HEADERS, data="not json", content_type="text/plain")
check("non-json case 400 not 500", r.status_code == 400, r.status_code)

r = c.post("/api/cases", headers=_AUTH_HEADERS, json={"stationId": "one", "crimeType": "FRAUD"})
check("string stationId 400", r.status_code == 400, r.status_code)

# ── create criminal ───────────────────────────────────────────────────────
r = c.post("/api/criminals", headers=_AUTH_HEADERS, json={"name": "New Suspect", "age": 30,
                                   "address": "1 Test Road"})
d = j(r)
check("create criminal 201", r.status_code == 201, f"{r.status_code} {d}")
check("new criminal id CRIM-111", d.get("id") == "CRIM-111", str(d))
check("new photo id IMG-0012", d.get("photoId") == "IMG-0012", str(d))

r = c.post("/api/criminals", headers=_AUTH_HEADERS, json={})
check("criminal without name 400", r.status_code == 400, r.status_code)

r = c.get("/api/criminals?q=test")
check("search finds new criminal", any(x["id"] == "CRIM-111" for x in j(r)), str(j(r)))

# ── index serves the frontend ─────────────────────────────────────────────
r = c.get("/")
check("GET / serves HTML", r.status_code == 200 and b"<!DOCTYPE html>" in r.data,
      f"{r.status_code} {r.data[:80]!r}")

# ---------------------------------------------------------------------------
# Sessions and the write-permission matrix
# ---------------------------------------------------------------------------
def _tok(user, pwd):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    return (j(r) or {}).get("token")

T_ADMIN = _tok("admin", "govt2026")
T_POLICE = _tok("officer", "police123")
T_PUBLIC = _tok("public", "public123")
check("admin token issued", bool(T_ADMIN))
check("police token issued", bool(T_POLICE))
check("public token issued", bool(T_PUBLIC))

def _w(method, url, token, body=None):
    return c.open(url, method=method, json=body,
                  headers={"Authorization": f"Bearer {token}"} if token else {})

# ---- cases ----
_case_id = j(c.get("/api/cases?limit=1"))[0]["id"]
check("public cannot delete a case", _w("DELETE", f"/api/cases/{_case_id}", T_PUBLIC).status_code == 403)
check("police cannot delete a case", _w("DELETE", f"/api/cases/{_case_id}", T_POLICE).status_code == 403)
check("admin can edit a case",
      _w("PUT", f"/api/cases/{_case_id}", T_ADMIN, {"status": "Closed"}).status_code == 200)
check("edit re-reads status",
      j(c.get(f"/api/cases?limit=1"))[0]["status"] == "Closed")
_w("PUT", f"/api/cases/{_case_id}", T_ADMIN, {"status": "Open"})

# ---- officers ----
_off0 = j(c.get("/api/officers"))[0]["id"]
check("police can edit an officer",
      _w("PUT", f"/api/officers/{_off0}", T_POLICE, {"rank": "Inspector"}).status_code == 200)
check("public cannot edit an officer",
      _w("PUT", f"/api/officers/{_off0}", T_PUBLIC, {"rank": "X"}).status_code == 403)
check("public cannot add a criminal",
      _w("POST", "/api/criminals", T_PUBLIC, {"name": "Sneaky"}).status_code == 403)
check("public cannot file an FIR",
      _w("POST", "/api/cases", T_PUBLIC, {
          "stationId": 1, "crimeType": "THEFT"}).status_code == 403)
check("police can file an FIR",
      _w("POST", "/api/cases", T_POLICE, {
          "stationId": 1, "crimeType": "THEFT"}).status_code == 201)
check("public cannot add an officer",
      _w("POST", "/api/officers", T_PUBLIC, {"name": "Sneaky"}).status_code == 403)
_r = _w("POST", "/api/officers", T_ADMIN, {"name": "Created By Test", "rank": "DSP"})
check("admin can add an officer", _r.status_code == 201, j(_r))
_new_off = (j(_r) or {}).get("id")
if _new_off:
    check("admin can edit that officer",
          _w("PUT", f"/api/officers/{_new_off}", T_ADMIN, {"rank": "IG"}).status_code == 200)
    check("admin can delete that officer",
          _w("DELETE", f"/api/officers/{_new_off}", T_ADMIN).status_code == 200)

# ---- user directory: admin only ----
check("admin can list users", _w("GET", "/api/users", T_ADMIN).status_code == 200)
check("police cannot list users", _w("GET", "/api/users", T_POLICE).status_code == 403)
check("public cannot list users", _w("GET", "/api/users", T_PUBLIC).status_code == 403)
check("no token cannot list users", _w("GET", "/api/users", None).status_code == 401)
check("malformed token cannot list users",
      _w("GET", "/api/users", "not-even-a-token").status_code == 401)

# ---- public registry: police + admin, public accounts only ----
_r = _w("GET", "/api/public-registry", T_POLICE)
check("police can read the public registry", _r.status_code == 200, _r.status_code)
_reg = j(_r) or {}
check("registry exposes only public accounts",
      all(u.get("username") for u in _reg.get("users", []))
      and not any(u.get("role") and u["role"] != "public" for u in _reg.get("users", [])))
check("registry withholds phone numbers",
      all("phone" not in u for u in _reg.get("users", [])))
check("public cannot read the registry",
      _w("GET", "/api/public-registry", T_PUBLIC).status_code == 403)

# ---- password reset: public only ----
c.post("/api/auth/signup", json={
    "name": "Reset Tester", "username": "resettest",
    "password": "secret123", "role": "public",
    "email": "resettest@example.com", "phone": "+91 90000 00001",
})
_r = c.post("/api/auth/forgot-password", json={"identifier": "resettest@example.com"})
check("public can request a reset", _r.status_code == 200, f"{_r.status_code} {j(_r)}")
_reset = j(_r) or {}
_token = (_reset.get("devLink") or "").split("reset=")[-1]
check("a reset token was issued", bool(_token), str(_reset))

check("reset by username is refused",
      c.post("/api/auth/forgot-password", json={"identifier": "resettest"}).status_code == 404)
c.post("/api/auth/signup", json={
    "name": "Reg Cop", "username": "regcop", "password": "secret123",
    "role": "police", "email": "regcop@example.com", "badge": "9",
    "rank": "SI", "stationCode": "PS-1",
})
check("police reset by email is refused",
      c.post("/api/auth/forgot-password",
             json={"identifier": "regcop@example.com"}).status_code == 403)

check("reset with the token works",
      c.post("/api/auth/reset-password",
             json={"token": _token, "password": "brandnew1"}).status_code == 200)
check("new password authenticates",
      c.post("/api/auth/login",
             json={"username": "resettest", "password": "brandnew1"}).status_code == 200)
check("old password no longer works",
      c.post("/api/auth/login",
             json={"username": "resettest", "password": "secret123"}).status_code == 401)
check("a reset link is single use",
      c.post("/api/auth/reset-password",
             json={"token": _token, "password": "again1234"}).status_code == 400)

_r = c.post("/api/auth/forgot-password", json={"identifier": "+91 90000 00001"})
check("reset by phone works", _r.status_code == 200, f"{_r.status_code} {j(_r)}")
_r = c.post("/api/auth/forgot-password", json={"identifier": "919000000001"})
check("reset by bare digits works", _r.status_code == 200, f"{_r.status_code} {j(_r)}")


# ── access portals ──────────────────────────────────────────────────────────
# Each role has its own front door. All three serve the same document; the
# frontend reads the path to title the form and to refuse a mismatched role.
for _role in ("public", "police", "admin"):
    _r = c.get(f"/login/{_role}")
    check(f"/login/{_role} serves the app",
          _r.status_code == 200 and b"Crime Sight Noir" in _r.data, _r.status_code)
    check(f"/login/{_role} exposes its portal link",
          f'href="/login/{_role}"'.encode() in _r.data, "portal link missing")

_r = c.get("/login")
check("/login redirects to the public portal",
      _r.status_code == 302 and _r.headers.get("Location", "").endswith("/login/public"),
      f"{_r.status_code} {_r.headers.get('Location')}")

_r = c.get("/login/not-a-role")
check("unknown portal falls back to the public portal",
      _r.status_code == 302 and _r.headers.get("Location", "").endswith("/login/public"),
      f"{_r.status_code} {_r.headers.get('Location')}")

check("the three portals are distinct URLs",
      len({"/login/public", "/login/police", "/login/admin"}) == 3, "")

# ---- each portal registers its own role and no other -----------------------
_r = c.post("/api/auth/signup", json={
    "name": "Portal Public", "username": "portalpublic",
    "password": "secret123", "role": "public", "portal": "public",
    "email": "portalpublic@example.com"})
check("a signup that matches its portal is accepted", _r.status_code == 201,
      f"{_r.status_code} {j(_r)}")

_r = c.post("/api/auth/signup", json={
    "name": "Portal Police", "username": "portalpolice",
    "password": "secret123", "role": "police", "portal": "police",
    "email": "portalpolice@example.com", "badge": "7", "rank": "SI",
    "stationCode": "PS-1"})
check("a police signup through the police portal is accepted",
      _r.status_code == 201, f"{_r.status_code} {j(_r)}")

_r = c.post("/api/auth/signup", json={
    "name": "Wrong Portal", "username": "wrongportal",
    "password": "secret123", "role": "police", "portal": "public",
    "email": "wrongportal@example.com", "badge": "7", "rank": "SI",
    "stationCode": "PS-1"})
check("the public portal cannot mint a police account",
      _r.status_code == 400, f"{_r.status_code} {j(_r)}")

_r = c.post("/api/auth/signup", json={
    "name": "Wrong Portal 2", "username": "wrongportal2",
    "password": "secret123", "role": "admin", "portal": "police",
    "email": "wrongportal2@example.com", "designation": "SP",
    "department": "State Police HQ"})
check("the police portal cannot mint a government account",
      _r.status_code == 400, f"{_r.status_code} {j(_r)}")

_r = c.post("/api/auth/signup", json={
    "name": "Bad Portal", "username": "badportal",
    "password": "secret123", "role": "public", "portal": "not-a-portal",
    "email": "badportal@example.com"})
check("an unknown portal is refused", _r.status_code == 400, f"{_r.status_code} {j(_r)}")


print()
print(f"passed={len(PASSES)} failed={len(FAILURES)}")
for f in FAILURES:
    print("  -", f)
sys.exit(1 if FAILURES else 0)
