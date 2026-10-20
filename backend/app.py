"""CRIME SIGHT NOIR - Flask API server.

Run:
    pip install -r backend/requirements.txt
    python backend/app.py

Environment:
    PORT        listen port                 (default 4000)
    HOST        bind address                (default 127.0.0.1; use 0.0.0.0
                                             to expose on the LAN)
    FLASK_DEBUG "1" enables the Werkzeug debugger (never on a public host)
    CSN_DB_PATH override the SQLite file location
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_cors import CORS

# Make the repository root importable no matter where the process is started.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from authentication.auth import auth_bp  # noqa: E402
from backend.database import DB_PATH, FRONTEND_FILE, get_db, init_db  # noqa: E402
from backend.mailer import send_case_alert_to_officials, send_case_confirmation  # noqa: E402

# Force UTF-8 output on Windows so the emoji in the banner don't crash the
# console. Guarded: sys.stdout can be None (pythonw.exe) or a non-text stream.
_stdout = sys.stdout
if _stdout is not None and hasattr(_stdout, "reconfigure"):
    try:
        _stdout.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

PORT = int(os.environ.get("PORT", "4000"))
HOST = os.environ.get("HOST", "127.0.0.1")
DEBUG = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

app = Flask(__name__)

# Restrict cross-origin access when deployed; permissive only for local dev.
_origins = os.environ.get("CSN_CORS_ORIGINS", "").strip()
if _origins:
    CORS(app, origins=[o.strip() for o in _origins.split(",") if o.strip()])
else:
    CORS(app)

app.register_blueprint(auth_bp)

# Build the schema at import time, not only under __main__. WSGI servers
# (PythonAnywhere, gunicorn, waitress) import this module and never call
# main(), so without this the first request dies with "no such table".
try:
    init_db()
except Exception as exc:  # pragma: no cover - startup diagnostics
    print(f"[!] database init failed: {exc}", file=sys.stderr)


# ── Helpers ────────────────────────────────────────────────────────────────

def get_severity(crime_type: str) -> str:
    """Classify a free-text crime type into Critical / High / Medium."""
    u = (crime_type or "").upper()
    if re.search(r"HOMICIDE|KIDNAP|SEXUAL|ARSON|HUMAN TRAF", u):
        return "Critical"
    if re.search(r"ROBBERY|ASSAULT|FIREARM|EXTORT|DRUG|COUNTER|IDENTITY", u):
        return "High"
    return "Medium"


def _body() -> dict:
    """Request payload as a dict; never None, never raises."""
    return request.get_json(silent=True) or {}


def _next_fir_number(conn) -> str:
    """Highest existing FIR number for the current year, plus one.

    Parses numerically instead of assuming every row matches ``FIR-2026-N``,
    which is what made the original version raise ValueError on unexpected data.
    """
    prefix = f"FIR-{datetime.now().year}-"
    best = 0
    for row in conn.execute(
        "SELECT fir_number FROM cases WHERE fir_number LIKE ?", (prefix + "%",)
    ):
        match = re.search(r"(\d+)$", row["fir_number"] or "")
        if match:
            best = max(best, int(match.group(1)))
    return f"{prefix}{best + 1}"


def _next_criminal_id(conn) -> str:
    """Next free ``CRIM-<n>`` id.

    The original sorted the text ids (``ORDER BY id DESC``) which returns
    ``CRIM-99`` for a table holding ``CRIM-100``..``CRIM-110`` and then crashes
    in ``int()``. Numbers are parsed instead.
    """
    best = None
    for row in conn.execute("SELECT id FROM criminals"):
        match = re.fullmatch(r"CRIM-(\d+)", row["id"] or "")
        if match:
            n = int(match.group(1))
            best = n if best is None else max(best, n)
    return f"CRIM-{100 if best is None else best + 1}"


def _exists(conn, table: str, column: str, value) -> bool:
    if value in (None, ""):
        return True
    return (
        conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)).fetchone()
        is not None
    )


# ── Static frontend ────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not FRONTEND_FILE.is_file():
        return jsonify(
            {"error": f"Frontend not found at {FRONTEND_FILE}"}
        ), 500
    return send_from_directory(str(FRONTEND_FILE.parent), FRONTEND_FILE.name)


# ── Access portals ──────────────────────────────────────────────────────────
#
# The three roles sign in through three different URLs, mirroring the way
# each role is given its own front door on the deployed site:
#
#   /login/public   citizen portal
#   /login/police   police officer portal
#   /login/admin    government official portal
#
# All three serve the same single-page document. The frontend reads the path,
# titles the form for that role and refuses an account of a different role.

LOGIN_ROLES = ("public", "police", "admin")


@app.get("/login")
def login_redirect():
    """Send a bare /login to the citizen portal."""
    return redirect("/login/public")


@app.get("/login/<role>")
def role_login(role: str):
    """Serve the application for one of the three role portals."""
    role = (role or "").strip().lower()
    if role not in LOGIN_ROLES:
        return redirect("/login/public")
    return send_from_directory(str(FRONTEND_FILE.parent), FRONTEND_FILE.name)


# ── Static 3D assets (three.js + OrbitControls, vendored) ──────────────────

VENDOR_DIR = FRONTEND_FILE.parent / "vendor"


@app.get("/vendor/<path:filename>")
def vendor_assets(filename: str):
    """Serve the vendored three.js build used by the 3D map.

    The HTML references these as `vendor/...`, i.e. relative to `/`.
    """
    if not VENDOR_DIR.is_dir():
        return jsonify({"error": "3D vendor directory missing"}), 404
    return send_from_directory(str(VENDOR_DIR), filename)


# ── GET /api/stations ──────────────────────────────────────────────────────

@app.get("/api/stations")
def get_stations():
    """Police stations of the South India database.

    Query parameters:
      ``state``      - only this state/UT (e.g. ``Karnataka``)
      ``with_cases`` - ``1`` to keep only stations that hold at least one FIR
      ``lite``       - ``1`` to drop the case counters (map pins, ~3x smaller)
    """
    state = request.args.get("state", "").strip()
    with_cases = request.args.get("with_cases", "").strip() in ("1", "true", "yes")
    lite = request.args.get("lite", "").strip() in ("1", "true", "yes")

    select = ("SELECT ps.id, ps.ps_code, ps.name, ps.district, ps.state, ps.lat, ps.lng,"
              " ps.sector" if lite else
              "SELECT ps.*,"
              " COUNT(c.id) AS total_cases,"
              " COALESCE(SUM(CASE WHEN c.severity='Critical' THEN 1 ELSE 0 END), 0) AS critical_count,"
              " COALESCE(SUM(CASE WHEN c.status='Open'       THEN 1 ELSE 0 END), 0) AS open_count")
    query = f"""
        {select}
        FROM police_stations ps
        LEFT JOIN cases c ON c.station_id = ps.id
        WHERE 1=1
    """
    params: list = []
    if state:
        query += " AND ps.state = ?"
        params.append(state)
    if with_cases:
        query += " AND (SELECT COUNT(*) FROM cases WHERE station_id = ps.id) > 0"
    query += " GROUP BY ps.id ORDER BY ps.state, ps.district, ps.name"

    conn = get_db()
    try:
        return jsonify(conn.execute(query, params).fetchall())
    finally:
        conn.close()


@app.get("/api/stations/summary")
def get_stations_summary():
    """Stations and FIR totals per state - drives the map's state filter."""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT ps.state,
                   COUNT(DISTINCT ps.id)        AS stations,
                   COUNT(c.id)                  AS cases,
                   COALESCE(SUM(CASE WHEN c.severity='Critical' THEN 1 ELSE 0 END), 0) AS critical
            FROM police_stations ps
            LEFT JOIN cases c ON c.station_id = ps.id
            GROUP BY ps.state
            ORDER BY stations DESC
            """
        ).fetchall()
        return jsonify(rows)
    finally:
        conn.close()


# ── GET /api/stations/<id>/cases ───────────────────────────────────────────

@app.get("/api/stations/<int:station_id>/cases")
def get_station_cases(station_id: int):
    severity = request.args.get("severity")
    status = request.args.get("status")
    conn = get_db()
    try:
        query = """
            SELECT c.*,
                   ps.ps_code, ps.name AS station_name, ps.district, ps.state, ps.sector,
                   o.name  AS officer_name,  o.rank  AS officer_rank, o.squad AS officer_squad,
                   cr.name AS criminal_name, cr.age  AS criminal_age,
                   cr.address AS criminal_address, cr.photo_id AS criminal_photo
            FROM cases c
            INNER JOIN police_stations ps ON ps.id = c.station_id
            LEFT  JOIN officers         o  ON o.id  = c.officer_id
            LEFT  JOIN criminals        cr ON cr.id = c.criminal_id
            WHERE c.station_id = ?
        """
        params: list = [station_id]
        if severity:
            query += " AND c.severity = ?"
            params.append(severity)
        if status:
            query += " AND c.status = ?"
            params.append(status)
        query += (
            " ORDER BY CASE c.severity WHEN 'Critical' THEN 1 WHEN 'High' THEN 2"
            " ELSE 3 END, c.status"
        )
        return jsonify(conn.execute(query, params).fetchall())
    finally:
        conn.close()


# ── GET /api/cases ─────────────────────────────────────────────────────────

@app.get("/api/cases")
def get_cases():
    severity = request.args.get("severity")
    status = request.args.get("status")
    state = request.args.get("state")
    ps_code = request.args.get("psCode")
    conn = get_db()
    try:
        query = """
            SELECT c.*, ps.ps_code, ps.name AS station_name,
                   ps.district AS district, ps.state,
                   o.name AS officer_name, cr.name AS criminal_name
            FROM cases c
            INNER JOIN police_stations ps ON ps.id = c.station_id
            LEFT  JOIN officers         o  ON o.id  = c.officer_id
            LEFT  JOIN criminals        cr ON cr.id = c.criminal_id
            WHERE 1=1
        """
        params: list = []
        if severity:
            query += " AND c.severity=?"
            params.append(severity)
        if status:
            query += " AND c.status=?"
            params.append(status)
        if state:
            query += " AND ps.state=?"
            params.append(state)
        if ps_code:
            query += " AND ps.ps_code=?"
            params.append(ps_code)
        # fir_number is TEXT, so plain ORDER BY yields 1, 10, 11, ... 2.
        # LENGTH first restores numeric order (suffixes are not zero-padded).
        query += " ORDER BY LENGTH(c.fir_number), c.fir_number"
        return jsonify(conn.execute(query, params).fetchall())
    finally:
        conn.close()


# ── POST /api/cases ────────────────────────────────────────────────────────

@app.post("/api/cases")
def create_case():
    denied = _require("add_case")
    if denied:
        return denied
    data = _body()
    station_id = data.get("stationId")
    crime_type = str(data.get("crimeType") or "").strip()

    if not isinstance(station_id, int) or not crime_type:
        return jsonify({"error": "stationId (integer) and crimeType are required"}), 400

    officer_id = data.get("officerId") or None
    criminal_id = data.get("criminalId") or None
    status = str(data.get("status") or "Open")
    handling = str(data.get("handling") or "Active")
    investigation_type = str(data.get("investigationType") or "Solo")
    crime_count = _int(data.get("crimeCount"), 1, minimum=1)
    completion_pct = _int(data.get("completionPct"), 0, minimum=0, maximum=100)
    remarks = str(data.get("remarks") or "—")
    arrest_date = str(data.get("arrestDate") or datetime.now().strftime("%Y-%m-%d"))

    conn = get_db()
    try:
        if not _exists(conn, "police_stations", "id", station_id):
            return jsonify({"error": f"Unknown stationId {station_id}"}), 400
        if not _exists(conn, "officers", "id", officer_id):
            return jsonify({"error": f"Unknown officerId {officer_id}"}), 400
        if not _exists(conn, "criminals", "id", criminal_id):
            return jsonify({"error": f"Unknown criminalId {criminal_id}"}), 400

        fir_number = _next_fir_number(conn)
        severity = get_severity(crime_type)
        conn.execute(
            """
            INSERT INTO cases
              (fir_number, station_id, officer_id, criminal_id, severity, crime_type,
               crime_count, status, handling, investigation_type, completion_pct,
               remarks, arrest_date, photo_resolution, assign_id, case_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'1920x1080','ASN-NEW','CASE-NEW')
            """,
            (
                fir_number, station_id, officer_id, criminal_id, severity, crime_type,
                crime_count, status, handling, investigation_type, completion_pct,
                remarks, arrest_date,
            ),
        )
        conn.commit()

        station_name = conn.execute(
            "SELECT name, district, state FROM police_stations WHERE id = ?",
            (station_id,),
        ).fetchone()

        # Notifications are best-effort: a mail failure must never lose the FIR.
        reporter = str(data.get("reporterEmail") or "").strip()
        mailed = {"confirmation": False, "officerAlert": False}
        try:
            if reporter:
                mailed["confirmation"] = send_case_confirmation(
                    reporter, fir_number, crime_type,
                    station_name["name"] if station_name else str(station_id), status)
            mailed["officerAlert"] = send_case_alert_to_officials(
                fir_number, crime_type, severity,
                station_name["name"] if station_name else str(station_id),
                station_name["district"] if station_name else "-",
                station_name["state"] if station_name else "-",
                reporter or "an on-duty officer")
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("case notification failed: %s", exc)

        return jsonify(
            {
                "firNumber": fir_number,
                "stationId": station_id,
                "crimeType": crime_type,
                "severity": severity,
                "status": status,
                "mailed": mailed,
            }
        ), 201
    finally:
        conn.close()


# ── GET /api/stats ─────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*)                                                AS total_cases,
                   (SELECT COUNT(*) FROM police_stations)                  AS total_stations,
                   COALESCE(SUM(CASE WHEN severity='Critical' THEN 1 ELSE 0 END), 0) AS critical_cases,
                   COALESCE(SUM(CASE WHEN status='Open'       THEN 1 ELSE 0 END), 0) AS open_cases,
                   COALESCE(SUM(CASE WHEN status='Closed'     THEN 1 ELSE 0 END), 0) AS closed_cases
            FROM cases
            """
        ).fetchone()
        return jsonify(row)
    finally:
        conn.close()


