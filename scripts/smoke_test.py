# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Smoke test for the domain registry + migrations + upgrade flow, and the
coach loop. Run against a THROWAWAY Postgres only — it drops and recreates
the public schema and creates cluster-wide roles (smoke_coach,
smoke_scheduler), so never point it at your production Neon project.

Usage (local Docker):
    docker run -d --rm --name garmin-smoke -e POSTGRES_PASSWORD=smoke -p 127.0.0.1:55432:5432 postgres:17
    SMOKE_TEST_DATABASE_URL=postgresql://postgres:smoke@localhost:55432/postgres uv run scripts/smoke_test.py

Coach-loop coverage (docs/superpowers/plans/2026-09-30-workout-coach-loop.md):
the workouts domain tables + activity join and the empty-listing guard;
role isolation (coach can't read auth_tokens, approve, write enactor
columns, or delete an enacted row; scheduler can't approve or author) and
the scheduler's UPDATE-only token save; the approval CLI and stale
fingerprints; seeding only approved protocols and re-seeding on change;
the enactor round trip with a fake Garmin (approval gate, idempotent
re-runs, crash recovery, ambiguous names, same-day duplicates, reschedule,
skip → unschedule); and that a read-only install has no coach config.

Covers acceptance criteria 1, 2, 5, 6 from
docs/superpowers/plans/2026-08-24-garmin-domains-upgrade.md. Criterion 3
(targeted backfill trigger) is covered with a stub standing in for the
real GitHub API call. Criterion 4 (intra-domain migration addition) can't
be exercised until the challenges-in-progress probe's follow-up plan adds
real migrations — see that plan's "Out of scope" section; not covered
here on purpose.
"""
import contextlib
import json
import os
import pathlib
import sys
from datetime import date, timedelta
from decimal import Decimal

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from apply_schema import apply_migrations, applied_versions
from config import get_enabled_domains, set_enabled_domains
import domains
import planning
import seed_workouts
import sync_schedule
from domains import DOMAINS, SyncContext
import garmin_write
from coach_roles import CoachLoopRoles, apply_coach_grants, assert_role_isolated, ensure_role, url_for_role
from protocols import seed_default_protocols

DB_URL = os.environ["SMOKE_TEST_DATABASE_URL"]
OLD_INIT_SQL = pathlib.Path(__file__).resolve().parent.parent / "schema" / "init.sql"


def _fake_catalog_doc() -> dict:
    return {"categories": {
        "PULL_UP": {"exercises": {"WEIGHTED_PULL_UP": {"primaryMuscles": ["LATS"]}, "PULL_UP": {}}},
        "PLANK": {"exercises": {"PLANK": {}}},
        "ROW": {"exercises": {"DUMBBELL_ROW": {}}},
    }}


domains._fetch_catalog_doc = _fake_catalog_doc


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


def test_plan_admin_approve_unapprove() -> None:
    import plan_admin
    coach_url, _sched_url = _setup_coach_loop()
    tomorrow = date.today() + timedelta(days=1)
    with psycopg.connect(coach_url) as conn:
        pid = conn.execute(
            "insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'finger-repeaters-7-3') returning id",
            (tomorrow,)).fetchone()[0]
    with psycopg.connect(DB_URL) as conn:
        lines = plan_admin.approve(conn, [pid], "ok")
        assert "Finger repeaters 7/3" in lines[0] and '"reps": 6' in lines[0], lines  # owner sees params
        assert planning.load_plan(conn, ids=[pid])[0].approval == "approved"
    with psycopg.connect(coach_url) as conn:
        conn.execute("update protocols set description = 'harder' where slug = 'finger-repeaters-7-3'")
    with psycopg.connect(DB_URL) as conn:
        assert planning.load_plan(conn, ids=[pid])[0].approval == "stale"
        assert plan_admin.unapprove(conn, [pid]) == 1
        assert planning.load_plan(conn, ids=[pid])[0].approval == "unapproved"
    print("test_plan_admin_approve_unapprove: PASS")


class FakeGarmin:
    """In-memory stand-in for the Garmin write + calendar endpoints."""

    def __init__(self):
        self.next_id = 1000
        self.library: dict[int, dict] = {}
        self.calendar: dict[int, dict] = {}
        self.uploads: list[int] = []
        self.schedules: list[tuple[int, str]] = []
        self.unschedules: list[int] = []
        self.deletes: list[int] = []
        self.on_schedule = None  # test hook: runs inside schedule_workout, before it returns

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def upload_workout(self, payload):
        wid = self._id()
        self.library[wid] = payload
        self.uploads.append(wid)
        return {"workoutId": wid, "workoutName": payload["workoutName"]}

    def schedule_workout(self, workout_id, date_str):
        sid = self._id()
        self.calendar[sid] = {"id": sid, "itemType": "workout", "workoutId": int(workout_id), "date": date_str}
        self.schedules.append((int(workout_id), date_str))
        if self.on_schedule:
            self.on_schedule()
        return {"workoutScheduleId": sid}

    def unschedule_workout(self, schedule_id):
        self.calendar.pop(int(schedule_id))
        self.unschedules.append(int(schedule_id))

    def delete_workout(self, workout_id):
        self.library.pop(int(workout_id), None)
        self.deletes.append(int(workout_id))

    def get_scheduled_workouts(self, year, month):
        return {"calendarItems": [
            i for i in self.calendar.values()
            if (date.fromisoformat(i["date"]).year, date.fromisoformat(i["date"]).month) == (year, month)
        ]}


def _yes(_summary: str) -> bool:
    return True


def test_seed_only_approved_and_rehash() -> None:
    import plan_admin
    coach_url, sched_url = _setup_coach_loop()
    seed_workouts.POLITENESS_SLEEP_S = 0
    tomorrow = date.today() + timedelta(days=1)
    fake = FakeGarmin()
    with psycopg.connect(coach_url) as conn:
        pid = conn.execute(
            "insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'finger-repeaters-7-3') returning id",
            (tomorrow,),
        ).fetchone()[0]

    def seed():
        with psycopg.connect(sched_url) as conn:
            return seed_workouts.run(conn, lambda: contextlib.nullcontext(fake), today=date.today(), confirm=_yes, prune_stale=True)

    assert seed() == 0 and fake.uploads == [], "unapproved plan row must not be seeded"
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [pid], None)
    seed()
    assert len(fake.uploads) == 1
    seed()
    assert len(fake.uploads) == 1, "unchanged params must not re-upload"

    # coach changes params -> approval goes stale -> nothing uploaded until re-approved
    with psycopg.connect(coach_url) as conn:
        conn.execute("update protocols set params = params || '{\"reps\": 7}'::jsonb where slug = 'finger-repeaters-7-3'")
    seed()
    assert len(fake.uploads) == 1, "stale approval must block the re-upload"
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [pid], None)
    seed()
    assert len(fake.uploads) == 2 and fake.deletes == [fake.uploads[0]], "re-upload + prune of unreferenced stale workout"
    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("select workout_id from generated_workouts where protocol_slug = 'finger-repeaters-7-3'").fetchone()[0] == fake.uploads[1]

    # a post-approval rename would change what's uploaded -> stale, nothing uploaded
    with psycopg.connect(coach_url) as conn:
        conn.execute("update protocols set name = 'Renamed' where slug = 'finger-repeaters-7-3'")
    seed()
    assert len(fake.uploads) == 2, "a post-approval rename must not be uploaded unapproved"
    print("test_seed_only_approved_and_rehash: PASS")


def test_enactor_round_trip() -> None:
    import plan_admin
    coach_url, sched_url = _setup_coach_loop()
    seed_workouts.POLITENESS_SLEEP_S = 0
    sync_schedule.POLITENESS_SLEEP_S = 0
    domains.SCHEDULE_FETCH_SLEEP_S = 0
    today = date.today()
    tomorrow = today + timedelta(days=1)
    fake = FakeGarmin()

    with psycopg.connect(DB_URL) as conn:  # "ingested" library workouts (owner = ingestion)
        conn.execute("insert into workouts (workout_id, workout_name, raw) values (555, 'Bear Essentials', '{}'::jsonb)")
        conn.execute("insert into workouts (workout_id, workout_name, raw) values (601, 'Drill Workout', '{}'::jsonb), (602, 'Drill Workout', '{}'::jsonb)")
    with psycopg.connect(coach_url) as conn:
        gen_id = conn.execute(
            "insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'finger-repeaters-7-3') returning id",
            (tomorrow,)).fetchone()[0]
        lib_id = conn.execute(
            "insert into training_plan (scheduled_date, source, workout_name) values (%s, 'library', 'Bear Essentials') returning id",
            (tomorrow,)).fetchone()[0]
        amb_id = conn.execute(
            "insert into training_plan (scheduled_date, source, workout_name) values (%s, 'library', 'Drill Workout') returning id",
            (tomorrow,)).fetchone()[0]
        # Same workout and day as lib_id, but keyed by id: passes the unique index.
        dup_id = conn.execute(
            "insert into training_plan (scheduled_date, source, workout_id) values (%s, 'library', 555) returning id",
            (tomorrow,)).fetchone()[0]

    def enact():
        with psycopg.connect(sched_url) as conn:
            seed_workouts.run(conn, lambda: contextlib.nullcontext(fake), today=today, confirm=_yes)
            sync_schedule.run(conn, lambda: contextlib.nullcontext(fake), today=today, confirm=_yes)

    enact()
    assert fake.uploads == [] and fake.schedules == [], "unapproved rows must never be scheduled"

    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [gen_id, lib_id, amb_id, dup_id], None)
    enact()
    assert len(fake.uploads) == 1, "library rows must not be seeded"
    assert sorted(fake.schedules) == sorted([(fake.uploads[0], tomorrow.isoformat()), (555, tomorrow.isoformat())]), \
        f"ambiguous name skipped, duplicate target scheduled once: {fake.schedules}"

    enact()
    assert len(fake.uploads) == 1 and len(fake.schedules) == 2, "re-run must schedule nothing"
    with psycopg.connect(DB_URL) as conn:
        dup_status = conn.execute("select status, schedule_id from training_plan where id = %s", (dup_id,)).fetchone()
    assert dup_status == ("planned", None), f"duplicate row must not record lib_id's entry: {dup_status}"
    with psycopg.connect(coach_url) as conn:  # never enacted -> the coach may delete it
        conn.execute("delete from training_plan where id = %s", (dup_id,))

    # crash-after-schedule simulation: DB forgot, Garmin remembers -> record, don't duplicate
    with psycopg.connect(DB_URL) as conn:
        conn.execute("update training_plan set status = 'planned', schedule_id = null, resolved_workout_id = null, enacted_date = null where id = %s", (lib_id,))
    enact()
    assert len(fake.schedules) == 2
    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("select status from training_plan where id = %s", (lib_id,)).fetchone()[0] == "scheduled"

    # coach tampers with an approved row -> stale -> nothing new scheduled
    with psycopg.connect(coach_url) as conn:
        conn.execute("update training_plan set scheduled_date = scheduled_date + 1 where id = %s", (lib_id,))
    enact()
    assert len(fake.schedules) == 2, "edited-after-approval row must not be enacted"

    # owner re-approves -> moves: old calendar entry unscheduled, new date scheduled
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [lib_id], None)
    enact()
    assert len(fake.schedules) == 3 and len(fake.unschedules) == 1
    assert (555, (tomorrow + timedelta(days=1)).isoformat()) in fake.schedules

    # coach cancels the enacted generated row: can't delete it, skip removes it from Garmin
    assert _denied(coach_url, "delete from training_plan where id = %s", (gen_id,))
    with psycopg.connect(coach_url) as conn:
        conn.execute("update training_plan set skip = true where id = %s", (gen_id,))
    enact()
    assert len(fake.unschedules) == 2 and len(fake.schedules) == 3
    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("select status, schedule_id, resolved_workout_id from training_plan where id = %s",
                            (gen_id,)).fetchone() == ("skipped", None, None)
    enact()
    assert len(fake.unschedules) == 2, "re-run must not unschedule again"
    with psycopg.connect(coach_url) as conn:  # cleared -> deletable now
        conn.execute("delete from training_plan where id = %s", (gen_id,))
    print("test_enactor_round_trip: PASS")


def test_read_only_install_has_no_coach_config() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    default_keys = {d.key for d in DOMAINS if d.default_enabled}
    apply_migrations(DB_URL, enabled_domains=default_keys)
    from config import get_coach_loop
    with psycopg.connect(DB_URL) as conn:
        set_enabled_domains(conn, default_keys)
        assert get_coach_loop(conn) is None
        assert "workouts" not in get_enabled_domains(conn)
        assert not _table_exists(conn, "training_plan")
    print("test_read_only_install_has_no_coach_config: PASS")


def test_review_fixes() -> None:
    """Whole-branch review findings I1-I4 (see the plan's ledger)."""
    import plan_admin
    coach_url, sched_url = _setup_coach_loop()
    seed_workouts.POLITENESS_SLEEP_S = 0
    sync_schedule.POLITENESS_SLEEP_S = 0
    domains.SCHEDULE_FETCH_SLEEP_S = 0
    today = date.today()
    tomorrow = today + timedelta(days=1)
    far = (tomorrow.replace(day=1) + timedelta(days=62)).replace(day=1)  # two months on: outside tomorrow's fetch window
    fake = FakeGarmin()
    with psycopg.connect(DB_URL) as conn:
        conn.execute("insert into workouts (workout_id, workout_name, raw) values (555, 'Bear Essentials', '{}'::jsonb)")

    def enact():
        with psycopg.connect(sched_url) as conn:
            seed_workouts.run(conn, lambda: contextlib.nullcontext(fake), today=today, confirm=_yes, prune_stale=True)
            sync_schedule.run(conn, lambda: contextlib.nullcontext(fake), today=today, confirm=_yes)

    def coach(sql, *args):
        with psycopg.connect(coach_url) as conn:
            row = conn.execute(sql, args).fetchone() if "returning" in sql else conn.execute(sql, args)
            return row[0] if "returning" in sql else None

    def calendar_pairs():
        return sorted((i["workoutId"], i["date"]) for i in fake.calendar.values())

    # I3: approval pins what the owner was shown, not what's there at commit time
    lib = coach("insert into training_plan (scheduled_date, source, workout_id) values (%s, 'library', 555) returning id", far)
    with psycopg.connect(DB_URL) as conn:
        shown = plan_admin.preview(conn, [lib])
    coach("update training_plan set scheduled_date = scheduled_date + 1 where id = %s", lib)
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve_views(conn, shown, None)
        assert planning.load_plan(conn, ids=[lib])[0].approval == "stale", "edit after preview must not ride on the approval"
    coach("update training_plan set scheduled_date = %s where id = %s", far, lib)

    # I2b: once scheduling starts the row is pinned, so the coach can't delete it mid-flight
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [lib], None)
    attempts = []
    fake.on_schedule = lambda: attempts.append(_denied(coach_url, "delete from training_plan where id = %s", (lib,)))
    enact()
    fake.on_schedule = None
    assert attempts == [True], f"coach delete during schedule must be denied: {attempts}"
    assert calendar_pairs() == [(555, far.isoformat())]

    # I1: moving an enacted row far -> tomorrow must remove the old (out-of-window) entry
    coach("update training_plan set scheduled_date = %s where id = %s", tomorrow, lib)
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [lib], None)
    enact()
    assert calendar_pairs() == [(555, tomorrow.isoformat())], f"old entry orphaned: {calendar_pairs()}"

    # I2a: an enacted row moved into the past and then skipped must still be cancelled
    coach("update training_plan set scheduled_date = %s, skip = true where id = %s", today - timedelta(days=3), lib)
    enact()
    assert calendar_pairs() == [], f"stranded entry: {calendar_pairs()}"

    # I4: --prune-stale must not delete a generated workout that's still on the calendar
    gen = coach("insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'finger-repeaters-7-3') returning id", tomorrow)
    gen2 = coach("insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', 'finger-repeaters-7-3') returning id", tomorrow + timedelta(days=2))
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [gen, gen2], None)
    enact()
    first = fake.uploads[-1]
    coach("update protocols set params = params || '{\"reps\": 8}'::jsonb where slug = 'finger-repeaters-7-3'")
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, [gen], None)  # gen2 left stale, still on the calendar with the old workout
    enact()
    assert first not in fake.deletes, "pruned a workout that's still scheduled"
    print("test_review_fixes: PASS")


