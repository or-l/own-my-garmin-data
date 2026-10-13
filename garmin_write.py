"""
The ONLY module that calls Garmin write endpoints (upload_workout,
schedule_workout, unschedule_workout, delete_workout).
tests/test_write_isolation.py fails if any other file references them, and
if the ingestion path imports this module.

Callers (seed_workouts.py, sync_schedule.py) connect with
SCHEDULER_DATABASE_URL. The scheduler role has SELECT + column UPDATE on
auth_tokens but no INSERT, so the token is saved back with a plain UPDATE
(save_token_update_only) rather than bootstrap.garmin_auth.save_token_to_db's
upsert.

No idempotency here — that's the callers' job (ledger / (workout_id, date) diff).
"""
from __future__ import annotations

import contextlib
import json
import sys
from datetime import date
from typing import Any, Iterator

import psycopg
from garminconnect import Garmin

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db

# The complete set of Garmin write endpoints this project uses. Other
# modules that need the names (the probe's preflight) import this tuple
# rather than spelling them — the isolation test flags string literals too.
WRITE_ENDPOINTS = ("upload_workout", "schedule_workout", "unschedule_workout", "delete_workout")


def find_first(obj: Any, key: str) -> Any:
    """First non-empty value for `key` anywhere in a nested dict/list (depth-first)."""
    if isinstance(obj, dict):
        if obj.get(key) not in (None, ""):
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find_first(child, key)
        if found is not None:
            return found
    return None


def save_token_update_only(conn: psycopg.Connection) -> None:
    payload = {f.name: f.read_text() for f in TOKEN_DIR.glob("*.json")}
    cur = conn.execute(
        "update auth_tokens set payload = %s, updated_at = now() where provider = 'garmin'",
        (json.dumps(payload),),
    )
    if cur.rowcount != 1:
        raise RuntimeError("No garmin row in auth_tokens to update — run `uv run install.py --reauth` first.")
    conn.commit()


@contextlib.contextmanager
def garmin_session(conn: psycopg.Connection) -> Iterator[Garmin]:
    load_token_from_db(conn)
    client = Garmin()
    client.login(tokenstore=str(TOKEN_DIR))
    try:
        yield client
    finally:
        conn.rollback()  # a failed statement mid-run would otherwise block the token save
        save_token_update_only(conn)


def upload(client: Garmin, payload: dict) -> int:
    resp = client.upload_workout(payload)
    workout_id = find_first(resp, "workoutId")
    if workout_id is None:
        raise RuntimeError(f"upload_workout returned no workoutId: {str(resp)[:300]}")
    return int(workout_id)


def schedule(client: Garmin, workout_id: int, day: date) -> int | None:
    resp = client.schedule_workout(workout_id, day.isoformat())
    for key in ("workoutScheduleId", "scheduleId"):
        found = find_first(resp, key)
        if found is not None:
            return int(found)
    if isinstance(resp, dict) and resp.get("id") is not None:
        return int(resp["id"])
    return None


def unschedule(client: Garmin, schedule_id: int) -> None:
    client.unschedule_workout(schedule_id)


def delete(client: Garmin, workout_id: int) -> None:
    client.delete_workout(workout_id)


def confirmed(confirm_flag: bool, summary: str) -> bool:
    """--confirm, or a typed 'yes' at a terminal. Anything else is a dry run."""
    if confirm_flag:
        return True
    if not sys.stdin.isatty():
        print("Dry run — nothing written to Garmin. Re-run with --confirm to write.")
        return False
    answer = input(f"{summary}\nType 'yes' to write this to Garmin: ")
    return answer.strip().lower() == "yes"