# ── GET /api/officers ──────────────────────────────────────────────────────

@app.get("/api/officers")
def get_officers():
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT o.*,
                   COUNT(c.id)                                             AS total_cases,
                   COALESCE(SUM(CASE WHEN c.severity='Critical' THEN 1 ELSE 0 END), 0) AS critical_cases,
                   COALESCE(SUM(CASE WHEN c.status='Open'       THEN 1 ELSE 0 END), 0) AS open_cases,
                   COALESCE(SUM(CASE WHEN c.status='Closed'     THEN 1 ELSE 0 END), 0) AS closed_cases
            FROM officers o
            LEFT JOIN cases c ON c.officer_id = o.id
            GROUP BY o.id
            ORDER BY total_cases DESC
            """
        ).fetchall()
        return jsonify(rows)
    finally:
        conn.close()


# ── GET /api/officers/<id>/cases ───────────────────────────────────────────

@app.get("/api/officers/<officer_id>/cases")
def get_officer_cases(officer_id: str):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT c.fir_number, c.crime_type, c.severity, c.status,
                   c.completion_pct, c.handling, c.arrest_date,
                   ps.name AS station_name, ps.ps_code, ps.district, ps.state,
                   cr.name AS criminal_name, cr.id AS criminal_id
            FROM cases c
            INNER JOIN police_stations ps ON ps.id = c.station_id
            LEFT  JOIN criminals        cr ON cr.id = c.criminal_id
            WHERE c.officer_id = ?
            ORDER BY CASE c.severity WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 ELSE 3 END
            """,
            (officer_id,),
        ).fetchall()
        return jsonify(rows)
    finally:
        conn.close()


