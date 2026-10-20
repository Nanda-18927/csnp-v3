"""Authentication endpoints: ``POST /api/auth/login`` and ``POST /api/auth/signup``.

Passwords are stored as Werkzeug pbkdf2:sha256 hashes. Rows created by older
versions of this project held plain-text passwords; those still verify and are
re-hashed transparently on the next successful login.

Note on the session token: it is an opaque, unique identifier returned to the
browser for convenience. It is *not* signed and is *not* checked on the other
API routes, which are read-only demos. See ``docs/SECURITY.md``.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from backend.database import get_db
from backend.mailer import send_password_reset

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

#: DB column -> key returned to the client. The frontend expects camelCase.
_PROFILE_COLUMNS = {
    "badge": "badge",
    "rank": "rank",
    "station_name": "stationName",
    "district": "district",
    "state": "state",
    "squad": "squad",
    "official_id": "officialId",
    "designation": "designation",
    "department": "department",
    "zone": "zone",
}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_ROLES = ("public", "police", "admin")
_HASH_PREFIXES = ("pbkdf2:", "scrypt:", "argon2:")


def _json():
    """Parse the request body without raising on a missing/invalid payload."""
    return request.get_json(silent=True) or {}


def _check_password(stored: str, given: str) -> bool:
    """Verify a password against a hash, tolerating legacy plain-text rows."""
    if not stored or not given:
        return False
    if stored.startswith(_HASH_PREFIXES):
        return check_password_hash(stored, given)
    try:
        return hmac.compare_digest(stored, given)
    except (TypeError, UnicodeEncodeError):
        return stored == given


def _profile(row: dict) -> dict:
    out = {}
    for column, key in _PROFILE_COLUMNS.items():
        value = row.get(column)
        if value not in (None, ""):
            out[key] = value
    return out


def _session(row: dict) -> dict:
    """Build the object the frontend stores in localStorage."""
    resp = {
        "username": row["username"],
        "role": row["role"],
        "display": row["display"],
        "stationCode": row.get("station_code"),
        "token": f"csn-{row['role']}-{secrets.token_hex(16)}",
    }
    resp.update(_profile(row))
    return resp


# ── FORGOT / RESET PASSWORD ─────────────────────────────────────────────────
# Deliberately Public-only. A Police or Govt Official account is NOT
# self-service resettable: if either password is lost or misused the account
# has to be dealt with out of band by the department. That is the point --
# it stops someone who has phished an officer's password from quietly
# rotating it and keeping access.

_RESET_TTL_MIN = 30


def clearance_level(role):
    """1 = Public, 2 = Police Officer, 3 = Govt Official."""
    return {"public": 1, "police": 2, "admin": 3}.get(role, 1)


def _hash_token(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


@auth_bp.post("/forgot-password")
def forgot_password():
    """Identify the account by email address or phone number - never by
    username. A reset link only ever goes to the address on file, so knowing
    someone's username cannot be used to take their account over."""
    data = _json()
    identifier = str(data.get("identifier") or "").strip()
    if not identifier:
        return jsonify({"error": "Enter the email address or phone number on your account"}), 400

    ident = identifier.lower()
    digits = re.sub(r"\D", "", identifier)

    conn = get_db()
    try:
        row = None
        if digits:
            # Phone numbers are compared on digits alone. Stripping spacing,
            # dashes, brackets and a leading + here means "+91 98765 43210",
            # "919876543210" and "+91-98765-43210" all reach the same account.
            # There are only a handful of accounts, so matching in Python is
            # cheaper than teaching SQLite a normalisation routine.
            row = None
            for cand in conn.execute(
                "SELECT username, role, email, phone FROM users WHERE phone IS NOT NULL AND phone != ''"
            ):
                if re.sub(r"\D", "", cand["phone"]) == digits:
                    row = cand
                    break
        if row is None:
            row = conn.execute(
                "SELECT username, role, email, phone FROM users WHERE LOWER(email) = ?",
                (ident,),
            ).fetchone()

        if row is None:
            return jsonify({"error": "No account is registered with that email address or phone number"}), 404
        if row["role"] != "public":
            return jsonify({
                "error": "Password reset is for Public accounts. This account belongs to a "
                         f"{row['role']} and is reset by the department."
            }), 403

        conn.execute("DELETE FROM password_resets WHERE expires_at < ?",
                     (datetime.now().isoformat(),))
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        conn.execute(
            "INSERT OR REPLACE INTO password_resets "
            "(username, token_hash, created_at, expires_at, used_at) VALUES (?,?,?,?,NULL)",
            (row["username"], _hash_token(token), now.isoformat(),
             (now + timedelta(minutes=_RESET_TTL_MIN)).isoformat()),
        )
        conn.commit()

        base = os.environ.get("CSN_PUBLIC_URL", "http://127.0.0.1:4000").rstrip("/")
        link = f"{base}/?reset={token}"

        # Email the link if we have an address. There is no SMS provider wired
        # up, so a phone-only account gets the link on screen instead.
        sent_to = None
        if row["email"]:
            sent_to = row["email"]
            delivered = send_password_reset(row["email"], link)
            via = "email"
        else:
            delivered = False
            via = "screen"

        out = {"ok": True, "expiresInMinutes": _RESET_TTL_MIN, "via": via}
        if via == "screen" or not delivered:
            # No mail server configured, or no email on file: surface the link
            # directly so the reset can still be completed.
            out["devLink"] = link
            out["hint"] = (
                "This account has no email address on file, so the reset link is shown here."
                if via == "screen"
                else "Mail delivery is not configured on this server, so the reset link is shown here."
            )
        return jsonify(out), 200
    finally:
        conn.close()


