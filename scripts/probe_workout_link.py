# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Probe: confirm the Lattice-in-Garmin round-trip, and specifically whether a
COMPLETED activity links back to the SCHEDULED/authored workout it came from.

WRITE-side probe. NOT part of the read-only ingestion pipeline. Lives in
scripts/ and does its Garmin writes through garmin_write (the only module
allowed to call write endpoints). Reuses the same DB-token auth, and cleans up after
itself. The load-bearing question: does a completed activity carry `workoutId`
back to the workout? That link is the join key between "what was prescribed"
(the workout) and "what was done" (the activity).

SUBCOMMANDS
  preflight                     check methods + list workouts
  reflect <workout_id>          dump a workout's exact JSON as a template
  schedule --from-template F     clone+upload+schedule a template (writes state.json)
           --builtin interval    best-effort from-scratch interval workout
  inspect                       check the join on the activity
           --workout-id ID       workout to look for (else read from state.json)
           --activity-id ID      inspect a specific activity (else auto-match)
           --limit N             how many recent activities to scan (default 15)
  cleanup [--all-probe]         unschedule + delete probe workout(s)

`inspect` works WITH OR WITHOUT state.json — pass --workout-id to check a
workout you scheduled by hand in Connect. With neither state.json nor
--workout-id, it just lists recent activities so you can grab an id.

DATABASE_URL is read from .env. Run from the repo root.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import pathlib
import sys
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv
from garminconnect import Garmin

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db, save_token_to_db
import garmin_write

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "probe_out"
OUT_DIR.mkdir(exist_ok=True)
STATE_PATH = OUT_DIR / "state.json"

REQUIRED = ["get_workouts", "get_workout_by_id", "get_activities", *garmin_write.WRITE_ENDPOINTS[:2]]
OPTIONAL = [*garmin_write.WRITE_ENDPOINTS[2:], "get_activity_exercise_sets", "get_scheduled_workouts"]

# Identity/audit keys stripped (recursively) from a reflected template before
# re-upload. stepId is included: carrying server-assigned stepIds into a create
# is the classic cause of a rejected or update-in-place POST. childStepId is an
# exact-key match and is NOT in this set, so repeat-group linkage is preserved.
STRIP_KEYS = {"workoutId", "ownerId", "updatedDate", "createdDate", "updateDate",
              "createDate", "author", "stepId"}


@contextlib.contextmanager
def session():
    """DB + Garmin via the pipeline's own DB-token auth; saves token on exit."""
    with psycopg.connect(DB_URL) as conn:
        load_token_from_db(conn)
        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))
        try:
            yield conn, client
        finally:
            with contextlib.suppress(Exception):
                save_token_to_db(conn)


def _dump(obj, path: pathlib.Path) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))
    try:
        rel = path.relative_to(pathlib.Path.cwd())
    except ValueError:
        rel = path
    print(f"    wrote {rel}")