# ── GET /api/criminals ─────────────────────────────────────────────────────

@app.get("/api/criminals")
def get_criminals():
    q = request.args.get("q", "").strip()
    conn = get_db()
    try:
        query = """
            SELECT cr.*,
                   COUNT(c.id)                  AS fir_count,
                   COUNT(DISTINCT c.station_id) AS station_count
            FROM criminals cr
            LEFT JOIN cases c ON c.criminal_id = cr.id
            WHERE 1=1
        """
        params: list = []
        if q:
            query += " AND (cr.name LIKE ? OR cr.id LIKE ? OR cr.address LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like, like])
        query += " GROUP BY cr.id ORDER BY fir_count DESC"
        return jsonify(conn.execute(query, params).fetchall())
    finally:
        conn.close()


@app.get("/api/districts")
def get_districts():
    """District roll-up of the South India database.

    Query params:
      ``state`` - restrict to one state/UT
      ``q``     - substring match on the district name
      ``min_stations`` - drop tiny districts (default 1)

    Each row carries the station count, the FIR totals and the number of
    tracked criminals whose cases were filed inside that district, plus the
    bounding box used to fly the camera there.
    """
    state = request.args.get("state", "").strip()
    q = request.args.get("q", "").strip()
    try:
        min_stations = max(1, int(request.args.get("min_stations", 1)))
    except ValueError:
        min_stations = 1

    query = """
        SELECT s.state, s.district, s.stations, s.cases, s.critical,
               COALESCE(k.criminals, 0) AS criminals,
               s.lat, s.lng, s.min_lat, s.max_lat, s.min_lng, s.max_lng
        FROM (
            SELECT ps.state, ps.district,
                   COUNT(DISTINCT ps.id)   AS stations,
                   COUNT(c.id)             AS cases,
                   COALESCE(SUM(CASE WHEN c.severity='Critical' THEN 1 ELSE 0 END), 0) AS critical,
                   AVG(ps.lat) AS lat,  AVG(ps.lng) AS lng,
                   MIN(ps.lat) AS min_lat, MAX(ps.lat) AS max_lat,
                   MIN(ps.lng) AS min_lng, MAX(ps.lng) AS max_lng
            FROM police_stations ps
            LEFT JOIN cases c ON c.station_id = ps.id
            WHERE 1=1
    """
    params: list = []
    if state:
        query += " AND ps.state = ?"
        params.append(state)
    if q:
        query += " AND ps.district LIKE ?"
        params.append(f"%{q}%")
    query += """
            GROUP BY ps.state, ps.district
            HAVING stations >= ?
        ) s
        LEFT JOIN (
            SELECT ps.state, ps.district, COUNT(DISTINCT cr.id) AS criminals
            FROM cases c
            INNER JOIN police_stations ps ON ps.id = c.station_id
            INNER JOIN criminals        cr ON cr.id = c.criminal_id
            GROUP BY ps.state, ps.district
        ) k ON k.state = s.state AND k.district = s.district
        ORDER BY s.cases DESC, s.stations DESC
    """
    params.append(min_stations)

    conn = get_db()
    try:
        return jsonify(conn.execute(query, params).fetchall())
    finally:
        conn.close()


