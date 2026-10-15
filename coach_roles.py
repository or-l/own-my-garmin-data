"""
Postgres roles + GRANTs for the optional coach loop. This file is the
grants spec (see the table in docs/superpowers/plans/2026-09-30-workout-coach-loop.md,
Task 6). Isolation between tiers is enforced here, by the database — not
by the tiers choosing to behave:

  coach      MCP write tool. Authors training_plan (authored columns only)
             and protocols. NO access to auth_tokens (can't obtain a
             Garmin-capable credential) or plan_approvals (can't approve).
  scheduler  Enactor (seed_workouts.py / sync_schedule.py). Reads the Garmin
             token, updates it in place, writes the ledger and the
             enactor-owned training_plan columns. Can't approve.
  owner      Your DATABASE_URL. Only plan_admin.py uses it to approve.

apply_coach_grants() revokes everything from coach/scheduler and re-grants,
so it's convergent and safe to re-run after every migration (install.py
--upgrade does that). Imported only by install.py and the smoke test.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import psycopg
from psycopg import sql

REQUIRED_TABLES = ("workouts", "scheduled_workouts", "protocols", "training_plan", "generated_workouts", "plan_approvals")
COACH_DENIED_TABLES = frozenset({"auth_tokens", "plan_approvals"})
COACH_PLAN_COLUMNS = ("scheduled_date", "source", "protocol_slug", "workout_id", "workout_name", "skip", "note")
SCHEDULER_PLAN_COLUMNS = ("status", "schedule_id", "resolved_workout_id", "enacted_date")
SCHEDULER_TOKEN_COLUMNS = ("payload", "updated_at")


@dataclass(frozen=True)
class CoachLoopRoles:
    coach: str = "coach"
    scheduler: str = "scheduler"
    read: str | None = None

    def to_config(self) -> dict:
        return {"coach_role": self.coach, "scheduler_role": self.scheduler, "read_role": self.read}

    @classmethod
    def from_config(cls, cfg: dict) -> "CoachLoopRoles":
        return cls(coach=cfg.get("coach_role", "coach"), scheduler=cfg.get("scheduler_role", "scheduler"),
                   read=cfg.get("read_role"))


def generate_password() -> str:
    return secrets.token_urlsafe(32)


def role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute("select exists (select 1 from pg_roles where rolname = %s)", (role,)).fetchone()[0]


def ensure_role(conn: psycopg.Connection, role: str, password: str) -> bool:
    verb = "alter" if role_exists(conn, role) else "create"
    conn.execute(sql.SQL(verb + " role {} with login password {}").format(sql.Identifier(role), sql.Literal(password)))
    return verb == "create"


def _cols(columns: tuple[str, ...]) -> sql.Composable:
    return sql.SQL(", ").join(map(sql.Identifier, columns))


def apply_coach_grants(conn: psycopg.Connection, roles: CoachLoopRoles) -> None:
    tables = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'").fetchall()]
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise RuntimeError(f"Coach-loop tables missing ({', '.join(missing)}) — enable the 'workouts' domain first.")

    coach, sched = sql.Identifier(roles.coach), sql.Identifier(roles.scheduler)

    def ex(template: str, **kw) -> None:
        conn.execute(sql.SQL(template).format(**kw))

    for r in (coach, sched):
        ex("revoke all on all tables in schema public from {r}", r=r)
        ex("revoke all on all sequences in schema public from {r}", r=r)
        ex("grant usage on schema public to {r}", r=r)

    for t in tables:
        ex("grant select on {t} to {r}", t=sql.Identifier(t), r=sched)
        if t not in COACH_DENIED_TABLES:
            ex("grant select on {t} to {r}", t=sql.Identifier(t), r=coach)

    # Views aren't in pg_tables; grant them read access the same way
    # (e.g. activity_exercise_set_rows). The coach may read every view.
    views = [r[0] for r in conn.execute("select viewname from pg_views where schemaname = 'public'").fetchall()]
    for v in views:
        ex("grant select on {t} to {r}", t=sql.Identifier(v), r=sched)
        ex("grant select on {t} to {r}", t=sql.Identifier(v), r=coach)

    ex("grant insert, update, delete on protocols to {r}", r=coach)
    ex("grant insert ({c}), update ({c}) on training_plan to {r}", c=_cols(COACH_PLAN_COLUMNS), r=coach)
    ex("grant delete on training_plan to {r}", r=coach)
    seq_schema, seq_name = conn.execute(
        "select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where c.oid = pg_get_serial_sequence('training_plan', 'id')::regclass"
    ).fetchone()
    ex("grant usage on sequence {s} to {r}", s=sql.Identifier(seq_schema, seq_name), r=coach)

    ex("grant update ({c}) on auth_tokens to {r}", c=_cols(SCHEDULER_TOKEN_COLUMNS), r=sched)
    ex("grant update ({c}) on training_plan to {r}", c=_cols(SCHEDULER_PLAN_COLUMNS), r=sched)
    ex("grant insert, update on generated_workouts to {r}", r=sched)

    if roles.read and role_exists(conn, roles.read):
        for t in tables:
            if t != "auth_tokens":
                ex("grant select on {t} to {r}", t=sql.Identifier(t), r=sql.Identifier(roles.read))
        for v in views:
            ex("grant select on {t} to {r}", t=sql.Identifier(v), r=sql.Identifier(roles.read))


def assert_role_isolated(conn: psycopg.Connection, role: str) -> None:
    """Fail loudly if a coach-loop role inherits anything (e.g. Neon's
    neon_superuser, pg_read_all_data) that would bypass the table grants."""
    flags = conn.execute(
        "select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb from pg_roles where rolname = %s", (role,)
    ).fetchone()
    if flags is None:
        raise RuntimeError(f"role {role} does not exist")
    if any(flags):
        raise RuntimeError(f"role {role} has elevated attributes (super/bypassrls/createrole/createdb): {flags}")
    memberships = [r[0] for r in conn.execute(
        "select b.rolname from pg_roles b where b.rolname <> %s and pg_has_role(%s, b.oid, 'member')", (role, role)
    ).fetchall()]
    if memberships:
        raise RuntimeError(f"role {role} is a member of {memberships} — that can bypass the coach-loop grants")


def url_for_role(owner_url: str, role: str, password: str) -> str:
    parts = urlsplit(owner_url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return parts._replace(netloc=f"{quote(role, safe='')}:{quote(password, safe='')}@{host}").geturl()


def role_of_url(url: str | None) -> str | None:
    if not url:
        return None
    netloc = urlsplit(url).netloc
    if "@" not in netloc:
        return None
    return unquote(netloc.rsplit("@", 1)[0].split(":", 1)[0])