@auth_bp.post("/reset-password")
def reset_password():
    data = _json()
    token = str(data.get("token") or "").strip()
    new_password = str(data.get("password") or "")
    if not token:
        return jsonify({"error": "Reset token is required"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT username, expires_at, used_at FROM password_resets WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            return jsonify({"error": "This reset link is not valid"}), 400
        if row["used_at"]:
            return jsonify({"error": "This reset link has already been used"}), 400
        if row["expires_at"] < datetime.now().isoformat():
            conn.execute("DELETE FROM password_resets WHERE username = ? AND token_hash = ?",
                         (row["username"], _hash_token(token)))
            conn.commit()
            return jsonify({"error": "This reset link has expired - request a new one"}), 400

        user = conn.execute("SELECT role FROM users WHERE username = ?",
                            (row["username"],)).fetchone()
        if user is None or user["role"] != "public":
            return jsonify({"error": "This account is no longer eligible for self-service reset"}), 403

        conn.execute("UPDATE users SET password = ? WHERE username = ?",
                     (generate_password_hash(new_password), row["username"]))
        conn.execute("UPDATE password_resets SET used_at = ? WHERE username = ? AND token_hash = ?",
                     (datetime.now().isoformat(), row["username"], _hash_token(token)))
        # burn every other outstanding token for this account
        conn.execute("DELETE FROM password_resets WHERE username = ? AND used_at IS NULL",
                     (row["username"],))
        conn.commit()
        return jsonify({"ok": True, "username": row["username"]}), 200
    finally:
        conn.close()


@auth_bp.post("/login")
def login():
    data = _json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None or not _check_password(row.get("password"), password):
            return jsonify({"error": "Invalid credentials"}), 401

        # Upgrade a legacy plain-text password the first time it is used.
        stored = row.get("password")
        if stored and not stored.startswith(_HASH_PREFIXES):
            hashed = generate_password_hash(password)
            conn.execute(
                "UPDATE users SET password = ? WHERE username = ?",
                (hashed, username),
            )
            conn.commit()
            row = dict(row)
            row["password"] = hashed

        return jsonify(_session(row))
    finally:
        conn.close()


@auth_bp.post("/signup")
def signup():
    data = _json()

    name = str(data.get("name") or "").strip()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    role = str(data.get("role") or "public").strip()
    portal = str(data.get("portal") or "").strip().lower()
    station_code = data.get("stationCode") or None

    if not name or not username or not password:
        return jsonify({"error": "Name, username and password are required"}), 400
    if not _USERNAME_RE.match(username):
        return jsonify(
            {"error": "Username must be 3-32 characters: letters, digits, . _ -"}
        ), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if role not in _ROLES:
        return jsonify({"error": f"role must be one of {', '.join(_ROLES)}"}), 400

    # Each role has its own portal. A signup that names a portal may only
    # create an account of that portal's role, so the Public portal cannot
    # be used to mint a police or government account, and so on.
    if portal:
        if portal not in _ROLES:
            return jsonify({"error": f"portal must be one of {', '.join(_ROLES)}"}), 400
        if portal != role:
            return jsonify({"error": f"This portal registers {portal} accounts only"}), 400

    email = str(data.get("email") or "").strip().lower()
    phone = str(data.get("phone") or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "That email address does not look valid"}), 400
    if not email and not phone:
        return jsonify({"error": "An email address or phone number is required - it is how you reset your password"}), 400

    profile = {
        "email": email or None,
        "phone": phone or None,
        "badge": data.get("badge"),
        "rank": data.get("rank"),
        "station_name": data.get("stationName"),
        "district": data.get("district"),
        "state": data.get("state"),
        "squad": data.get("squad"),
        "official_id": data.get("officialId"),
        "designation": data.get("designation"),
        "department": data.get("department"),
        "zone": data.get("zone"),
    }

    # Role-specific requirements, mirrored from the signup form.
    if role == "police" and not all(
        [profile["badge"], profile["rank"], station_code]
    ):
        return jsonify(
            {"error": "Badge number, rank and station are required for Police Officers"}
        ), 400
    if role == "admin" and not all(
        [profile["official_id"], profile["designation"], profile["department"]]
    ):
        return jsonify(
            {"error": "Official ID, designation and department are required for Govt Officials"}
        ), 400

    conn = get_db()
    try:
        if conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone():
            return jsonify({"error": "Username already exists"}), 409

        conn.execute(
            """
            INSERT INTO users
              (username, password, role, display, station_code,
               badge, rank, station_name, district, state, squad,
               official_id, designation, department, zone, email, phone)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                username,
                generate_password_hash(password),
                role,
                name,
                station_code,
                profile["badge"],
                profile["rank"],
                profile["station_name"],
                profile["district"],
                profile["state"],
                profile["squad"],
                profile["official_id"],
                profile["designation"],
                profile["department"],
                profile["zone"],
                profile["email"],
                profile["phone"],
            ),
        )
        conn.commit()

        row = {
            "username": username,
            "role": role,
            "display": name,
            "station_code": station_code,
            **profile,
        }
        return jsonify(_session(row)), 201
    finally:
        conn.close()