# ── GET /api/relations ──────────────────────────────────────────────────────

@app.get("/api/relations")
def get_relations():
    """The criminal relationship graph.

    Query parameters:
      ``criminal`` - only links touching this criminal id
      ``kind``     - ``officer`` | ``station`` | ``city`` | ``crime``
      ``min``      - minimum strength (default 1)
      ``limit``    - cap the number of links (default 1200)
    """
    criminal = request.args.get("criminal", "").strip()
    kind = request.args.get("kind", "").strip()
    try:
        min_strength = max(1, int(request.args.get("min", 1)))
    except ValueError:
        min_strength = 1
    try:
        limit = min(5000, max(1, int(request.args.get("limit", 1200))))
    except ValueError:
        limit = 1200

    query = """
        SELECT l.a, l.b, l.kind, l.strength, l.label,
               ca.name AS a_name, ca.city AS a_city, ca.lat AS a_lat, ca.lng AS a_lng,
               cb.name AS b_name, cb.city AS b_city, cb.lat AS b_lat, cb.lng AS b_lng
        FROM criminal_links l
        LEFT JOIN criminals ca ON ca.id = l.a
        LEFT JOIN criminals cb ON cb.id = l.b
        WHERE l.strength >= ?
    """
    params: list = [min_strength]
    if criminal:
        query += " AND (l.a = ? OR l.b = ?)"
        params.extend([criminal, criminal])
    if kind:
        query += " AND l.kind = ?"
        params.append(kind)
    query += " ORDER BY l.strength DESC, l.kind LIMIT ?"
    params.append(limit)

    conn = get_db()
    try:
        return jsonify(conn.execute(query, params).fetchall())
    finally:
        conn.close()