def iter_items(obj, path=""):
    """Yield (dotted_path, key, value) for every dict entry, recursively."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            yield p, k, v
            yield from iter_items(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_items(v, f"{path}[{i}]")


def _strip_ids(obj):
    if isinstance(obj, dict):
        return {k: _strip_ids(v) for k, v in obj.items() if k not in STRIP_KEYS}
    if isinstance(obj, list):
        return [_strip_ids(v) for v in obj]
    return obj


def _find_first(obj, key):
    for _, k, v in iter_items(obj):
        if k == key and v not in (None, ""):
            return v
    return None


# ------------------------------------------------------------------ preflight
def cmd_preflight(_args):
    import garminconnect

    print(f"garminconnect version: {getattr(garminconnect, '__version__', '(unknown)')}\n")
    with session() as (_conn, client):
        ok = True
        print("Required methods:")
        for name in REQUIRED:
            has = callable(getattr(client, name, None))
            ok = ok and has
            print(f"  [{'ok ' if has else 'MISSING'}] {name}")
        print("Optional methods:")
        for name in OPTIONAL:
            has = callable(getattr(client, name, None))
            print(f"  [{'ok ' if has else '--- '}] {name}")

        print("\nExisting workouts (most recent):")
        try:
            for w in (client.get_workouts(0, 8) or []):
                print(f"  {w.get('workoutId')}  {w.get('workoutName')}")
        except Exception as e:
            print(f"  (could not list workouts: {e})")

        print("\nVerdict:", "READY" if ok else "NOT READY — a required method is missing.")


# ------------------------------------------------------------------ reflect
def cmd_reflect(args):
    with session() as (_conn, client):
        wk = client.get_workout_by_id(args.workout_id)
        if not wk:
            sys.exit(f"No workout {args.workout_id} found.")
        path = OUT_DIR / f"template_{args.workout_id}.json"
        _dump(wk, path)
        print(f"\nName: {_find_first(wk, 'workoutName')!r}")
        print("Use this as: schedule --from-template", path.name)


# ------------------------------------------------------------------ schedule
def _builtin_interval() -> dict:
    def step(order, stype_id, stype_key, dur=None):
        return {
            "type": "ExecutableStepDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": stype_id, "stepTypeKey": stype_key},
            "endCondition": ({"conditionTypeId": 2, "conditionTypeKey": "time"} if dur
                             else {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}),
            "endConditionValue": dur,
        }

    work_rest = {
        "type": "RepeatGroupDTO", "stepOrder": 2,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "numberOfIterations": 6, "smartRepeat": False,
        "workoutSteps": [step(3, 3, "interval", 7.0), step(4, 4, "recovery", 3.0)],
    }
    return {
        "workoutName": "PROBE — repeaters (builtin)",
        "sportType": {"sportTypeId": 3, "sportTypeKey": "other"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 3, "sportTypeKey": "other"},
            "workoutSteps": [step(1, 1, "warmup", 180.0), work_rest],
        }],
    }


def cmd_schedule(args):
    when = args.date or (date.today() + timedelta(days=1)).isoformat()

    if args.from_template:
        template = json.loads(pathlib.Path(args.from_template).read_text())
        payload = _strip_ids(copy.deepcopy(template))
        base_name = _find_first(template, "workoutName") or "workout"
        payload["workoutName"] = args.name or f"PROBE — {base_name}"
    elif args.builtin:
        payload = _builtin_interval()
        if args.name:
            payload["workoutName"] = args.name
    else:
        sys.exit("Give --from-template <file> (recommended) or --builtin interval.")

    with session() as (_conn, client):
        print(f"Uploading {payload['workoutName']!r} ...")
        workout_id = garmin_write.upload(client, payload)
        print(f"  workoutId = {workout_id}")

        print(f"Scheduling for {when} ...")
        schedule_id = garmin_write.schedule(client, workout_id, date.fromisoformat(when))
        print(f"  scheduleId = {schedule_id}")

        state = {"workout_id": workout_id, "schedule_id": schedule_id,
                 "scheduled_date": when, "workout_name": payload["workoutName"]}
        _dump(state, STATE_PATH)
        print("\nNext: sync watch, run it on the Epix, sync back, then: inspect")


# ------------------------------------------------------------------ inspect
def cmd_inspect(args):
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    target_wid = str(args.workout_id or state.get("workout_id") or "")
    target_sid = str(state.get("schedule_id") or "")
    target_name = state.get("workout_name", "")

    with session() as (_conn, client):
        acts = client.get_activities(0, args.limit) or []
        print(f"Scanning {len(acts)} recent activities...\n")

        chosen = None
        if args.activity_id:
            chosen = next((a for a in acts if str(a.get("activityId")) == str(args.activity_id)), None)
            if not chosen:  # not in the recent window — fetch it directly if possible
                getter = getattr(client, "get_activity", None) or getattr(client, "get_activity_details", None)
                if getter:
                    with contextlib.suppress(Exception):
                        chosen = getter(args.activity_id)
        if not chosen and target_wid:
            for a in acts:
                if target_wid in {str(v) for _, _, v in iter_items(a)}:
                    chosen = a
                    break
        if not chosen and target_name:
            for a in acts:
                nm = (a.get("activityName") or "")
                if target_name.lower() in nm.lower() or (nm and nm.lower() in target_name.lower()):
                    chosen = a
                    break

        if not chosen:
            print("No match. Most recent activities (re-run with --activity-id <ID>):")
            for a in acts[:12]:
                print(f"  {a.get('activityId')}  {a.get('startTimeLocal')}  "
                      f"{a.get('activityName')}  [{(a.get('activityType') or {}).get('typeKey')}]")
            if not target_wid:
                print("\n(Tip: also pass --workout-id 1709094396 so I know what to look for.)")
            return

        aid = chosen.get("activityId")
        _dump(chosen, OUT_DIR / f"activity_{aid}_summary.json")

        fn = getattr(client, "get_activity_exercise_sets", None)
        if callable(fn):
            with contextlib.suppress(Exception):
                _dump(fn(aid), OUT_DIR / f"activity_{aid}_exercise_sets.json")

        all_vals = {str(v) for _, _, v in iter_items(chosen)}
        wid_hit = bool(target_wid) and target_wid in all_vals
        sid_hit = target_sid not in ("", "None") and target_sid in all_vals
        link_keys = [(p, v) for p, k, v in iter_items(chosen)
                     if ("workout" in k.lower() or "schedul" in k.lower()) and v not in (None, "", 0)]

        print(f"Matched activity {aid}  ({chosen.get('activityName')})  "
              f"[{(chosen.get('activityType') or {}).get('typeKey')}]\n")
        print("workout/schedule-ish fields on the activity:")
        for p, v in link_keys or [("(none found)", "")]:
            print(f"  {p} = {v}")
        print()
        print(f"  looking for workoutId {target_wid or '(unknown)'} : {'YES' if wid_hit else 'no'}")
        if target_sid not in ("", "None"):
            print(f"  looking for scheduleId {target_sid} : {'YES' if sid_hit else 'no'}")
        print("\nVERDICT:", "PASS — clean join key exists; match plan<->execution on workoutId."
        if wid_hit else
        "PARTIAL/FAIL — workoutId not found on the activity. Check the fields above and "
        "the dumped JSON; if nothing links back, fall back to name+date matching.")


# ------------------------------------------------------------------ cleanup
def cmd_cleanup(args):
    with session() as (_conn, client):
        if args.all_probe:
            deleted = 0
            for w in (client.get_workouts(0, 100) or []):
                if (w.get("workoutName") or "").startswith("PROBE —"):
                    with contextlib.suppress(Exception):
                        garmin_write.delete(client, w["workoutId"])
                        deleted += 1
                        print(f"  deleted {w['workoutId']}  {w['workoutName']}")
            print(f"Deleted {deleted} probe workout(s).")
            return

        if not STATE_PATH.exists():
            sys.exit("No state.json; nothing to clean (or use --all-probe).")
        state = json.loads(STATE_PATH.read_text())
        if state.get("schedule_id"):
            with contextlib.suppress(Exception):
                garmin_write.unschedule(client, state["schedule_id"])
                print(f"  unscheduled {state['schedule_id']}")
        if state.get("workout_id"):
            with contextlib.suppress(Exception):
                garmin_write.delete(client, state["workout_id"])
                print(f"  deleted workout {state['workout_id']}")
        STATE_PATH.unlink(missing_ok=True)
        print("Cleaned up.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight").set_defaults(fn=cmd_preflight)

    r = sub.add_parser("reflect"); r.add_argument("workout_id"); r.set_defaults(fn=cmd_reflect)

    s = sub.add_parser("schedule")
    s.add_argument("--from-template")
    s.add_argument("--builtin", choices=["interval"])
    s.add_argument("--date", help="YYYY-MM-DD (default: tomorrow)")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_schedule)

    i = sub.add_parser("inspect")
    i.add_argument("--workout-id", help="workout to look for (else from state.json)")
    i.add_argument("--activity-id")
    i.add_argument("--limit", type=int, default=15)
    i.set_defaults(fn=cmd_inspect)

    c = sub.add_parser("cleanup")
    c.add_argument("--all-probe", action="store_true", help="delete every 'PROBE —' workout")
    c.set_defaults(fn=cmd_cleanup)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()