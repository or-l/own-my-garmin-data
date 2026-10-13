# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Enactor: put APPROVED training_plan rows on the Garmin calendar. Manual
only; runs as the scheduler role. Run seed_workouts.py first.

    uv run sync_schedule.py              # dry run: prints what would be scheduled
    uv run sync_schedule.py --confirm    # schedules

Only rows with a plan_approvals row whose fingerprint still matches are
considered (status is advisory; the approval row is authoritative). Each is
resolved to a real workout id — generated -> the seed ledger; library ->
the row's workout_id, else a unique workouts.workout_name match — and
diffed against the LIVE Garmin calendar on (workout_id, date), so re-runs
and crash recovery never double-schedule. Each (workout_id, date) has one
owning row per run; other rows resolving to it are noted, never scheduled.

Rows the coach marked skip = true are unscheduled from Garmin whatever their
approval state (removal is the safe direction), then marked 'skipped'.

Env: SCHEDULER_DATABASE_URL.
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
from domains import _fetch_scheduled

POLITENESS_SLEEP_S = 1.0


@dataclass(frozen=True)
class EnactAction:
    plan_id: int
    day: date
    workout_id: int
    kind: str  # "schedule" | "reschedule" | "record" | "unschedule"
    old_schedule_id: int | None = None
    live_schedule_id: int | None = None


def resolve_workout_id(conn: psycopg.Connection, view: planning.PlanView,
                       ledger: dict[str, tuple[int, str]]) -> tuple[int | None, str | None]:
    row = view.row
    if row.source == "generated":
        entry = ledger.get(row.protocol_slug)
        if entry is None:
            return None, "not seeded yet — run seed_workouts.py first"
        if entry[1] != view.proto_hash:
            return None, "seeded workout is out of date — run seed_workouts.py first"
        return entry[0], None
    if row.workout_id is not None:
        return row.workout_id, None
    ids = [r[0] for r in conn.execute(
        "select workout_id from workouts where workout_name = %s", (row.workout_name,)).fetchall()]
    if not ids:
        return None, f"no library workout named {row.workout_name!r} (has ingestion synced it yet?)"
    if len(ids) > 1:
        return None, f"{len(ids)} library workouts named {row.workout_name!r} — set workout_id instead"
    return ids[0], None


def plan_enact(conn: psycopg.Connection, views: list[planning.PlanView], live_items: dict[int, dict],
               today: date) -> tuple[list[EnactAction], list[str]]:
    ledger = {slug: (wid, h) for slug, wid, h in
              conn.execute("select protocol_slug, workout_id, params_hash from generated_workouts").fetchall()}
    live = {(int(i["workoutId"]), date.fromisoformat(i["date"])): sid for sid, i in live_items.items()}
    actions: list[EnactAction] = []
    for v in planning.cancellations(views):
        row = v.row
        if row.enacted_date < today:
            continue  # history; leave past calendar entries alone
        # None when it's already gone from Garmin (removed in Connect, or a crash
        # after unschedule): then it's a DB-only cleanup.
        actions.append(EnactAction(row.id, row.enacted_date, row.resolved_workout_id, "unschedule",
                                   old_schedule_id=live.get((row.resolved_workout_id, row.enacted_date))))

    ready, notes = planning.partition_for_enactment(views)
    resolved = []
    for v in ready:
        workout_id, why = resolve_workout_id(conn, v, ledger)
        if workout_id is None:
            notes.append(f"plan {v.row.id} ({v.describe()}): skipped — {why}")
        else:
            resolved.append((v, workout_id))

    def recorded(row, workout_id) -> bool:
        return (row.status == "scheduled" and row.resolved_workout_id == workout_id
                and row.enacted_date == row.scheduled_date)

    # One owner per (workout_id, date) per run: a row already recorded against
    # it wins, else the first by (date, id). Two rows can collide when one names
    # a library workout by id and another by name; the unique index can't see that.
    owner = {(wid, v.row.scheduled_date): v.row.id for v, wid in resolved if recorded(v.row, wid)}
    for v, workout_id in resolved:
        row = v.row
        target = (workout_id, row.scheduled_date)
        if owner.setdefault(target, row.id) != row.id:
            notes.append(f"plan {row.id} ({v.describe()}): skipped — same workout and day as plan {owner[target]}")
            continue
        # The entry this row put on the calendar before (if it moved, it's stale).
        old = None
        if row.resolved_workout_id is not None and row.enacted_date is not None \
                and (row.resolved_workout_id, row.enacted_date) != target:
            old = live.get((row.resolved_workout_id, row.enacted_date))
        if target in live:
            if not recorded(row, workout_id):
                actions.append(EnactAction(row.id, row.scheduled_date, workout_id, "record",
                                           old_schedule_id=old, live_schedule_id=live[target]))
            continue
        if row.scheduled_date < today:
            notes.append(f"plan {row.id} ({v.describe()}): in the past — not scheduling")
            continue
        actions.append(EnactAction(row.id, row.scheduled_date, workout_id,
                                   "reschedule" if old else "schedule", old_schedule_id=old))
    return actions, notes