# ── GET /api/criminals/<id> ────────────────────────────────────────────────

@app.get("/api/criminals/<criminal_id>")
def get_criminal(criminal_id: str):
    conn = get_db()
    try:
        criminal = conn.execute(
            "SELECT * FROM criminals WHERE id = ?", (criminal_id,)
        ).fetchone()
        if not criminal:
            return jsonify({"error": "Criminal not found"}), 404

        cases = conn.execute(
            """
            SELECT c.fir_number, c.crime_type, c.severity, c.status,
                   c.completion_pct, c.handling, c.investigation_type, c.arrest_date,
                   ps.name AS station_name, ps.ps_code, ps.district, ps.state,
                   ps.lat, ps.lng,
                   o.name AS officer_name, o.rank AS officer_rank
            FROM cases c
            INNER JOIN police_stations ps ON ps.id = c.station_id
            LEFT  JOIN officers         o  ON o.id  = c.officer_id
            WHERE c.criminal_id = ?
            ORDER BY c.arrest_date DESC
            """,
            (criminal_id,),
        ).fetchall()

        stations = conn.execute(
            """
            SELECT ps.*, COUNT(c.id) AS fir_count
            FROM police_stations ps
            LEFT JOIN cases c ON c.station_id = ps.id AND c.criminal_id = ?
            GROUP BY ps.id
            HAVING fir_count > 0
            ORDER BY fir_count DESC
            """,
            (criminal_id,),
        ).fetchall()

        links = conn.execute(
            """
            SELECT l.a, l.b, l.kind, l.strength, l.label,
                   other.name, other.city, other.crime_type, other.status,
                   other.lat, other.lng
            FROM criminal_links l
            JOIN criminals other ON other.id = CASE WHEN l.a = ? THEN l.b ELSE l.a END
            WHERE l.a = ? OR l.b = ?
            ORDER BY l.strength DESC
            """,
            (criminal_id, criminal_id, criminal_id),
        ).fetchall()

        return jsonify({"criminal": criminal, "cases": cases,
                        "stations": stations, "relations": links})
    finally:
        conn.close()


# ── POST /api/criminals ────────────────────────────────────────────────────

@app.post("/api/criminals")
def create_criminal():
    denied = _require("add")
    if denied:
        return denied
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    if len(name) > 200:
        return jsonify({"error": "Name must be 200 characters or fewer"}), 400

    age = _int(data.get("age"), 25, minimum=0, maximum=150)
    address = str(data.get("address") or "").strip() or "Unknown"
    alias = str(data.get("alias") or "").strip()
    photo_id = str(data.get("photoId") or "").strip()

    conn = get_db()
    try:
        new_id = _next_criminal_id(conn)
        if not photo_id:
            number = re.search(r"(\d+)$", new_id)
            photo_id = f"IMG-{int(number.group(1)) - 99:04d}" if number else "IMG-0000"

        conn.execute(
            "INSERT INTO criminals (id,name,age,address,photo_id,alias) "
            "VALUES (?,?,?,?,?,?)",
            (new_id, name, age, address, photo_id, alias),
        )
        conn.commit()
        return jsonify(
            {
                "id": new_id,
                "name": name,
                "age": age,
                "address": address,
                "photoId": photo_id,
                "alias": alias,
            }
        ), 201
    finally:
        conn.close()


# ── GET /api/analytics ─────────────────────────────────────────────────────

