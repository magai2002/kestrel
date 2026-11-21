"""The Postgres role the **public** API connects as.

The public app is the only part of kestrel reachable from the internet,
so it gets the least the database can give it:

- ``SELECT`` on exactly ``api.queries.PUBLIC_TABLES`` and nothing else:
  no writes anywhere, and no reads of ``orders``, ``instruments``, the
  trial ledger or the hold-out log;
- ``default_transaction_read_only`` on, a 5 s ``statement_timeout`` and
  a connection limit, so a bug or a flood can't hold the engine's
  database hostage.

The engine, migrations and admin app keep using the owner role. Run by
``kestrel admin ensure-public-role`` after every migration (the prod
``migrate`` service does), so grants follow schema changes; idempotent.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from api.queries import PUBLIC_TABLES

PUBLIC_ROLE = "kestrel_public"
CONNECTION_LIMIT = 20
STATEMENT_TIMEOUT = "5s"


def ensure_public_role(conn: Connection, password: str | None) -> None:
    """Create or update the role and reset its grants to exactly
    ``PUBLIC_TABLES``. ``password=None`` leaves it NOLOGIN (tests reach it
    with ``SET ROLE``)."""
    exists = conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": PUBLIC_ROLE})
    if not exists:
        conn.execute(text(f"CREATE ROLE {PUBLIC_ROLE} NOLOGIN"))
    if password is None:
        conn.execute(text(f"ALTER ROLE {PUBLIC_ROLE} NOLOGIN"))
    else:
        # ALTER ROLE takes no bind parameters; quote the literal ourselves.
        quoted = conn.scalar(text("SELECT quote_literal(:p)"), {"p": password})
        conn.execute(text(f"ALTER ROLE {PUBLIC_ROLE} LOGIN PASSWORD {quoted}"))
    conn.execute(
        text(
            f"ALTER ROLE {PUBLIC_ROLE} NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {CONNECTION_LIMIT}"
        )
    )
    conn.execute(text(f"ALTER ROLE {PUBLIC_ROLE} SET default_transaction_read_only = on"))
    conn.execute(text(f"ALTER ROLE {PUBLIC_ROLE} SET statement_timeout = '{STATEMENT_TIMEOUT}'"))

    conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {PUBLIC_ROLE}"))
    conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {PUBLIC_ROLE}"))
    conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {PUBLIC_ROLE}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {PUBLIC_ROLE}"))
    tables = ", ".join(f'"{t}"' for t in PUBLIC_TABLES)
    conn.execute(text(f"GRANT SELECT ON {tables} TO {PUBLIC_ROLE}"))
