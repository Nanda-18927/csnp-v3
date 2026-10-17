"""Outbound email.

Configuration is read from the environment (or a .env file next to the
project root):

    CSN_SMTP_HOST     smtp.gmail.com
    CSN_SMTP_PORT     587
    CSN_SMTP_USER     you@example.com
    CSN_SMTP_PASS     (use an APP PASSWORD, not your real password)
    CSN_SMTP_FROM     you@example.com
    CSN_PUBLIC_URL    http://127.0.0.1:4000

If CSN_SMTP_HOST is unset the senders below fall back to printing the
message to the console and report `delivered=False`, so the whole flow
remains testable without a mail account.
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env reader - avoids adding a dependency for four settings."""
    env_file = _ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

# Where notifications about new FIRs are routed by clearance level.
OFFICIAL_RECIPIENTS = [
    r.strip()
    for r in os.environ.get("CSN_NOTIFY_OFFICIALS", "").split(",")
    if r.strip()
]


def smtp_configured() -> bool:
    return bool(
        os.environ.get("CSN_SMTP_HOST")
        and os.environ.get("CSN_SMTP_USER")
        and os.environ.get("CSN_SMTP_PASS")
    )


def _send(to_addrs: list[str], subject: str, body_text: str) -> bool:
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        return False
    if not smtp_configured():
        print("\n" + "=" * 72, file=sys.stderr)
        print(f"[mail:NOT SENT - SMTP not configured] {subject}", file=sys.stderr)
        for a in to_addrs:
            print(f"  to: {a}", file=sys.stderr)
        print(body_text, file=sys.stderr)
        print("=" * 72 + "\n", file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["From"] = os.environ.get("CSN_SMTP_FROM") or os.environ["CSN_SMTP_USER"]
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body_text)

    host = os.environ["CSN_SMTP_HOST"]
    port = int(os.environ.get("CSN_SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if port == 587:
                smtp.starttls()
                smtp.ehlo()
            smtp.login(os.environ["CSN_SMTP_USER"], os.environ["CSN_SMTP_PASS"])
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - never let mail break the request
        print(f"[mail:FAILED] {subject} -> {exc}", file=sys.stderr)
        return False


def send_password_reset(username: str, link: str) -> bool:
    return _send(
        [username],
        f"Crime Sight Noir - password reset for '{username}'",
        f"Hello {username},\n\n"
        f"A password reset was requested for your Crime Sight Noir Public account.\n\n"
        f"Open this link within 30 minutes to choose a new password:\n\n"
        f"    {link}\n\n"
        f"If you did not request this, you can ignore this message - nothing has\n"
        f"changed and your password still works.\n",
    )


def send_case_confirmation(reporter_email: str, fir_number: str, crime_type: str,
                           station: str, status: str) -> bool:
    return _send(
        [reporter_email],
        f"FIR {fir_number} registered - Crime Sight Noir",
        f"Your First Information Report has been recorded.\n\n"
        f"    FIR number : {fir_number}\n"
        f"    Crime type : {crime_type}\n"
        f"    Station    : {station}\n"
        f"    Status     : {status}\n\n"
        f"A police officer will review it. Quote {fir_number} for any follow-up.\n",
    )


def send_case_alert_to_officials(fir_number: str, crime_type: str, severity: str,
                                 station: str, district: str, state: str,
                                 reporter: str) -> bool:
    if not OFFICIAL_RECIPIENTS:
        print(f"[mail:no recipients configured] new FIR {fir_number} "
              f"({severity}) at {station}", file=sys.stderr)
        return False
    return _send(
        OFFICIAL_RECIPIENTS,
        f"New FIR {fir_number} - {severity} - {crime_type}",
        f"A new First Information Report has been filed.\n\n"
        f"    FIR number : {fir_number}\n"
        f"    Crime type : {crime_type}\n"
        f"    Severity   : {severity}\n"
        f"    Station    : {station}\n"
        f"    District   : {district}\n"
        f"    State      : {state}\n"
        f"    Filed by   : {reporter}\n",
    )