@app.get("/api/analytics")
def get_analytics():
    conn = get_db()
    try:
        totals = conn.execute(
            """
            SELECT COUNT(*)                                                AS total,
                   COALESCE(SUM(CASE WHEN severity='Critical' THEN 1 ELSE 0 END), 0) AS crit,
                   COALESCE(SUM(CASE WHEN severity='High'     THEN 1 ELSE 0 END), 0) AS hi,
                   COALESCE(SUM(CASE WHEN severity='Medium'   THEN 1 ELSE 0 END), 0) AS med,
                   COALESCE(SUM(CASE WHEN status='Open'                THEN 1 ELSE 0 END), 0) AS opn,
                   COALESCE(SUM(CASE WHEN status='Closed'              THEN 1 ELSE 0 END), 0) AS cls,
                   COALESCE(SUM(CASE WHEN status='Under Investigation' THEN 1 ELSE 0 END), 0) AS unv
            FROM cases
            """
        ).fetchone()

        by_state = {
            r["state"]: r["count"]
            for r in conn.execute(
                """
                SELECT ps.state, COUNT(c.id) AS count
                FROM cases c INNER JOIN police_stations ps ON ps.id = c.station_id
                GROUP BY ps.state ORDER BY count DESC
                """
            ).fetchall()
        }
        by_type = {
            r["crime_type"]: r["count"]
            for r in conn.execute(
                """
                SELECT crime_type, COUNT(*) AS count
                FROM cases GROUP BY crime_type ORDER BY count DESC LIMIT 10
                """
            ).fetchall()
        }
        counts = {
            key: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for key, table in (
                ("totalStations", "police_stations"),
                ("totalOfficers", "officers"),
                ("totalCriminals", "criminals"),
            )
        }
        return jsonify(
            {
                "totalCases": totals["total"],
                **counts,
                "criticalCases": totals["crit"],
                "highCases": totals["hi"],
                "mediumCases": totals["med"],
                "openCases": totals["opn"],
                "closedCases": totals["cls"],
                "underInvCases": totals["unv"],
                "byState": by_state,
                "byType": by_type,
            }
        )
    finally:
        conn.close()


# ── JSON error handlers (the original returned HTML error pages) ───────────

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(409)
@app.errorhandler(422)
def _http_error(err):
    """Flask passes the HTTPException instance, not the bare status code."""
    status = getattr(err, "code", None)
    if not isinstance(status, int):
        status = 500
    message = _MESSAGE.get(status, "Error")
    description = getattr(err, "description", None)
    if description and description != _MESSAGE.get(status):
        message = description
    return jsonify({"error": {"status": status, "message": message}}), status


@app.errorhandler(500)
def _server_error(_error):
    app.logger.exception("Unhandled server error")
    return jsonify({"error": {"status": 500, "message": "Internal server error"}}), 500


_MESSAGE = {
    400: "Bad request",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    422: "Unprocessable entity",
}


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    print("\n🔍 CRIME SIGHT NOIR — Flask backend")
    print(f"   Database : {DB_PATH}")
    init_db()
    print(f"\n   Server   : http://{HOST}:{PORT}")
    print(f"   Frontend : http://{HOST}:{PORT}/")
    print("\n   POST /api/auth/login      POST /api/auth/signup")
    print("   GET  /api/stations        GET  /api/stations/<id>/cases")
    print("   GET  /api/cases           POST /api/cases")
    print("   GET  /api/stats           GET  /api/analytics")
    print("   GET  /api/officers        GET  /api/officers/<id>/cases")
    print("   GET  /api/criminals       GET  /api/criminals/<id>")
    print("   POST /api/criminals\n")
    app.run(host=HOST, port=PORT, debug=DEBUG)


# ── ADMIN WRITE ENDPOINTS ───────────────────────────────────────────────────
# The session token is the opaque value handed out by /api/auth/login. Each
# write below checks the caller's role before touching the database:
#   police -> add + edit,  admin -> add + edit + delete.

def _role_of():
    """Read the bearer token and resolve it to a role, or None."""
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else None
    token = token or request.headers.get("X-Session-Token")
    if not token:
        return None
    # token format: csn-<role>-<hex>
    parts = token.split("-")
    if len(parts) < 3 or parts[0] != "csn":
        return None
    role = parts[1]
    return role if role in ("public", "police", "admin") else None


def _require(action):
    role = _role_of()
    table = {
        # A Public account may only view records. Officers, criminals, FIRs
        # and user records are closed to them.
        "add_case": ("police", "admin"),
        "add": ("police", "admin"),
        "edit": ("police", "admin"),
        "delete": ("admin",),
        "admin": ("admin",),
    }[action]
    if role is None:
        return jsonify({"error": "Authentication required"}), 401
    if role not in table:
        return jsonify({"error": f"Role '{role}' may not {action} records"}), 403
    return None


def _no_id():
    return jsonify({"error": "Record not found"}), 404


# ── officers ───────────────────────────────────────────────────────────────

@app.post("/api/officers")
def create_officer():
    denied = _require("add")
    if denied:
        return denied
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    rank = str(data.get("rank") or "").strip() or "Inspector"
    squad = str(data.get("squad") or "").strip() or "General"

    conn = get_db()
    try:
        base, n = "OFF-ADM", 1
        while _exists(conn, "officers", "id", f"{base}-{n:04d}"):
            n += 1
        new_id = f"{base}-{n:04d}"
        conn.execute(
            "INSERT INTO officers (id, name, squad, rank) VALUES (?,?,?,?)",
            (new_id, name, squad, rank),
        )
        conn.commit()
        return jsonify({"id": new_id, "name": name, "squad": squad, "rank": rank}), 201
    finally:
        conn.close()