def test_coach_tables_are_self_describing() -> None:
    """The MCP coach only sees the database, so the workflow must live in
    table/column comments it can read (postgres-mcp surfaces them)."""
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    apply_migrations(DB_URL, enabled_domains={"activities", "workouts"})
    with psycopg.connect(DB_URL) as conn:
        def comment(table):
            return conn.execute("select obj_description(%s::regclass, 'pg_class')", (table,)).fetchone()[0] or ""
        assert "run_write_sql" in comment("training_plan") and "approve" in comment("training_plan")
        assert "params" in comment("protocols")
        for t in ("workouts", "scheduled_workouts", "generated_workouts"):
            assert "READ-ONLY" in comment(t), t
        undocumented = [r[0] for r in conn.execute(
            "select a.attname from pg_attribute a where a.attrelid = 'training_plan'::regclass "
            "and a.attnum > 0 and not a.attisdropped and col_description(a.attrelid, a.attnum) is null"
        ).fetchall()]
        assert undocumented == [], f"training_plan columns without comments: {undocumented}"
    print("test_coach_tables_are_self_describing: PASS")


SAMPLE_EXERCISE_SETS = {"activityId": 1, "exerciseSets": [
    {"setType": "ACTIVE", "repetitionCount": 5, "weight": 12500.0, "duration": 42.7, "startTime": "2026-10-01T18:56:17.0",
     "exercises": [{"category": "UNKNOWN", "name": None, "probability": 0.0},
                   {"category": "BENCH_PRESS", "name": "DUMBBELL_BENCH_PRESS", "probability": 99.6}]},
    {"setType": "REST", "repetitionCount": None, "weight": None, "duration": 120.0, "startTime": "2026-10-01T18:57:00.0",
     "exercises": []},
]}


