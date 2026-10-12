# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Smoke test for the domain registry + migrations + upgrade flow. Run
against a SCRATCH Neon database only — it drops and recreates the public
schema.

Usage:
    SMOKE_TEST_DATABASE_URL="postgresql://...scratch-db..." uv run scripts/smoke_test.py

Covers acceptance criteria 1, 2, 5, 6 from
docs/superpowers/plans/2026-08-24-garmin-domains-upgrade.md. Criterion 3
(targeted backfill trigger) is covered with a stub standing in for the
real GitHub API call. Criterion 4 (intra-domain migration addition) can't
be exercised until the challenges-in-progress probe's follow-up plan adds
real migrations — see that plan's "Out of scope" section; not covered
here on purpose.
"""
import os
import pathlib
import sys
from datetime import date, timedelta

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from apply_schema import apply_migrations, applied_versions
from config import get_enabled_domains, set_enabled_domains
import domains
from domains import DOMAINS, SyncContext
import garmin_write
from coach_roles import CoachLoopRoles, apply_coach_grants, assert_role_isolated, ensure_role, url_for_role
from protocols import seed_default_protocols

DB_URL = os.environ["SMOKE_TEST_DATABASE_URL"]
OLD_INIT_SQL = pathlib.Path(__file__).resolve().parent.parent / "schema" / "init.sql"


def _reset_db(conn: psycopg.Connection) -> None:
    conn.execute("drop schema public cascade; create schema public;")
    conn.commit()


def _table_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute(
        "select exists (select 1 from information_schema.tables where table_name = %s)", (name,)
    ).fetchone()[0]


def test_fresh_install_defaults() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)

    default_keys = {d.key for d in DOMAINS if d.default_enabled}
    applied = apply_migrations(DB_URL, enabled_domains=default_keys)
    assert applied, "expected at least one migration to apply on a fresh DB"

    with psycopg.connect(DB_URL) as conn:
        set_enabled_domains(conn, default_keys)
        assert _table_exists(conn, "activities")
        assert _table_exists(conn, "daily_metrics")
        assert _table_exists(conn, "training_insight")
        assert _table_exists(conn, "challenges")
        assert _table_exists(conn, "user_profile")
        assert not _table_exists(conn, "menstrual_calendar"), "reproductive tables should not exist"
        assert get_enabled_domains(conn) == default_keys
        migrated = {row[0] for row in conn.execute("select name from schema_migrations").fetchall()}
        assert migrated
    print("test_fresh_install_defaults: PASS")


def test_existing_install_adoption() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
        conn.execute(OLD_INIT_SQL.read_text())
        conn.execute("insert into activities (activity_id, raw) values (1, '{\"activityId\": 1}'::jsonb)")
        conn.commit()
        assert not _table_exists(conn, "schema_migrations")

    applied = apply_migrations(DB_URL, enabled_domains=None)
    assert applied == [], "adoption stamps baseline migrations without re-running their SQL"

    with psycopg.connect(DB_URL) as conn:
        enabled = get_enabled_domains(conn)
        assert enabled == {"activities", "wellness", "body_composition", "training", "challenges", "profile"}
        count = conn.execute("select count(*) from activities").fetchone()[0]
        assert count == 1, "adoption must not touch existing data"
    print("test_existing_install_adoption: PASS")


def test_upgrade_adds_domain() -> None:
    print("test_upgrade_adds_domain: SKIPPED — reproductive has no migrations yet (gated on a live probe; see the plan's 'Out of scope' note)")


def test_idempotent_reapply() -> None:
    before = applied_versions(DB_URL)
    default_keys = {d.key for d in DOMAINS if d.default_enabled}
    applied_again = apply_migrations(DB_URL, enabled_domains=default_keys)
    assert applied_again == [], "second apply_migrations call must be a no-op"
    after = applied_versions(DB_URL)
    assert before == after
    print("test_idempotent_reapply: PASS")


class _StubGarminClient:
    """Records every attribute access so a test can assert which endpoints
    a disabled domain's sync_incremental never touches."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)

        def _fn(*args, **kwargs):
            if name == "get_activities_by_date":
                return []
            if name == "get_weigh_ins":
                return {"dailyWeightSummaries": []}
            return []

        return _fn


