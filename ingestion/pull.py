# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Runs on every scheduled GitHub Actions execution.
Assumes a Garmin token was already generated once via bootstrap/generate_token.py
and pushed to the auth_tokens table via bootstrap/load_token_to_db.py.

Each run:
  1. Pulls the current token from Postgres, writes it to the expected local path.
  2. Logs in to Garmin using that token (no password needed — resumes session).
  3. Loops over the domains enabled in sync_config (see config.py,
     domains.py) and calls each domain's sync_incremental, which pulls its
     own data for a lookback window (not just "today", so a missed run
     doesn't leave a permanent gap) and upserts it into the normalized
     tables. Snapshot-style domains (challenges/badges, profile/goals)
     always refresh in full, since they only ever reflect current state,
     not history.
  4. Reads back the (possibly refreshed) token files and saves them to Postgres,
     so the next ephemeral runner picks up the latest one.

For pulling FULL history (years of past data), use backfill.py instead —
this script is deliberately a small, cheap, incremental sync.
"""

import os
import pathlib
import sys
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv
from garminconnect import Garmin

# `python ingestion/pull.py` puts ingestion/ (this file's own directory) on
# sys.path[0], not the repo root — so bootstrap/ isn't importable without
# this explicit insert.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db, save_token_to_db
from config import get_enabled_domains
from domains import DOMAINS, SyncContext

load_dotenv()  # reads .env in the current directory into os.environ, if present

DB_URL = os.environ["DATABASE_URL"]

# How many days back to re-check on every run. Covers gaps if a scheduled
# run is skipped (e.g. runner outage) without re-pulling everything.
LOOKBACK_DAYS = 5


def table_count(conn: psycopg.Connection, table: str) -> int:
    # A table can be missing when the code is newer than the schema (e.g.
    # merged before `install.py --upgrade` applied its migration): count 0
    # instead of failing the whole sync.
    if conn.execute("select to_regclass(%s)", (table,)).fetchone()[0] is None:
        return 0
    return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608 (fixed table names, not user input)


def write_step_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)
    else:
        print(text)  # local run — no GITHUB_STEP_SUMMARY file, just print instead


def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        load_token_from_db(conn)

        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))

        enabled = get_enabled_domains(conn)
        today = date.today()
        window_start = today - timedelta(days=LOOKBACK_DAYS)

        domains_to_run = [d for d in DOMAINS if d.key in enabled]
        tables = sorted({t for d in domains_to_run for t in d.tables})
        before = {t: table_count(conn, t) for t in tables}

        ctx = SyncContext(client=client, conn=conn, window_start=window_start, today=today)
        api_counts: dict[str, int] = {}
        for domain in domains_to_run:
            for table, count in domain.sync_incremental(ctx).items():
                api_counts[table] = api_counts.get(table, 0) + count

        save_token_to_db(conn)

        after = {t: table_count(conn, t) for t in tables}

    summary = ["### Garmin sync summary", "", "| Table | New rows | Fetched from API | Total in DB |", "|---|---|---|---|"]
    for t in tables:
        new_rows = after[t] - before[t]
        fetched = api_counts.get(t, "—")
        summary.append(f"| {t} | +{new_rows} | {fetched} | {after[t]} |")

    total_new = sum(after[t] - before[t] for t in tables)
    if total_new == 0:
        summary.append("")
        summary.append(
            "_No new rows this run — normal if nothing changed on Garmin's side "
            "since the last sync (e.g. a rest day). If you expected new activity "
            "data and see 0 here, that's worth investigating._"
        )
    if not domains_to_run:
        summary.append("")
        summary.append("_No domains enabled — check sync_config.enabled_domains._")

    write_step_summary(summary)


if __name__ == "__main__":
    main()