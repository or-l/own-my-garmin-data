"""
Single source of truth for which Garmin data domains this project knows
about. Imported by ingestion/pull.py on the bare GitHub Actions runner
(see ingestion/requirements.txt: garminconnect, psycopg[binary],
python-dotenv only) and by backfill.py (own uv inline deps: garminconnect,
psycopg[binary]) — so this module and everything it imports at module
scope must stay within that dependency set. No rich, no questionary, no
requests, no workflow_tools.

Each Domain declares:
  - key: machine name, used in sync_config.enabled_domains and the
    `-- domain:` tag in schema/migrations/*.sql.
  - category: checklist grouping header (install.py).
  - label / description: human text for the checklist.
  - default_enabled: pre-checked in the checklist.
  - sync_incremental(ctx: SyncContext) -> dict[str, int]: pulls this
    domain's data for the incremental lookback window and upserts it.
    Returns counts keyed by table name, e.g. {"activities": 12} — callers
    sum these across domains that share a table (wellness and
    body_composition both touch daily_metrics).
  - backfill(ctx: BackfillContext) -> None, or None for snapshot-only
    domains (challenges, profile) — the next incremental sync populates
    those; there's no history to backfill.
  - tables: table names this domain owns, for docs / the upgrade "what's
    new" summary. Not used to run schema (schema/migrations/*.sql is the
    source of truth for that).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import psycopg
from garminconnect import Garmin, GarminConnectConnectionError

import upserts
from bootstrap.garmin_auth import save_token_to_db

ACTIVITY_HISTORY_START = "2000-01-01"

# Chunk size for range endpoints during backfill — see backfill_range_endpoint.
RANGE_CHUNK_DAYS = 90
BODY_BATTERY_CHUNK_DAYS = 28


@dataclass
class SyncContext:
    client: Garmin
    conn: psycopg.Connection
    window_start: date
    today: date


@dataclass
class BackfillContext:
    client: Garmin
    conn: psycopg.Connection
    start_date: date
    today: date


@dataclass(frozen=True)
class Domain:
    key: str
    category: str
    label: str
    description: str
    default_enabled: bool
    sync_incremental: Callable[[SyncContext], dict[str, int]]
    backfill: Callable[[BackfillContext], None] | None
    tables: tuple[str, ...]


def backfill_range_endpoint(
    client, start: date, today: date, label: str, fetch_and_upsert, chunk_days: int = RANGE_CHUNK_DAYS
) -> None:
    # chunk_days is a guess at each backend's undocumented server-side max
    # range. Rather than guess a new constant when it's wrong, shrink on
    # the actual "requested date range is too big" 400 and keep the
    # smaller size for subsequent chunks.
    print(f"Backfilling {label} from {start} to {today}...")
    chunk_start = start
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), today)
        while True:
            try:
                fetch_and_upsert(client, chunk_start.isoformat(), chunk_end.isoformat())
                break
            except GarminConnectConnectionError as e:
                if "too big" not in str(e).lower() or chunk_end == chunk_start:
                    raise
                chunk_days = max(1, (chunk_end - chunk_start).days // 2)
                chunk_end = chunk_start + timedelta(days=chunk_days - 1)
                print(f"  ...{label} range too big, shrinking chunk to {chunk_days} days and retrying")
                time.sleep(1)
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(1)
    print(f"  -> {label} done.")


# ---------------------------------------------------------------- activities

def _activities_sync(ctx: SyncContext) -> dict[str, int]:
    activities = ctx.client.get_activities_by_date(
        ctx.window_start.isoformat(), ctx.today.isoformat(), sortorder="asc"
    )
    upserts.upsert_activities(ctx.conn, activities)
    return {"activities": len(activities)}


def _activities_backfill(ctx: BackfillContext) -> None:
    print(f"Backfilling all activities since {ACTIVITY_HISTORY_START}...")
    activities = ctx.client.get_activities_by_date(ACTIVITY_HISTORY_START, ctx.today.isoformat(), sortorder="asc")
    upserts.upsert_activities(ctx.conn, activities)
    print(f"  -> {len(activities)} activities upserted.")


# ------------------------------------------------------------------ wellness

def _wellness_fields(client: Garmin, iso: str) -> dict:
    return {
        "stats": client.get_stats(iso),
        "sleep": client.get_sleep_data(iso),
        "stress": client.get_all_day_stress(iso),
        "hrv": client.get_hrv_data(iso),
        "max_metrics": client.get_max_metrics(iso),
        "respiration": client.get_respiration_data(iso),
        "spo2": client.get_spo2_data(iso),
        "body_battery_events": client.get_body_battery_events(iso),
        "intensity_minutes": client.get_intensity_minutes_data(iso),
        "floors": client.get_floors(iso),
        "steps_intraday": client.get_steps_data(iso),
        "heart_rates": client.get_heart_rates(iso),
        "day_events": client.get_all_day_events(iso),
    }


def _wellness_sync(ctx: SyncContext) -> dict[str, int]:
    body_battery_days = ctx.client.get_body_battery(ctx.window_start.isoformat(), ctx.today.isoformat())
    upserts.upsert_body_battery(ctx.conn, body_battery_days)

    lookback = (ctx.today - ctx.window_start).days
    days_pulled = 0
    for offset in range(lookback + 1):
        d = ctx.window_start + timedelta(days=offset)
        upserts.upsert_daily_metrics(ctx.conn, d, _wellness_fields(ctx.client, d.isoformat()))
        days_pulled += 1
        time.sleep(1)
    return {"daily_metrics": days_pulled}


def _wellness_already_backfilled(conn: psycopg.Connection, d: date) -> bool:
    row = conn.execute(
        "select stats is not null and respiration is not null from daily_metrics where metric_date = %s",
        (d,),
    ).fetchone()
    return bool(row and row[0])


def _wellness_backfill(ctx: BackfillContext) -> None:
    backfill_range_endpoint(
        ctx.client, ctx.start_date, ctx.today, "body battery",
        lambda c, s, e: upserts.upsert_body_battery(ctx.conn, c.get_body_battery(s, e)),
        chunk_days=BODY_BATTERY_CHUNK_DAYS,
    )

    total_days = (ctx.today - ctx.start_date).days + 1
    done, skipped = 0, 0
    for offset in range(total_days):
        d = ctx.start_date + timedelta(days=offset)
        if _wellness_already_backfilled(ctx.conn, d):
            skipped += 1
            continue
        upserts.upsert_daily_metrics(ctx.conn, d, _wellness_fields(ctx.client, d.isoformat()))
        done += 1
        if done % 20 == 0:
            print(f"  ...wellness: {done} days pulled, {skipped} already present, at {d.isoformat()}")
            save_token_to_db(ctx.conn)
        time.sleep(1)
    print(f"Wellness backfill done. {done} days pulled, {skipped} already present.")


# ---------------------------------------------------------- body_composition

def _body_composition_sync(ctx: SyncContext) -> dict[str, int]:
    weigh_ins = ctx.client.get_weigh_ins(ctx.window_start.isoformat(), ctx.today.isoformat())
    upserts.upsert_weigh_ins(ctx.conn, weigh_ins)
    return {"daily_metrics": len(weigh_ins.get("dailyWeightSummaries", []))}


def _body_composition_backfill(ctx: BackfillContext) -> None:
    backfill_range_endpoint(
        ctx.client, ctx.start_date, ctx.today, "weigh-ins",
        lambda c, s, e: upserts.upsert_weigh_ins(ctx.conn, c.get_weigh_ins(s, e)),
    )


# ------------------------------------------------------------------ training

def _upsert_performance_snapshots(conn: psycopg.Connection, snapshots: dict) -> None:
    for metric_name, raw in snapshots.items():
        conn.execute(
            """
            insert into performance_snapshots (metric_name, raw, updated_at)
            values (%s, %s, now())
                on conflict (metric_name) do update set raw = excluded.raw, updated_at = now()
            """,
            (metric_name, json.dumps(raw)),
        )
    conn.commit()


def _upsert_personal_records(conn: psycopg.Connection, records: list[dict]) -> None:
    for r in records:
        conn.execute(
            """
            insert into personal_records (record_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (record_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (r["id"], json.dumps(r)),
        )
    conn.commit()


def _training_fields(client: Garmin, iso: str) -> dict:
    return {
        "training_status": client.get_training_status(iso),
        "training_readiness": client.get_training_readiness(iso),
        "morning_training_readiness": client.get_morning_training_readiness(iso),
        "endurance_score": client.get_endurance_score(iso),
        "hill_score": client.get_hill_score(iso),
        "fitness_age": client.get_fitnessage_data(iso),
        "running_tolerance": client.get_running_tolerance(iso, iso, aggregation="daily"),
    }


def _training_sync(ctx: SyncContext) -> dict[str, int]:
    snapshots = {
        "race_predictions": ctx.client.get_race_predictions(),
        "cycling_ftp": ctx.client.get_cycling_ftp(),
        "lactate_threshold": ctx.client.get_lactate_threshold(),
    }
    _upsert_performance_snapshots(ctx.conn, snapshots)

    personal_records = ctx.client.get_personal_record()
    _upsert_personal_records(ctx.conn, personal_records)

    lookback = (ctx.today - ctx.window_start).days
    days_pulled = 0
    for offset in range(lookback + 1):
        d = ctx.window_start + timedelta(days=offset)
        upserts.upsert_training_insight(ctx.conn, d, _training_fields(ctx.client, d.isoformat()))
        days_pulled += 1
        time.sleep(1)
    return {
        "training_insight": days_pulled,
        "performance_snapshots": len(snapshots),
        "personal_records": len(personal_records),
    }


def _training_already_backfilled(conn: psycopg.Connection, d: date) -> bool:
    row = conn.execute(
        "select metric_date is not null from training_insight where metric_date = %s", (d,)
    ).fetchone()
    return bool(row and row[0])


def _training_backfill(ctx: BackfillContext) -> None:
    total_days = (ctx.today - ctx.start_date).days + 1
    done, skipped = 0, 0
    for offset in range(total_days):
        d = ctx.start_date + timedelta(days=offset)
        if _training_already_backfilled(ctx.conn, d):
            skipped += 1
            continue
        upserts.upsert_training_insight(ctx.conn, d, _training_fields(ctx.client, d.isoformat()))
        done += 1
        if done % 20 == 0:
            print(f"  ...training: {done} days pulled, {skipped} already present, at {d.isoformat()}")
            save_token_to_db(ctx.conn)
        time.sleep(1)
    print(f"Training backfill done. {done} days pulled, {skipped} already present.")


# ----------------------------------------------------------------- challenges

def _upsert_challenges(conn: psycopg.Connection, challenges: list[dict]) -> None:
    for c in challenges:
        conn.execute(
            """
            insert into challenges (challenge_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (challenge_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (c["uuid"], json.dumps(c)),
        )
    conn.commit()


def _upsert_earned_badges(conn: psycopg.Connection, badges: list[dict]) -> None:
    for b in badges:
        conn.execute(
            """
            insert into earned_badges (badge_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (badge_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (b["badgeId"], json.dumps(b)),
        )
    conn.commit()


def _upsert_available_badges(conn: psycopg.Connection, badges: list[dict]) -> None:
    for b in badges:
        conn.execute(
            """
            insert into available_badges (badge_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (badge_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (b["badgeId"], json.dumps(b)),
        )
    conn.commit()


def _challenges_sync(ctx: SyncContext) -> dict[str, int]:
    challenges = ctx.client.get_available_badge_challenges(1, 100)
    _upsert_challenges(ctx.conn, challenges)

    earned_badges = ctx.client.get_earned_badges()
    _upsert_earned_badges(ctx.conn, earned_badges)

    available_badges = ctx.client.get_available_badges()
    _upsert_available_badges(ctx.conn, available_badges)

    return {"challenges": len(challenges), "earned_badges": len(earned_badges), "available_badges": len(available_badges)}


# -------------------------------------------------------------------- profile

def _upsert_user_profile(conn: psycopg.Connection, profile: dict, settings: dict) -> None:
    conn.execute(
        """
        insert into user_profile (id, profile, settings, updated_at)
        values (1, %s, %s, now())
            on conflict (id) do update set
            profile = excluded.profile, settings = excluded.settings, updated_at = now()
        """,
        (json.dumps(profile), json.dumps(settings)),
    )
    conn.commit()


def _replace_goals(conn: psycopg.Connection, status: str, goals: list[dict]) -> None:
    conn.execute("delete from goals where status = %s", (status,))
    for g in goals:
        conn.execute(
            "insert into goals (status, raw, updated_at) values (%s, %s, now())",
            (status, json.dumps(g)),
        )
    conn.commit()


def _profile_sync(ctx: SyncContext) -> dict[str, int]:
    profile = ctx.client.get_user_profile()
    settings = ctx.client.get_userprofile_settings()
    _upsert_user_profile(ctx.conn, profile, settings)

    goals_total = 0
    for status in ("active", "future", "past"):
        goals = ctx.client.get_goals(status=status)
        _replace_goals(ctx.conn, status, goals)
        goals_total += len(goals)
        time.sleep(1)

    return {"user_profile": 1, "goals": goals_total}


# ------------------------------------------------------------------ workouts
# Read-only. The write side (upload/schedule) lives in garmin_write.py and
# must never be imported from here — tests/test_write_isolation.py enforces it.

SCHEDULE_LOOKAHEAD_DAYS = 62
SCHEDULE_FETCH_SLEEP_S = 1.0
WORKOUT_PAGE_SIZE = 100


def parse_scheduled(calendar: dict | None) -> list[dict]:
    """Workout items from a get_scheduled_workouts() calendar payload. The
    payload also carries weigh-ins, activities, naps, etc., and spans the
    whole calendar grid (neighbouring-month days included)."""
    return [
        i for i in (calendar or {}).get("calendarItems", [])
        if i.get("itemType") == "workout" and i.get("workoutId")
    ]


def month_window(start: date, end: date) -> tuple[date, date]:
    first = start.replace(day=1)
    last = (end.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first, last


def _fetch_scheduled(client, start: date, end: date) -> dict[int, dict]:
    """Calendar item id -> workout item, for every month from start's to
    end's. get_scheduled_workouts takes a 1-based month (the library
    converts). Adjacent months overlap in the grid, hence the id dedupe."""
    items: dict[int, dict] = {}
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        for item in parse_scheduled(client.get_scheduled_workouts(year, month)):
            items[int(item["id"])] = item
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        time.sleep(SCHEDULE_FETCH_SLEEP_S)
    return items


def _fetch_workout_list(client) -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        page = client.get_workouts(start, WORKOUT_PAGE_SIZE) or []
        out.extend(page)
        if len(page) < WORKOUT_PAGE_SIZE:
            return out
        start += WORKOUT_PAGE_SIZE
        time.sleep(1)


def _upsert_workout(conn: psycopg.Connection, workout: dict, change_marker: str | None) -> None:
    conn.execute(
        """
        insert into workouts (workout_id, workout_name, updated_date, raw, synced_at)
        values (%s, %s, %s, %s, now())
            on conflict (workout_id) do update set
            workout_name = excluded.workout_name, updated_date = excluded.updated_date,
            raw = excluded.raw, synced_at = now()
        """,
        (workout["workoutId"], workout.get("workoutName"), change_marker, json.dumps(workout)),
    )


def _replace_scheduled(conn: psycopg.Connection, items: dict[int, dict], window: tuple[date, date]) -> None:
    # Rows in the fetched window that Garmin no longer returns were
    # unscheduled (in Connect, or by the enactor) — drop them.
    conn.execute(
        "delete from scheduled_workouts where scheduled_date between %s and %s and not (schedule_id = any(%s::bigint[]))",
        (window[0], window[1], list(items)),
    )
    for schedule_id, item in items.items():
        conn.execute(
            """
            insert into scheduled_workouts (schedule_id, workout_id, scheduled_date, raw, synced_at)
            values (%s, %s, %s, %s, now())
                on conflict (schedule_id) do update set
                workout_id = excluded.workout_id, scheduled_date = excluded.scheduled_date,
                raw = excluded.raw, synced_at = now()
            """,
            (schedule_id, item["workoutId"], item["date"], json.dumps(item)),
        )


def _workouts_sync(ctx: SyncContext) -> dict[str, int]:
    listing = _fetch_workout_list(ctx.client)
    stored = dict(ctx.conn.execute("select workout_id, updated_date from workouts").fetchall())
    for summary in listing:
        workout_id = int(summary["workoutId"])
        # The list calls its change marker `updateDate`; the detail payload
        # calls it `updatedDate`. Compare and store the list's.
        marker = summary.get("updateDate")
        if workout_id in stored and stored[workout_id] == marker:
            continue
        _upsert_workout(ctx.conn, ctx.client.get_workout_by_id(workout_id), marker)
        time.sleep(1)
    # An empty listing is far more likely an API hiccup than a user who
    # deleted every workout — never wipe the table on it.
    if listing:
        ctx.conn.execute(
            "delete from workouts where not (workout_id = any(%s::bigint[]))",
            ([int(w["workoutId"]) for w in listing],),
        )
    ctx.conn.commit()

    end = ctx.today + timedelta(days=SCHEDULE_LOOKAHEAD_DAYS)
    scheduled = _fetch_scheduled(ctx.client, ctx.today, end)
    _replace_scheduled(ctx.conn, scheduled, month_window(ctx.today, end))
    ctx.conn.commit()
    return {"workouts": len(listing), "scheduled_workouts": len(scheduled)}


# --------------------------------------------------------------------- registry

DOMAINS: list[Domain] = [
    Domain(
        key="activities", category="Activities", label="Activities",
        description="Every recorded activity (runs, rides, etc.) via get_activities_by_date.",
        default_enabled=True, sync_incremental=_activities_sync, backfill=_activities_backfill,
        tables=("activities",),
    ),
    Domain(
        key="wellness", category="Daily wellness", label="Daily wellness",
        description="Stats, sleep, stress, HRV, respiration, SpO2, body battery, intensity minutes, floors, steps, heart rate, day events.",
        default_enabled=True, sync_incremental=_wellness_sync, backfill=_wellness_backfill,
        tables=("daily_metrics",),
    ),
    Domain(
        key="body_composition", category="Daily wellness", label="Body composition (weigh-ins)",
        description="Smart-scale weigh-ins via get_weigh_ins.",
        default_enabled=True, sync_incremental=_body_composition_sync, backfill=_body_composition_backfill,
        tables=("daily_metrics",),
    ),
    Domain(
        key="training", category="Training & performance", label="Training & performance",
        description="Training status/readiness, endurance/hill score, fitness age, running tolerance, race predictions, cycling FTP, lactate threshold, personal records.",
        default_enabled=True, sync_incremental=_training_sync, backfill=_training_backfill,
        tables=("training_insight", "performance_snapshots", "personal_records"),
    ),
    Domain(
        key="challenges", category="Challenges & badges", label="Challenges & badges",
        description="Available badge challenges, earned badges, available badges.",
        default_enabled=True, sync_incremental=_challenges_sync, backfill=None,
        tables=("challenges", "earned_badges", "available_badges"),
    ),
    Domain(
        key="profile", category="Profile & records", label="Profile & goals",
        description="Account profile/settings and goals (active/future/past).",
        default_enabled=True, sync_incremental=_profile_sync, backfill=None,
        tables=("user_profile", "goals"),
    ),
    Domain(
        key="workouts", category="Workouts", label="Workout library & calendar",
        description="Your workout library (fully expanded steps + description) and workouts scheduled on your Garmin calendar. Read-only.",
        default_enabled=False, sync_incremental=_workouts_sync, backfill=None,
        tables=("workouts", "scheduled_workouts"),
    ),
    # "reproductive" (menstrual calendar, pregnancy summary) is deliberately
    # not registered here yet — its endpoints haven't been verified live.
    # See docs/superpowers/plans/2026-08-24-garmin-domains-upgrade.md,
    # "Out of scope for this plan".
]

DOMAINS_BY_KEY: dict[str, Domain] = {d.key: d for d in DOMAINS}
