#!/usr/bin/env python3
"""Seed the **Work** profile from the PopWheels software tracker spreadsheet.

The projects were parsed from ``media/PopWheels Software Project Tracker 2026.xlsx``
(the union of the Q1/Q2/Q3 cycle-tracker sheets, deduped by project name) into
``work_profile.json``, already shaped like the app's DB. This inserts them under
``profile = 'work'`` so they show up only when the Work profile is active — the
Personal profile (your existing data) is never touched.

    DATABASE_URL='postgresql://...pooler.supabase.com:5432/postgres' \\
        python load_work_profile.py

Run ONCE. It refuses to run if the Work profile already has projects, so a second
run can't double-insert. Items carry per-cycle completion (done deliverables →
built stories), so the city renders a skyline of varied heights.
"""
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = "work"


def main():
    dsn = os.environ["DATABASE_URL"]
    projects = json.load(open(os.path.join(HERE, "work_profile.json")))

    # Make sure the schema (incl. the `profile` column) exists.
    import app
    app.init_db()

    # prepare_threshold=None: a one-shot bulk load has no use for prepared
    # statements and it keeps the load working against any pooler mode.
    with psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None) as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE profile = %s", (PROFILE,)
        ).fetchone()["n"]
        if n:
            sys.exit(f"refusing to run: the '{PROFILE}' profile already has {n} projects")

        # pos is per-profile, starting after any existing rows (there are none, but
        # keep it correct if that ever changes).
        base = db.execute(
            "SELECT COALESCE(MAX(pos), -1) + 1 AS p FROM projects WHERE profile = %s",
            (PROFILE,),
        ).fetchone()["p"]

        n_items = 0
        for offset, p in enumerate(projects):
            row = db.execute(
                "INSERT INTO projects (title, pos, render_progress, profile)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (p["title"], base + offset, int(p.get("render_progress", 1)), PROFILE),
            ).fetchone()
            pid = row["id"]
            for it in p["items"]:
                comp = float(it["completion"])
                db.execute(
                    "INSERT INTO items (project_id, text, done, completion, pos,"
                    " created_at, start_at, due_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        pid, it["text"], 1 if comp >= 1.0 else 0, comp, it["pos"],
                        it.get("created_at", ""), it.get("start_at", ""), it.get("due_at", ""),
                    ),
                )
                n_items += 1
        db.commit()
        print(f"loaded {len(projects)} projects, {n_items} items into the '{PROFILE}' profile")


if __name__ == "__main__":
    main()
