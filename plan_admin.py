# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
#     "questionary>=2.0",
# ]
# ///
"""
Approval CLI — the ONLY writer of plan_approvals (the coach loop's hard
gate). Connects with your owner DATABASE_URL; the coach and scheduler roles
can't write that table.

    uv run plan_admin.py list [--all]            # upcoming rows + approval state
    uv run plan_admin.py approve                 # checklists: tick rows to approve,
                                                 # then (optionally) rows to skip,
                                                 # then offers to publish to Garmin
    uv run plan_admin.py publish                 # upload + schedule approved rows
    uv run plan_admin.py approve --id 12 --id 13 [--note "..."] [--yes]
    uv run plan_admin.py approve --date 2026-10-02
    uv run plan_admin.py unapprove --id 12       # alias: reject

`approve` shows the rows and asks you to type 'yes' (or pass --yes), then
records a fingerprint of exactly what it showed you (planning.py). If
the coach edits the row or its protocol afterwards, `list` shows it as
STALE and the enactor skips it until you approve again.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
from datetime import date

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import planning


def ids_for_date(conn: psycopg.Connection, day: date) -> list[int]:
    return [r[0] for r in conn.execute("select id from training_plan where scheduled_date = %s order by id", (day,)).fetchall()]


def preview(conn: psycopg.Connection, plan_ids: list[int]) -> list[planning.PlanView]:
    """The rows as they are now, in plan_ids order. approve_views() stores
    these exact fingerprints, so what you were shown is what gets approved."""
    views = {v.row.id: v for v in planning.load_plan(conn, ids=plan_ids)}
    missing = sorted(set(plan_ids) - set(views))
    if missing:
        raise SystemExit(f"No training_plan row(s) with id {missing}")
    return [views[pid] for pid in plan_ids]


def approve_views(conn: psycopg.Connection, views: list[planning.PlanView], note: str | None) -> list[str]:
    """Approve the previewed rows. If the coach edited one after preview(),
    its stored fingerprint no longer matches and it shows as STALE."""
    lines = []
    for v in views:
        pid = v.row.id
        conn.execute(
            """
            insert into plan_approvals (plan_id, fingerprint, note) values (%s, %s, %s)
                on conflict (plan_id) do update set fingerprint = excluded.fingerprint, note = excluded.note,
                approved_by = current_user, approved_at = now()
            """,
            (pid, v.fingerprint, note),
        )
        lines.append(f"approved plan {pid}: {v.describe()}")
    conn.commit()
    return lines


def approve(conn: psycopg.Connection, plan_ids: list[int], note: str | None) -> list[str]:
    return approve_views(conn, preview(conn, plan_ids), note)


def pending_rows(views: list[planning.PlanView], today: date) -> list[planning.PlanView]:
    """Rows waiting on the owner: upcoming, not cancelled, and unapproved or
    changed since approval (STALE)."""
    return [v for v in views
            if v.row.scheduled_date >= today and not v.row.skip and v.approval in ("unapproved", "stale")]


def _num(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def _exercise_summary(ex: dict) -> str:
    sets = _num(ex.get("sets"))
    target = f"{sets}×{_num(ex['reps'])}" if ex.get("reps") is not None else f"{sets}×{_num(ex.get('time_s'))}s"
    weight = f" @{_num(ex['weight_kg'])}kg" if ex.get("weight_kg") is not None else ""
    return f"{ex.get('exercise')} {target}{weight}"


def _summary(v: planning.PlanView) -> str:
    r = v.row
    if r.source != "generated":
        return f"{v.library_name or r.workout_name or f'workout {r.workout_id}'} (library)"
    p = v.protocol or {}
    name, params = p.get("name") or r.protocol_slug, p.get("params") or {}
    try:
        if p.get("kind") == "strength":
            exercises = params.get("exercises") or []
            shown = ", ".join(_exercise_summary(e) for e in exercises[:2])
            more = ", …" if len(exercises) > 2 else ""
            return f"{name} — {len(exercises)} exercises ({shown}{more})"
        if p.get("kind") == "other":
            return f"{name} — {_num(params.get('work_s'))}s on / {_num(params.get('rest_s'))}s off ×{_num(params.get('reps'))}"
    except (AttributeError, KeyError, TypeError):
        pass  # odd params: fall back to the name; the seed reports what's wrong
    return name


def label(v: planning.PlanView) -> str:
    """One readable checklist line: id, day, STALE marker, short summary."""
    stale = "STALE " if v.approval == "stale" else ""
    return f"{v.row.id:>3}  {v.row.scheduled_date.strftime('%a %d %b')}  {stale}{_summary(v)}"


def skip_rows(conn: psycopg.Connection, plan_ids: list[int]) -> int:
    """Cancel rows (skip = true). The next enact removes any that are
    already on the Garmin calendar."""
    n = conn.execute("update training_plan set skip = true where id = any(%s::bigint[])", (list(plan_ids),)).rowcount
    conn.commit()
    return n


def review(conn: psycopg.Connection, ask_approve, ask_skip, confirm, *, today: date | None = None,
           note: str | None = None) -> str:
    """Two checklists: rows to approve, then (of the rest) rows to skip;
    everything unticked is left as it is. Nothing is written until confirm()
    agrees to the summary. Approvals pin the versions that were listed, so a
    coach edit made while the checklist is open shows up as STALE."""
    today = today or date.today()
    views = pending_rows(planning.load_plan(conn, since=today), today)
    if not views:
        print("Nothing waiting for approval.")
        return ""
    choices = [(label(v), v.row.id) for v in views]
    to_approve = set(ask_approve(choices) or [])
    rest = [c for c in choices if c[1] not in to_approve]
    to_skip = set(ask_skip(rest) or []) if rest else set()
    leave = [cid for _, cid in rest if cid not in to_skip]
    if not to_approve and not to_skip:
        print("Nothing selected — no changes.")
        return ""
    ordered = [cid for _, cid in choices]
    parts = [f"{verb} {', '.join(str(i) for i in ordered if i in ids)}"
             for verb, ids in (("approve", to_approve), ("skip", to_skip), ("leave", set(leave))) if ids]
    summary = " · ".join(parts)
    for v in views:
        if v.row.id in to_approve:
            print(f"  approve {v.describe()}")
        elif v.row.id in to_skip:
            print(f"  skip    {label(v).strip()}")
    if not confirm(summary):
        print("Cancelled — no changes.")
        return summary
    for line in approve_views(conn, [v for v in views if v.row.id in to_approve], note):
        print(line)
    if to_skip:
        print(f"Skipped {skip_rows(conn, sorted(to_skip))} row(s).")
    return summary


ROOT = pathlib.Path(__file__).resolve().parent


def publish(run=subprocess.run) -> int:
    """Upload, then schedule, approved rows: runs seed_workouts.py and
    sync_schedule.py as they'd run by hand (scheduler login from .env).
    Neither gets --confirm, so each prints its dry-run plan and asks before
    writing to Garmin. Stops if the upload step fails."""
    if not os.environ.get("SCHEDULER_DATABASE_URL"):
        print("SCHEDULER_DATABASE_URL isn't set (.env) — run `uv run install.py --coach-loop` first.")
        return 1
    for script in ("seed_workouts.py", "sync_schedule.py"):
        print(f"\n— {script} —", flush=True)
        code = run(["uv", "run", str(ROOT / script)], cwd=ROOT).returncode
        if code != 0:
            print(f"{script} exited with {code}; stopping.")
            return code
    return 0


def _offer_publish() -> None:
    import questionary
    if questionary.confirm("Publish approved rows to Garmin now? (you'll confirm each step)", default=True).ask():
        publish()


def _interactive_review(conn: psycopg.Connection, yes: bool, note: str | None) -> None:
    if not sys.stdin.isatty():
        raise SystemExit("No terminal for the checklist — give --id / --date instead.")
    import questionary  # only the interactive path needs it

    def ask(question: str):
        def _ask(choices):
            if not choices:
                return []
            picked = questionary.checkbox(
                question, choices=[questionary.Choice(title=text, value=cid) for text, cid in choices],
                instruction="(space = tick, a = all, enter = done)",
            ).ask()
            if picked is None:  # Ctrl+C / Esc
                raise SystemExit("Cancelled — no changes.")
            return picked
        return _ask

    def confirm(summary: str) -> bool:
        print(f"\n{summary}")
        return yes or bool(questionary.confirm("Apply this?", default=False).ask())

    review(conn, ask("Approve which rows?"), ask("Cancel (skip) any of the rest? (enter = leave them)"),
           confirm, note=note)
    # Offered even when nothing new was approved: rows approved earlier may
    # still be waiting to be uploaded/scheduled.
    _offer_publish()


def _confirmed(yes_flag: bool) -> bool:
    if yes_flag:
        return True
    if not sys.stdin.isatty():
        print("Not approved — re-run with --yes after reviewing the rows above.")
        return False
    return input("Type 'yes' to approve exactly these rows: ").strip().lower() == "yes"


def unapprove(conn: psycopg.Connection, plan_ids: list[int]) -> int:
    n = conn.execute("delete from plan_approvals where plan_id = any(%s::bigint[])", (list(plan_ids),)).rowcount
    conn.commit()
    return n


def _print_list(conn: psycopg.Connection, show_all: bool) -> None:
    views = planning.load_plan(conn, since=None if show_all else date.today())
    if not views:
        print("No plan rows.")
        return
    print(f"{'id':>5}  {'date':10}  {'status':9}  {'approval':10}  item")
    for v in views:
        status = f"{v.row.status}+SKIP" if v.row.skip and v.row.status != "skipped" else v.row.status
        print(f"{v.row.id:>5}  {v.row.scheduled_date}  {status:9}  {v.approval.upper():10}  {v.describe().split(' ', 1)[1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true", help="Include past rows.")
    sub.add_parser("publish", help="Upload + schedule approved rows (each step shows its plan and asks).")
    for name in ("approve", "unapprove", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, action="append", default=[])
        p.add_argument("--date", type=date.fromisoformat)
        if name == "approve":
            p.add_argument("--note")
            p.add_argument("--yes", action="store_true", help="Skip the typed confirmation (you've reviewed the rows).")
    args = parser.parse_args()

    load_dotenv()
    if args.cmd == "publish":
        sys.exit(publish())
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.cmd == "list":
            _print_list(conn, args.all)
            return
        ids = list(args.id) + (ids_for_date(conn, args.date) if args.date else [])
        if not ids and args.cmd == "approve" and not args.id and not args.date:
            _interactive_review(conn, args.yes, args.note)
            return
        if not ids:
            raise SystemExit("Give --id and/or --date.")
        if args.cmd == "approve":
            views = preview(conn, ids)
            print("About to approve:")
            for v in views:
                print(f"  plan {v.row.id} [{v.approval.upper()}]: {v.describe()}")
            if not _confirmed(args.yes):
                return
            for line in approve_views(conn, views, args.note):
                print(line)
        else:
            print(f"Removed {unapprove(conn, ids)} approval(s).")


if __name__ == "__main__":
    main()