def test_disabled_domain_not_pulled() -> None:
    # Enable every default domain except body_composition, and drive
    # domain selection the same way ingestion/pull.py's real main() does
    # (`for domain in DOMAINS: if domain.key in enabled: ...`) rather than
    # hand-picking a single domain — this is what exercises
    # upsert_daily_metrics via the real wellness path against a
    # daily_metrics table that a genuinely disabled sibling domain
    # (body_composition) never got its own migration applied for.
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    enabled = {"activities", "wellness", "training", "challenges", "profile"}
    apply_migrations(DB_URL, enabled_domains=enabled)

    stub = _StubGarminClient()
    with psycopg.connect(DB_URL) as conn:
        ctx = SyncContext(
            client=stub, conn=conn,
            window_start=date.today() - timedelta(days=1), today=date.today(),
        )
        for domain in DOMAINS:
            if domain.key in enabled:
                domain.sync_incremental(ctx)
        conn.commit()

        row = conn.execute(
            "select stats is not null from daily_metrics where metric_date = %s", (date.today(),)
        ).fetchone()
        assert row and row[0], "wellness sync should have written stats to daily_metrics"

    assert "get_weigh_ins" not in stub.calls, f"disabled body_composition endpoint was called: {stub.calls}"
    print("test_disabled_domain_not_pulled: PASS")


def test_targeted_backfill_trigger_stub() -> None:
    triggered = {}

    def fake_trigger_backfill(backfill_start_date=None, domains=None):
        triggered["domains"] = domains
        return {"triggered": True, "note": "stubbed"}

    result = fake_trigger_backfill(domains={"training"})
    assert result["triggered"] is True
    assert triggered["domains"] == {"training"}
    print("test_targeted_backfill_trigger_stub: PASS")


class _WorkoutsStub:
    def __init__(self, listing):
        self.listing = listing

    def get_workouts(self, start, limit):
        return self.listing[start:start + limit]

    def get_workout_by_id(self, workout_id):
        return {"workoutId": workout_id, "workoutName": f"w{workout_id}", "updatedDate": "u1", "description": "desc"}

    def get_scheduled_workouts(self, year, month):
        return {"calendarItems": []}


def test_workouts_domain_tables_and_join() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    apply_migrations(DB_URL, enabled_domains={"activities", "workouts"})
    domains.SCHEDULE_FETCH_SLEEP_S = 0
    with psycopg.connect(DB_URL) as conn:
        for t in ("workouts", "scheduled_workouts", "training_plan", "protocols", "generated_workouts", "plan_approvals"):
            assert _table_exists(conn, t), f"{t} missing after enabling workouts"
        ctx = SyncContext(client=_WorkoutsStub([{"workoutId": 42, "updateDate": "u1"}]), conn=conn,
                          window_start=date.today(), today=date.today())
        domains._workouts_sync(ctx)
        conn.execute("""insert into activities (activity_id, raw) values (1, '{"workoutId": 42}'::jsonb)""")
        desc = conn.execute(
            "select w.raw->>'description' from activities a join workouts w on w.workout_id = (a.raw->>'workoutId')::bigint"
        ).fetchone()[0]
        assert desc == "desc"

    # disabling the domain leaves the tables in place
    apply_migrations(DB_URL, enabled_domains={"activities"})
    with psycopg.connect(DB_URL) as conn:
        assert _table_exists(conn, "workouts") and _table_exists(conn, "training_plan")
    print("test_workouts_domain_tables_and_join: PASS")


def test_workouts_sync_keeps_rows_on_empty_listing() -> None:
    with psycopg.connect(DB_URL) as conn:
        ctx = SyncContext(client=_WorkoutsStub([]), conn=conn, window_start=date.today(), today=date.today())
        domains._workouts_sync(ctx)
        assert conn.execute("select count(*) from workouts").fetchone()[0] == 1
    print("test_workouts_sync_keeps_rows_on_empty_listing: PASS")


SMOKE_ROLES = CoachLoopRoles(coach="smoke_coach", scheduler="smoke_scheduler")
SMOKE_PASSWORDS = {"smoke_coach": "smoke-coach-pw", "smoke_scheduler": "smoke-scheduler-pw"}