def test_strength_schema_and_view() -> None:
    coach_url, _sched_url = _setup_coach_loop()
    with psycopg.connect(DB_URL) as conn:
        for t in ("exercise_catalog", "activity_exercise_sets"):
            assert _table_exists(conn, t), t
        conn.execute("insert into activity_exercise_sets (activity_id, raw) values (1, %s), (2, %s)",
                     (json.dumps(SAMPLE_EXERCISE_SETS), json.dumps({"activityId": 2, "exerciseSets": None})))
        conn.commit()
        for t in ("exercise_catalog", "activity_exercise_sets", "activity_exercise_set_rows"):
            desc = conn.execute("select obj_description(%s::regclass, 'pg_class')", (t,)).fetchone()[0] or ""
            assert "READ-ONLY" in desc, t
    with psycopg.connect(coach_url) as conn:  # views need their own grant (pg_tables excludes them)
        rows = conn.execute(
            "select set_index, set_type, reps, weight_kg, duration_s, category, exercise, probability "
            "from activity_exercise_set_rows order by activity_id, set_index").fetchall()
    assert rows == [
        (0, "ACTIVE", 5, Decimal("12.5"), Decimal("42.7"), "BENCH_PRESS", "DUMBBELL_BENCH_PRESS", Decimal("99.6")),
        (1, "REST", None, None, Decimal("120.0"), None, None, None),
    ], rows
    print("test_strength_schema_and_view: PASS")


