-- domain: workouts
-- The Garmin workout library: every workout template on the account (hand-built,
-- catalog, or uploaded by seed_workouts.py), fully expanded via get_workout_by_id
-- so steps + description are stored. updated_date is the LIST endpoint's
-- `updateDate` string (the detail endpoint calls it `updatedDate`), used only to
-- skip re-expanding unchanged workouts.
-- Join: activities.raw->>'workoutId' = workouts.workout_id::text.
create table if not exists workouts (
    workout_id   bigint primary key,
    workout_name text,
    updated_date text,
    raw          jsonb not null,
    synced_at    timestamptz not null default now()
);

-- Workouts placed on the Garmin calendar (get_scheduled_workouts items with
-- itemType='workout'). schedule_id is the calendar item id.
create table if not exists scheduled_workouts (
    schedule_id    bigint primary key,
    workout_id     bigint not null,
    scheduled_date date not null,
    raw            jsonb not null,
    synced_at      timestamptz not null default now()
);

create index if not exists scheduled_workouts_pair_idx on scheduled_workouts (workout_id, scheduled_date);
