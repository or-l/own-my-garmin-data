# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Approval CLI — the ONLY writer of plan_approvals (the coach loop's hard
gate). Connects with your owner DATABASE_URL; the coach and scheduler roles
can't write that table.

    uv run plan_admin.py list [--all]            # upcoming rows + approval state
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
    for name in ("approve", "unapprove", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, action="append", default=[])
        p.add_argument("--date", type=date.fromisoformat)
        if name == "approve":
            p.add_argument("--note")
            p.add_argument("--yes", action="store_true", help="Skip the typed confirmation (you've reviewed the rows).")
    args = parser.parse_args()

    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.cmd == "list":
            _print_list(conn, args.all)
            return
        ids = list(args.id) + (ids_for_date(conn, args.date) if args.date else [])
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
