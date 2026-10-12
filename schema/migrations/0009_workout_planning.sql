-- domain: workouts
-- Coach-loop planning tables. Created with the (read-only) workouts domain so
-- the schema is uniform, but only writable once the coach loop is opted into
-- (install.py --coach-loop creates the coach/scheduler roles and GRANTs —
-- see coach_roles.py; that file is the grants spec).

create or replace function set_updated_at() returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end
$$;

-- Workout recipes the coach authors; params are workout_gen kwargs for `kind`.
create table if not exists protocols (
    slug        text primary key,
    name        text not null,
    description text,
    kind        text not null default 'other',
    params      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- One row per planned workout-day. There is deliberately no 'approved' status
-- (approval is a plan_approvals row the coach role can't write) and no 'done'
-- status (derive it: activities.raw->>'workoutId' joined to the enacted id).
-- `skip` is the coach's cancel intent. status / schedule_id /
-- resolved_workout_id / enacted_date are written only by the enactor
-- (scheduler role, column-level UPDATE): status describes the calendar, not
-- the coach's wishes.
create table if not exists training_plan (
    id                  bigint generated always as identity primary key,
    scheduled_date      date not null,
    source              text not null check (source in ('generated', 'library')),
    protocol_slug       text references protocols (slug),
    workout_id          bigint,
    workout_name        text,
    skip                boolean not null default false,
    status              text not null default 'planned' check (status in ('planned', 'scheduled', 'skipped')),
    note                text,
    schedule_id         bigint,
    resolved_workout_id bigint,
    enacted_date        date,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    -- Tighter than the spec on purpose: a library row sets EXACTLY one of
    -- workout_id / workout_name, so the owner can't approve "Easy Run" by name
    -- while the row points at a different workout by id.
    constraint training_plan_source_fields check (
        (source = 'generated' and protocol_slug is not null and workout_id is null and workout_name is null)
        or (source = 'library' and protocol_slug is null and ((workout_id is null) <> (workout_name is null)))
    )
);

create unique index if not exists training_plan_day_item_uniq
    on training_plan (scheduled_date, coalesce(protocol_slug, workout_name, workout_id::text));

-- Ledger: which Garmin workout currently materializes each generated protocol.
-- Written only by the enactor (it's the only thing that knows real workoutIds).
-- params_hash = planning.protocol_hash(kind, name, description, params).
create table if not exists generated_workouts (
    protocol_slug text primary key,
    workout_id    bigint not null,
    params_hash   text not null,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- The hard gate. Only the owner can write it (plan_admin.py). fingerprint pins
-- exactly what was approved (planning.fingerprint) so a later coach edit to the
-- row or its protocol's params voids the approval instead of riding on it.
create table if not exists plan_approvals (
    plan_id     bigint primary key references training_plan (id) on delete cascade,
    fingerprint text not null,
    approved_by text not null default current_user,
    approved_at timestamptz not null default now(),
    note        text
);

drop trigger if exists protocols_updated_at on protocols;
create trigger protocols_updated_at before update on protocols
    for each row execute function set_updated_at();
drop trigger if exists training_plan_updated_at on training_plan;
create trigger training_plan_updated_at before update on training_plan
    for each row execute function set_updated_at();
drop trigger if exists generated_workouts_updated_at on generated_workouts;
create trigger generated_workouts_updated_at before update on generated_workouts
    for each row execute function set_updated_at();

-- An enacted row is on the Garmin calendar. Deleting it would orphan that
-- entry (and cascade its approval away), so only the table owner may. The
-- coach sets skip = true, the enactor unschedules and clears
-- resolved_workout_id, and then the row can be deleted.
create or replace function training_plan_guard_delete() returns trigger language plpgsql as $$
begin
    if old.resolved_workout_id is not null
       and current_user <> (select pg_get_userbyid(relowner) from pg_class where oid = tg_relid) then
        raise exception 'plan % is on the Garmin calendar: set skip = true and run the enactor before deleting', old.id
            using errcode = 'insufficient_privilege';
    end if;
    return old;
end
$$;
drop trigger if exists training_plan_guard_delete on training_plan;
create trigger training_plan_guard_delete before delete on training_plan
    for each row execute function training_plan_guard_delete();
