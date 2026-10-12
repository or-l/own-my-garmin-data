# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Seed job: materialize `generated` protocols referenced by APPROVED plan
rows as real Garmin workouts. Manual only; runs as the scheduler role.

    uv run seed_workouts.py              # dry run: prints what would upload
    uv run seed_workouts.py --confirm    # uploads
    uv run seed_workouts.py --confirm --prune-stale

Per slug: not in generated_workouts -> build + upload + insert ledger;
protocol hash changed -> re-upload + repoint ledger (old workout left alone
unless --prune-stale and nothing references it: no activity, no enacted
plan row, no synced calendar entry); unchanged -> skip.
Library workouts are never touched. A row whose approval went stale (edited
after approval) is skipped until re-approved — see planning.py.

Env: SCHEDULER_DATABASE_URL (the Garmin token is read from auth_tokens through it).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, ContextManager

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import garmin_write
import planning
import protocols

POLITENESS_SLEEP_S = 1.0


@dataclass(frozen=True)
class SeedAction:
    slug: str
    kind: str  # "upload" | "reupload"
    params_hash: str  # planning.protocol_hash of what gets uploaded (ledger column name)
    old_workout_id: int | None = None


def plan_seed(conn: psycopg.Connection, today: date) -> tuple[list[SeedAction], list[str]]:
    ready, notes = planning.partition_for_enactment(planning.load_plan(conn, since=today))
    wanted = {v.row.protocol_slug: v.proto_hash for v in ready if v.row.source == "generated"}
    ledger = {slug: (wid, h) for slug, wid, h in
              conn.execute("select protocol_slug, workout_id, params_hash from generated_workouts").fetchall()}
    actions = []
    for slug, current in sorted(wanted.items()):
        entry = ledger.get(slug)
        if entry is None:
            actions.append(SeedAction(slug, "upload", current))
        elif entry[1] != current:
            actions.append(SeedAction(slug, "reupload", current, old_workout_id=entry[0]))
    return actions, notes


def _still_referenced(conn: psycopg.Connection, workout_id: int) -> str | None:
    """Why the old workout must be kept, or None if it's safe to delete:
    an activity was done from it, a plan row is enacted with it (e.g. a row
    left stale, still on the calendar), or the synced calendar holds it."""
    if conn.execute("select to_regclass('activities')").fetchone()[0] is None:
        return "can't check activities"  # can't tell -> treat as referenced, never delete
    if conn.execute("select exists (select 1 from activities where raw->>'workoutId' = %s)", (str(workout_id),)).fetchone()[0]:
        return "an activity references it"
    if conn.execute("select exists (select 1 from training_plan where resolved_workout_id = %s)", (workout_id,)).fetchone()[0]:
        return "a plan row is still scheduled with it"
    if conn.execute("select exists (select 1 from scheduled_workouts where workout_id = %s)", (workout_id,)).fetchone()[0]:
        return "it's on the synced Garmin calendar"
    return None


def execute_seed(conn: psycopg.Connection, client, actions: list[SeedAction], *, prune_stale: bool) -> tuple[list[str], int]:
    lines, errors = [], 0
    for a in actions:
        try:
            name, description, kind, params = conn.execute(
                "select name, description, kind, params from protocols where slug = %s", (a.slug,)
            ).fetchone()
            if planning.protocol_hash(kind, name, description, params) != a.params_hash:
                raise RuntimeError("protocol changed while seeding — re-run")
            workout_id = garmin_write.upload(client, protocols.build(kind, name, description, params))
            conn.execute(
                """
                insert into generated_workouts (protocol_slug, workout_id, params_hash) values (%s, %s, %s)
                    on conflict (protocol_slug) do update set workout_id = excluded.workout_id, params_hash = excluded.params_hash
                """,
                (a.slug, workout_id, a.params_hash),
            )
            conn.commit()
            lines.append(f"{a.kind} {a.slug} -> workout {workout_id}")
            if a.kind == "reupload" and prune_stale:
                why = _still_referenced(conn, a.old_workout_id)
                if why:
                    lines.append(f"  kept old workout {a.old_workout_id} ({why})")
                else:
                    garmin_write.delete(client, a.old_workout_id)
                    lines.append(f"  deleted stale workout {a.old_workout_id}")
        except Exception as e:  # keep going; report at the end
            conn.rollback()
            errors += 1
            lines.append(f"{a.slug}: FAILED — {e}")
        time.sleep(POLITENESS_SLEEP_S)
    return lines, errors


def run(conn: psycopg.Connection, open_client: Callable[[], ContextManager], *, today: date,
        confirm: Callable[[str], bool], prune_stale: bool = False) -> int:
    actions, notes = plan_seed(conn, today)
    for n in notes:
        print(f"  note: {n}")
    if not actions:
        print("Seed: nothing to upload.")
        return 0
    print("Seed plan:")
    for a in actions:
        print(f"  {a.kind:9} {a.slug}" + (f" (replaces workout {a.old_workout_id})" if a.old_workout_id else ""))
    if not confirm(f"{len(actions)} workout(s) to upload."):
        return 0
    with open_client() as client:
        lines, errors = execute_seed(conn, client, actions, prune_stale=prune_stale)
    for line in lines:
        print(f"  {line}")
    print(f"Seed done: {len(actions) - errors} ok, {errors} failed.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="Actually upload (default: dry run).")
    parser.add_argument("--prune-stale", action="store_true", help="Delete a replaced workout if nothing references it (activity, enacted plan row, synced calendar).")
    args = parser.parse_args()
    load_dotenv()
    with psycopg.connect(os.environ["SCHEDULER_DATABASE_URL"]) as conn:
        sys.exit(run(conn, lambda: garmin_write.garmin_session(conn), today=date.today(),
                     confirm=lambda s: garmin_write.confirmed(args.confirm, s), prune_stale=args.prune_stale))


if __name__ == "__main__":
    main()