@app.put("/api/officers/<officer_id>")
def update_officer(officer_id):
    denied = _require("edit")
    if denied:
        return denied
    data = _body()
    conn = get_db()
    try:
        if not _exists(conn, "officers", "id", officer_id):
            return _no_id()
        sets, vals = [], []
        for col, key in (("name", "name"), ("squad", "squad"), ("rank", "rank")):
            if key in data:
                sets.append(f"{col} = ?")
                vals.append(str(data[key] or "").strip())
        if not sets:
            return jsonify({"error": "No editable fields supplied"}), 400
        vals.append(officer_id)
        conn.execute(f"UPDATE officers SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM officers WHERE id = ?", (officer_id,)).fetchone()
        return jsonify(dict(row))
    finally:
        conn.close()


@app.delete("/api/officers/<officer_id>")
def delete_officer(officer_id):
    denied = _require("delete")
    if denied:
        return denied
    conn = get_db()
    try:
        if not _exists(conn, "officers", "id", officer_id):
            return _no_id()
        open_cases = len(conn.execute(
            "SELECT id FROM cases WHERE officer_id = ? AND status != 'Closed'",
            (officer_id,),
        ).fetchall())
        if open_cases:
            return jsonify({
                "error": f"{officer_id} still handles {open_cases} open case(s); "
                         "close or reassign them first"
            }), 409
        conn.execute("UPDATE cases SET officer_id = NULL WHERE officer_id = ?", (officer_id,))
        conn.execute("DELETE FROM officers WHERE id = ?", (officer_id,))
        conn.commit()
        return jsonify({"deleted": officer_id})
    finally:
        conn.close()


# ── criminals ──────────────────────────────────────────────────────────────

@app.put("/api/criminals/<criminal_id>")
def update_criminal(criminal_id):
    denied = _require("edit")
    if denied:
        return denied
    data = _body()
    editable = ("name", "age", "address", "alias", "gender", "city", "state",
                "crime_type", "status", "station", "officer", "case_ref")
    conn = get_db()
    try:
        if not _exists(conn, "criminals", "id", criminal_id):
            return _no_id()
        sets, vals = [], []
        for col in editable:
            if col in data:
                sets.append(f"{col} = ?")
                v = data[col]
                if col == "age":
                    v = _int(v, None)
                vals.append(v)
        if not sets:
            return jsonify({"error": "No editable fields supplied"}), 400
        vals.append(criminal_id)
        conn.execute(f"UPDATE criminals SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM criminals WHERE id = ?", (criminal_id,)).fetchone()
        return jsonify(dict(row))
    finally:
        conn.close()


@app.delete("/api/criminals/<criminal_id>")
def delete_criminal(criminal_id):
    denied = _require("delete")
    if denied:
        return denied
    conn = get_db()
    try:
        if not _exists(conn, "criminals", "id", criminal_id):
            return _no_id()
        open_cases = len(conn.execute(
            "SELECT id FROM cases WHERE criminal_id = ? AND status != 'Closed'",
            (criminal_id,),
        ).fetchall())
        if open_cases:
            return jsonify({
                "error": f"{criminal_id} is named in {open_cases} open case(s); "
                         "close or reassign them first"
            }), 409
        # drop the relationship graph edges that touch this record
        conn.execute("DELETE FROM criminal_links WHERE a = ? OR b = ?",
                     (criminal_id, criminal_id))
        conn.execute("UPDATE cases SET criminal_id = NULL WHERE criminal_id = ?",
                     (criminal_id,))
        conn.execute("DELETE FROM criminals WHERE id = ?", (criminal_id,))
        conn.commit()
        return jsonify({"deleted": criminal_id})
    finally:
        conn.close()


# ── cases / FIRs ───────────────────────────────────────────────────────────

@app.put("/api/cases/<int:case_id>")
def update_case(case_id):
    denied = _require("edit")
    if denied:
        return denied
    data = _body()
    conn = get_db()
    try:
        if not _exists(conn, "cases", "id", case_id):
            return _no_id()

        crime_type = data.get("crimeType")
        status = data.get("status")
        # severity is derived from the crime type unless one was pinned
        severity = data.get("severity") or (get_severity(crime_type) if crime_type else None)

        sets, vals = [], []
        for col, key, kind in (
            ("crime_type", "crimeType", "text"),
            ("severity", "severity", "text"),
            ("status", "status", "text"),
            ("handling", "handling", "text"),
            ("investigation_type", "investigationType", "text"),
            ("remarks", "remarks", "text"),
            ("arrest_date", "arrestDate", "text"),
            ("completion_pct", "completionPct", "pct"),
            ("crime_count", "crimeCount", "cnt"),
            ("transfers", "transfers", "cnt"),
            ("station_id", "stationId", "int"),
            ("officer_id", "officerId", "text"),
            ("criminal_id", "criminalId", "text"),
        ):
            if key not in data:
                continue
            v = data[key]
            if kind == "pct":
                v = _int(v, 0, minimum=0, maximum=100)
            elif kind == "cnt":
                v = _int(v, 1, minimum=1)
            elif kind == "int":
                v = _int(v, None)
                if v is not None and not _exists(conn, "police_stations", "id", v):
                    return jsonify({"error": f"Unknown stationId {v}"}), 400
            else:
                v = str(v or "").strip()
            if col == "severity" and severity is None:
                continue
            sets.append(f"{col} = ?")
            vals.append(v)
        if not sets:
            return jsonify({"error": "No editable fields supplied"}), 400
        vals.append(case_id)
        conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return jsonify(dict(row))
    finally:
        conn.close()


