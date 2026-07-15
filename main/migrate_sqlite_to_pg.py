#!/usr/bin/env python3
"""One-shot copy of the old SQLite database into Supabase Postgres.

Run ONCE, after the Postgres schema exists (start the app once, or let this
script call init_db for you), to carry your existing projects/items/tags over:

    DATABASE_URL='postgresql://...pooler.supabase.com:5432/postgres' \\
        python migrate_sqlite_to_pg.py [path/to/projects.db]

It preserves primary-key ids (items reference projects and each other by id, so
the ids must survive), inserts in FK-dependency order, then advances each SERIAL
sequence past the highest id copied so future inserts don't collide. Idempotency
is NOT assumed: run it against an empty target. It refuses to run if the target
tables already hold rows, so a second accidental run can't double-insert.
"""
import os
import sqlite3
import sys

import psycopg
from psycopg.rows import dict_row

# Tables in FK-dependency order, with the columns to copy verbatim. `id` is
# copied explicitly to keep every cross-row reference (project_id, depends_on,
# item_tags) pointing at the same rows it did in SQLite.
TABLES = {
    "projects": ["id", "title", "pos", "render_progress", "logo", "logo_src", "logo_size"],
    "items": ["id", "project_id", "text", "done", "completion", "notes", "pos",
              "created_at", "start_at", "due_at", "depends_on"],
    "tags": ["id", "project_id", "name", "color", "pos"],
    "item_tags": ["item_id", "tag_id"],
}


def main():
    src_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "projects.db"
    )
    dsn = os.environ["DATABASE_URL"]

    # Make sure the destination schema exists (safe to call repeatedly).
    import app
    app.init_db()

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    # prepare_threshold=None: a one-shot bulk copy has no use for prepared
    # statements, and disabling them keeps this working against any pooler mode.
    dst = psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None)

    try:
        for table in TABLES:
            n = dst.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if n:
                sys.exit(f"refusing to run: {table} already has {n} rows in Postgres")

        total = 0
        for table, cols in TABLES.items():
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            if not rows:
                print(f"{table}: 0 rows")
                continue
            placeholders = ", ".join(["%s"] * len(cols))
            collist = ", ".join(cols)
            with dst.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
                    [tuple(r[c] for c in cols) for r in rows],
                )
            # Advance the SERIAL sequence so the next app INSERT gets MAX(id)+1.
            if "id" in cols:
                dst.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                    f" (SELECT MAX(id) FROM {table}))"
                )
            print(f"{table}: {len(rows)} rows")
            total += len(rows)

        dst.commit()
        print(f"done — {total} rows copied")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
