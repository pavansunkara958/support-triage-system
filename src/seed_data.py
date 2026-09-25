"""The support database the tools read from.

SQLite rather than dictionaries, because the refund path needs a real
transaction: `issue_refund` must mark an invoice refunded atomically, or a
retried call refunds it twice. That is the kind of defect a dictionary-backed
mock cannot exhibit, so it would never be caught before production.

Seeded deliberately with:
  - a duplicate charge pair (same amount, same day) — the canonical billing case
  - one invoice large enough to make a split-refund attempt possible
  - a known issue matching the technical demo
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Overridable so tests and CI can point at scratch storage. SQLite needs file
# locking, which some network and container-mounted filesystems refuse — and a
# test suite that cannot run outside one directory is a test suite people skip.
_default = Path(__file__).resolve().parent.parent / "data" / "support.db"
DB_PATH = Path(os.getenv("SUPPORT_DB", _default))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL,
    seats       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id  TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    amount_usd  REAL NOT NULL,
    issued_on   TEXT NOT NULL,
    description TEXT NOT NULL,
    refunded    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id   TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL UNIQUE,
    amount_usd  REAL NOT NULL,
    reason      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS known_issues (
    issue_id    TEXT PRIMARY KEY,
    keywords    TEXT NOT NULL,
    title       TEXT NOT NULL,
    workaround  TEXT NOT NULL,
    status      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_status (
    component   TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL
);
"""

CUSTOMERS = [
    ("CUS-1001", "dana.k@northwind.example", "Dana Kowalski", "pro", 25),
    ("CUS-1002", "sam.oyelaran@contoso.example", "Sam Oyelaran", "starter", 5),
]

INVOICES = [
    # The duplicate pair: same amount, same day.
    ("INV-2026-0412", "CUS-1001", 89.00, "2026-03-04", "Pro plan — March"),
    ("INV-2026-0413", "CUS-1001", 89.00, "2026-03-04", "Pro plan — March"),
    ("INV-2026-0501", "CUS-1001", 180.00, "2026-05-02", "Additional seats"),
    ("INV-2026-0377", "CUS-1002", 29.00, "2026-02-04", "Starter plan — February"),
]

KNOWN_ISSUES = [
    ("KI-118", "export csv timeout slow download",
     "CSV export times out above 500k rows",
     "Filter by date range to keep the export under 500k rows.",
     "Engineering is tracking this. No fix date is available."),
    ("KI-204", "login sso saml redirect loop",
     "SSO redirect loop after session expiry",
     "Clear cookies for the auth domain and sign in again.",
     "Fix shipped in release 5.1; upgrade resolves it."),
]

SERVICE_STATUS = [
    ("exports", "degraded", "Elevated timeouts on large exports since 09:00 UTC."),
    ("api", "operational", "No known issues."),
    ("auth", "operational", "No known issues."),
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main(path: Path | None = None) -> Path:
    """Create and seed the database. Idempotent — safe to re-run."""
    path = Path(path or DB_PATH)
    conn = connect(path)
    conn.executescript(SCHEMA)

    # Reset transactional state so the demo is reproducible: a second run must
    # show the same refund succeeding, not "already refunded".
    conn.execute("DELETE FROM refunds")
    conn.execute("UPDATE invoices SET refunded = 0")

    conn.executemany(
        "INSERT OR REPLACE INTO customers VALUES (?,?,?,?,?)", CUSTOMERS)
    conn.executemany(
        "INSERT OR REPLACE INTO invoices (invoice_id, customer_id, amount_usd,"
        " issued_on, description, refunded) VALUES (?,?,?,?,?,0)", INVOICES)
    conn.executemany(
        "INSERT OR REPLACE INTO known_issues VALUES (?,?,?,?,?)", KNOWN_ISSUES)
    conn.executemany(
        "INSERT OR REPLACE INTO service_status VALUES (?,?,?)", SERVICE_STATUS)
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    p = main()
    print(f"seeded {p}")
    print(f"  {len(CUSTOMERS)} customers, {len(INVOICES)} invoices, "
          f"{len(KNOWN_ISSUES)} known issues")