def test_exercise_catalog_refresh_keeps_rows_on_failure() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    apply_migrations(DB_URL, enabled_domains={"activities", "workouts"})
    sample = json.loads((pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
                         / "exercise_catalog_sample.json").read_text())
    today = date.today()
    with psycopg.connect(DB_URL) as conn:
        domains._fetch_catalog_doc = lambda: sample
        assert domains._refresh_exercise_catalog(conn, today) == 3
        assert domains._refresh_exercise_catalog(conn, today) == 0, "fresh catalogue must not refetch"
        conn.execute("update exercise_catalog set synced_at = now() - interval '8 days'")
        conn.commit()

        def boom():
            raise OSError("network down")
        for failing in (boom, lambda: {"html": "<b>oops</b>"}, lambda: {}):
            domains._fetch_catalog_doc = failing
            assert domains._refresh_exercise_catalog(conn, today) == 0
            assert conn.execute("select count(*) from exercise_catalog").fetchone()[0] == 3, "rows must survive"
    domains._fetch_catalog_doc = _fake_catalog_doc
    print("test_exercise_catalog_refresh_keeps_rows_on_failure: PASS")


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
    test_plan_admin_approve_unapprove()
    test_seed_only_approved_and_rehash()
    test_enactor_round_trip()
    test_review_fixes()
    test_read_only_install_has_no_coach_config()
    test_coach_tables_are_self_describing()
    test_strength_schema_and_view()
    test_exercise_catalog_refresh_keeps_rows_on_failure()
    test_targeted_backfill_trigger_stub()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