@app.delete("/api/cases/<int:case_id>")
def delete_case(case_id):
    denied = _require("delete")
    if denied:
        return denied
    conn = get_db()
    try:
        row = conn.execute("SELECT fir_number FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            return _no_id()
        conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
        conn.commit()
        return jsonify({"deleted": case_id, "firNumber": row["fir_number"]})
    finally:
        conn.close()


# ── USER DIRECTORY (admin only) ────────────────────────────────────────────
# Every account that has signed up, with the role it holds and the post it
# fills. Admin-only: the whole point of the page is oversight.

@app.get("/api/public-registry")
def public_registry():
    """Registry of Public accounts only.

    Available to Police Officers and Govt Officials. It deliberately exposes
    ONLY rows where role = 'public': Police and Govt Official accounts are
    never returned, so an officer cannot enumerate the department's own
    staff accounts through this route.
    """
    denied = _require("edit")      # police + admin
    if denied:
        return denied
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT username, display, email, phone, state, district
            FROM users WHERE role = 'public'
            ORDER BY username
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # A phone number is contact detail, not intelligence - omit it.
            d.pop("phone", None)
            out.append(d)
        by_state = {}
        for d in out:
            by_state[d.get("state") or "Unspecified"] = \
                by_state.get(d.get("state") or "Unspecified", 0) + 1
        return jsonify({
            "total": len(out),
            "byState": [{"state": k, "count": v}
                        for k, v in sorted(by_state.items(), key=lambda kv: -kv[1])],
            "users": out,
        })
    finally:
        conn.close()


@app.get("/api/users")
def list_users():
    denied = _require("admin")
    if denied:
        return denied
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT username, role, display, badge, rank, squad, official_id,
                   station_code, station_name, district, state, department,
                   zone, email, phone
            FROM users ORDER BY role, username
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["permissions"] = {
                "public": ["view"],
                "police": ["view", "add", "edit"],
                "admin": ["view", "add", "edit", "delete"],
            }.get(d.get("role"), [])
            # never expose anything password-shaped
            d.pop("password", None)
            out.append(d)
        return jsonify(out)
    finally:
        conn.close()


@app.put("/api/users/<username>")
def update_user(username):
    denied = _require("admin")
    if denied:
        return denied
    data = _body()
    editable = ("display", "role", "badge", "rank", "squad", "official_id",
                "station_code", "station_name", "district", "state",
                "department", "zone", "email", "phone")
    if "role" in data and data["role"] not in ("public", "police", "admin"):
        return jsonify({"error": "role must be public, police or admin"}), 400

    conn = get_db()
    try:
        if not _exists(conn, "users", "username", username):
            return jsonify({"error": f"No such user '{username}'"}), 404

        # an admin must not be able to delete or demote the last admin
        if username.lower() == "admin" and str(data.get("role", "")).lower() != "admin" \
                and "role" in data:
            others = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND username != ?",
                (username,),
            ).fetchone()
            if not others or others["n"] == 0:
                return jsonify({"error": "Cannot demote the last remaining admin"}), 409

        sets, vals = [], []
        for col in editable:
            if col in data:
                sets.append(f"{col} = ?")
                vals.append(str(data[col] or "").strip())
        if not sets:
            return jsonify({"error": "No editable fields supplied"}), 400
        vals.append(username)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE username = ?", vals)
        conn.commit()
        row = conn.execute(
            "SELECT username, role, display, badge, rank, squad, official_id,"
            " station_code, station_name, district, state, department, zone,"
            " email, phone FROM users WHERE username = ?", (username,)
        ).fetchone()
        return jsonify(dict(row))
    finally:
        conn.close()


@app.delete("/api/users/<username>")
def delete_user(username):
    denied = _require("admin")
    if denied:
        return denied
    conn = get_db()
    try:
        if not _exists(conn, "users", "username", username):
            return jsonify({"error": f"No such user '{username}'"}), 404
        if username.lower() == "admin":
            others = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND username != ?",
                (username,),
            ).fetchone()
            if not others or others["n"] == 0:
                return jsonify({"error": "Cannot delete the last remaining admin"}), 409
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return jsonify({"deleted": username})
    finally:
        conn.close()


def _int(value, default, minimum=None, maximum=None):
    """Coerce a client-supplied value to an int, clamped to a range."""
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and n < minimum:
        return minimum
    if maximum is not None and n > maximum:
        return maximum
    return n


if __name__ == "__main__":
    main()

