-- domain: workouts
-- Strength workouts: Garmin's exercise catalogue (what kind='strength'
-- protocols may name) and per-set results of strength activities, plus
-- coach-facing comments. Read-only for coach/scheduler (granted by
-- coach_roles.apply_coach_grants, which now also covers views).

create table if not exists exercise_catalog (
    category          text not null,
    exercise          text not null,
    primary_muscles   text[] not null default '{}',
    secondary_muscles text[] not null default '{}',
    counterpart       text,
    synced_at         timestamptz not null default now(),
    primary key (category, exercise)
);

create table if not exists activity_exercise_sets (
    activity_id bigint primary key,
    raw         jsonb not null,
    synced_at   timestamptz not null default now()
);

-- One row per set. Garmin reports weight in grams (12500 = 12.5 kg in the
-- app); startTime is UTC. The exercise shown is the watch's top guess for
-- that set (for a guided workout, the planned exercise).
create or replace view activity_exercise_set_rows as
select s.activity_id,
       (e.ord - 1)::int                                   as set_index,
       e.item->>'setType'                                 as set_type,
       (e.item->>'repetitionCount')::int                  as reps,
       round((e.item->>'weight')::numeric / 1000.0, 1)    as weight_kg,
       round((e.item->>'duration')::numeric, 1)           as duration_s,
       (e.item->>'startTime')::timestamp                  as start_time_utc,
       top.category,
       top.exercise,
       top.probability
  from activity_exercise_sets s
 cross join lateral jsonb_array_elements(
           case when jsonb_typeof(s.raw->'exerciseSets') = 'array' then s.raw->'exerciseSets' else '[]'::jsonb end
       ) with ordinality as e(item, ord)
  left join lateral (
        select x->>'category' as category, x->>'name' as exercise, (x->>'probability')::numeric as probability
          from jsonb_array_elements(coalesce(e.item->'exercises', '[]'::jsonb)) x
         order by (x->>'probability')::numeric desc nulls last
         limit 1
  ) top on true;

comment on table exercise_catalog is
'READ-ONLY reference: Garmin''s exercise catalogue, refreshed weekly by the sync. Every exercise in a kind=''strength'' protocol must be a (category, exercise) pair from this table — the seed refuses any other pair, so look names up instead of guessing (e.g. where exercise ilike ''%PULL_UP%''). primary_muscles/secondary_muscles help pick antagonists; counterpart is the weighted/unweighted twin (WEIGHTED_PULL_UP <-> PULL_UP).';

comment on table activity_exercise_sets is
'READ-ONLY: raw per-set results of strength activities from the last 30 days (get_activity_exercise_sets), fetched by the sync. Query the activity_exercise_set_rows view instead.';

comment on view activity_exercise_set_rows is
'READ-ONLY: one row per set of a strength activity. set_type ACTIVE = work set, REST = rest between sets. weight_kg is already converted from Garmin''s grams (one decimal, as in the Garmin app). category/exercise = what the watch recorded: for a guided (generated) workout the planned exercise, for freestyle a guess — check probability (low = unreliable, UNKNOWN = not recognised). To compare planned vs done: join activities on activity_id, then training_plan on (activities.raw->>''workoutId'')::bigint = resolved_workout_id and (raw->>''startTimeLocal'')::date = enacted_date.';

comment on table protocols is
'Workout recipes the coach may author for source=''generated'' plan rows. Editing a protocol voids approvals of plan rows that use it. Two kinds:
(1) kind=''other'' — interval workout on Garmin''s ''Other'' sport (hangboard etc.): warm-up (ends on lap button), reps × (work_s on / rest_s off), cool-down (lap button). params (seconds): {"work_s": 7, "rest_s": 3, "reps": 6}, optional "warmup_s"/"cooldown_s"; keep "sets" at 1 (multi-set not verified yet).
(2) kind=''strength'' — guided Garmin Strength workout, one exercise at a time. params: {"warmup": "lap" (default) or null, "exercises": [{"category": "PULL_UP", "exercise": "WEIGHTED_PULL_UP", "sets": 4, "reps": 3, "weight_kg": 10, "rest_s": 180, "note": "full lockout"}, {"category": "PLANK", "exercise": "PLANK", "sets": 3, "time_s": 45}]}. Each exercise: category + exercise from exercise_catalog (required), sets >= 1, exactly one of reps / time_s, optional weight_kg, rest_s in seconds (omit = rest until lap button), note <= 50 chars shown on the watch (use "L"/"R" with two consecutive entries for per-side work). No supersets/pyramids. Unknown keys or exercises are refused at upload.
name and description are what the athlete sees on the watch. For runs, rides etc. there is no generator: schedule an existing library workout (training_plan.source=''library'') instead.';