def execute_enact(conn: psycopg.Connection, client, actions: list[EnactAction]) -> tuple[list[str], int]:
    lines, errors = [], 0
    for a in actions:
        try:
            schedule_id = a.live_schedule_id
            if a.kind == "unschedule":
                if a.old_schedule_id is not None:
                    garmin_write.unschedule(client, a.old_schedule_id)
                    time.sleep(POLITENESS_SLEEP_S)
                conn.execute(
                    "update training_plan set status = 'skipped', schedule_id = null, resolved_workout_id = null, enacted_date = null where id = %s",
                    (a.plan_id,),
                )
                conn.commit()
                lines.append(f"unschedule plan {a.plan_id}: workout {a.workout_id} on {a.day}")
                continue
            if a.old_schedule_id is not None:  # reschedule, or a record that replaces an older entry
                garmin_write.unschedule(client, a.old_schedule_id)
            if a.kind in ("schedule", "reschedule"):
                # Pin the row before touching Garmin: with resolved_workout_id set,
                # the delete guard stops the coach deleting it mid-flight, and a
                # crash leaves a pointer the next run can diff against.
                pinned = conn.execute(
                    "update training_plan set schedule_id = null, resolved_workout_id = %s, enacted_date = %s where id = %s",
                    (a.workout_id, a.day, a.plan_id),
                ).rowcount
                conn.commit()
                if pinned != 1:
                    lines.append(f"plan {a.plan_id}: row deleted before scheduling — skipped")
                    continue
                schedule_id = garmin_write.schedule(client, a.workout_id, a.day)
                time.sleep(POLITENESS_SLEEP_S)
            updated = conn.execute(
                "update training_plan set status = 'scheduled', schedule_id = %s, resolved_workout_id = %s, enacted_date = %s where id = %s",
                (schedule_id, a.workout_id, a.day, a.plan_id),
            ).rowcount
            conn.commit()
            if updated != 1:
                raise RuntimeError(f"plan row vanished after Garmin write (schedule {schedule_id}) — remove it in Connect")
            lines.append(f"{a.kind} plan {a.plan_id}: workout {a.workout_id} on {a.day} (schedule {schedule_id})")
        except Exception as e:
            conn.rollback()
            errors += 1
            lines.append(f"plan {a.plan_id}: FAILED — {e}")
    return lines, errors


def run(conn: psycopg.Connection, open_client: Callable[[], ContextManager], *, today: date,
        confirm: Callable[[str], bool]) -> int:
    views = planning.load_plan(conn, since=today)
    ready, _ = planning.partition_for_enactment(views)
    cancels = [v for v in planning.cancellations(views) if v.row.enacted_date >= today]
    if not ready and not cancels:
        for n in planning.partition_for_enactment(views)[1]:
            print(f"  note: {n}")
        print("Enactor: no approved or cancelled plan rows from today on.")
        return 0
    # Cover where rows are going AND where they were enacted before, so a moved
    # row's old entry is visible to unschedule even when it's outside the new window.
    end = max([v.row.scheduled_date for v in ready] + [v.row.enacted_date for v in cancels]
              + [v.row.enacted_date for v in ready if v.row.enacted_date is not None])
    with open_client() as client:
        live = _fetch_scheduled(client, today, end)
        actions, notes = plan_enact(conn, views, live, today)
        for n in notes:
            print(f"  note: {n}")
        if not actions:
            print("Enactor: calendar already matches the approved plan.")
            return 0
        print("Enactor plan:")
        for a in actions:
            print(f"  {a.kind:10} plan {a.plan_id}: workout {a.workout_id} on {a.day}")
        garmin_writes = sum((a.kind in ("schedule", "reschedule")) + (a.old_schedule_id is not None) for a in actions)
        if not confirm(f"{garmin_writes} Garmin calendar write(s), {len(actions) - garmin_writes} DB-only update(s)."):
            return 0
        lines, errors = execute_enact(conn, client, actions)
    for line in lines:
        print(f"  {line}")
    print(f"Enactor done: {len(actions) - errors} ok, {errors} failed.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="Actually schedule (default: dry run).")
    args = parser.parse_args()
    load_dotenv()
    with psycopg.connect(os.environ["SCHEDULER_DATABASE_URL"]) as conn:
        sys.exit(run(conn, lambda: garmin_write.garmin_session(conn), today=date.today(),
                     confirm=lambda s: garmin_write.confirmed(args.confirm, s)))


if __name__ == "__main__":
    main()