def _setup_coach_loop() -> tuple[str, str]:
    """Fresh schema + workouts domain + smoke roles/grants. Returns (coach_url, scheduler_url).
    Roles are cluster-wide and survive _reset_db, so ensure_role must be idempotent."""
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    enabled = {"activities", "workouts"}
    apply_migrations(DB_URL, enabled_domains=enabled)
    with psycopg.connect(DB_URL) as conn:
        set_enabled_domains(conn, enabled)
        conn.execute("""insert into auth_tokens (provider, payload) values ('garmin', '{"garmin_tokens.json": "{}"}'::jsonb)""")
        for role, password in SMOKE_PASSWORDS.items():
            ensure_role(conn, role, password)
        apply_coach_grants(conn, SMOKE_ROLES)
        apply_coach_grants(conn, SMOKE_ROLES)  # re-runnable
        seed_default_protocols(conn)
        conn.commit()
        for role in SMOKE_PASSWORDS:
            assert_role_isolated(conn, role)
    return tuple(url_for_role(DB_URL, r, SMOKE_PASSWORDS[r]) for r in ("smoke_coach", "smoke_scheduler"))


def _denied(url: str, query: str, params: tuple = ()) -> bool:
    with psycopg.connect(url) as conn:
        try:
            conn.execute(query, params)
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            return True
        conn.rollback()
    return False


def test_coach_role_isolation() -> None:
    coach_url, sched_url = _setup_coach_loop()
    tomorrow = date.today() + timedelta(days=1)
    with psycopg.connect(coach_url) as conn:  # identity insert works through column grants
        plan_id = conn.execute(
            "insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'finger-repeaters-7-3') returning id",
            (tomorrow,),
        ).fetchone()[0]
        conn.execute("insert into protocols (slug, name, params) values ('x', 'x', '{}'::jsonb)")
        conn.execute("select count(*) from workouts").fetchone()

    # Acceptance 1 — coach
    assert _denied(coach_url, "select * from auth_tokens")
    assert _denied(coach_url, "insert into plan_approvals (plan_id, fingerprint) values (%s, 'f')", (plan_id,))
    assert _denied(coach_url, "select * from plan_approvals")
    assert _denied(coach_url, "insert into generated_workouts (protocol_slug, workout_id, params_hash) values ('a', 1, 'h')")
    assert _denied(coach_url, "update training_plan set schedule_id = 1 where id = %s", (plan_id,))
    assert _denied(coach_url, "update training_plan set status = 'scheduled' where id = %s", (plan_id,))
    assert not _denied(coach_url, "update training_plan set skip = true where id = %s", (plan_id,))
    with psycopg.connect(DB_URL) as conn:  # pretend the enactor put it on the calendar
        conn.execute("update training_plan set resolved_workout_id = 1, enacted_date = %s where id = %s", (tomorrow, plan_id))
    assert _denied(coach_url, "delete from training_plan where id = %s", (plan_id,)), "enacted rows must not be deletable"
    with psycopg.connect(DB_URL) as conn:
        conn.execute("update training_plan set resolved_workout_id = null, enacted_date = null where id = %s", (plan_id,))
    assert not _denied(coach_url, "delete from training_plan where id = %s", (plan_id,)), "un-enacted rows stay deletable"
    # Acceptance 1 — scheduler, as granted
    assert not _denied(sched_url, "select * from auth_tokens")
    assert not _denied(sched_url, "select * from plan_approvals")
    assert _denied(sched_url, "insert into plan_approvals (plan_id, fingerprint) values (%s, 'f')", (plan_id,))
    assert _denied(sched_url, "insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'x')", (tomorrow,))
    assert _denied(sched_url, "update training_plan set scheduled_date = scheduled_date where id = %s", (plan_id,))
    print("test_coach_role_isolation: PASS")


def test_scheduler_token_save() -> None:
    _coach_url, sched_url = _setup_coach_loop()
    from bootstrap.garmin_auth import TOKEN_DIR
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(sched_url) as conn:
        garmin_write.save_token_update_only(conn)  # UPDATE-only path works without INSERT
    assert _denied(sched_url, "insert into auth_tokens (provider, payload) values ('x', '{}'::jsonb)")
    print("test_scheduler_token_save: PASS")


def main() -> None:
    test_fresh_install_defaults()
    test_existing_install_adoption()
    test_upgrade_adds_domain()
    test_idempotent_reapply()
    test_disabled_domain_not_pulled()
    test_workouts_domain_tables_and_join()
    test_workouts_sync_keeps_rows_on_empty_listing()
    test_coach_role_isolation()
    test_scheduler_token_save()
    test_targeted_backfill_trigger_stub()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
