# Strength workout generator — design

Status: draft for review (2026-10-15)
Builds on: `docs/superpowers/plans/2026-09-30-workout-coach-loop.md` (the coach loop, deployed 2026-10-14).

## Why

The coach (a claude.ai Project using postgres-mcp's `run_write_sql`) can only *generate*
"Other" interval workouts (hangboard). For strength it must reuse a library workout as-is,
and the library has exactly one (Bear Essentials, a generic circuit). So it can't publish
strength sessions designed around the athlete's goals (weighted pull-ups, one-arm pull-up
progressions, core, antagonists), and it can't see what was actually done: the athlete's
freestyle strength sessions log most sets as `UNKNOWN` or mis-guessed exercises.

## Outcome / success criteria

1. The coach writes a `protocols` row with `kind = 'strength'` (exercises, sets, reps **or**
   time, optional weight, rest), plans it in `training_plan` (`source = 'generated'`), the owner
   approves, and the existing seed → enact path uploads and schedules it **unchanged**.
2. The watch guides the session exercise by exercise.
3. After the session and a sync, the coach can read per-set results (reps, weight, duration,
   detected exercise) from a new `activity_exercise_sets` table.
4. The coach picks exercise names from a readable `exercise_catalog` table instead of guessing;
   a protocol naming an exercise Garmin doesn't know never reaches Garmin.
5. Nothing changes for existing `kind = 'other'` protocols, the approval gate, or the role grants
   model.

## Ground truth (verified 2026-10-15, read-only)

- **Reference workout** `1730744876` "testing workout for coaching", hand-built by the athlete in
  Connect, reflected to `probe_out/template_1730744876.json`:
  - warm-up step: `stepType warmup`, `category CARDIO`, `exerciseName ''`, ends on `lap.button`.
  - per exercise: `RepeatGroupDTO` (×sets, `childStepId` n) containing an `interval`
    `ExecutableStepDTO` (`category`, `exerciseName`, end condition `reps` **or** `time`,
    `weightValue` + `weightUnit kilogram` when weighted, `description ''`) and a `rest` step
    (end condition `lap.button` here).
  - exercises covered: `PULL_UP/WEIGHTED_PULL_UP` 3×3 @ 10 kg, `PLANK/PLANK` 3×60 s,
    `ROW/DUMBBELL_ROW` 3×10 @ 10 kg.
- **Bear Essentials** `1231158443` (already in `workouts.raw`): the same shape with **timed** rests
  (`time` = 60 s) and no weight — the timed-rest variant.
- **Catalogue**: `https://connect.garmin.com/web-data/exercises/Exercises.json` — public, no auth,
  `{"categories": {CAT: {"exercises": {NAME: {primaryMuscles, secondaryMuscles, counterpart?}}}}}`,
  47 categories / 1531 exercises. Contains `PULL_UP/WEIGHTED_PULL_UP`, `CORE/HANGING_L_SIT`,
  `CORE/MODIFIED_FRONT_LEVER`, `ROW/FACE_PULL`, one-arm variants, etc.
- **Per-set results**: `get_activity_exercise_sets(activity_id)` on a strength activity returns
  `exerciseSets[]` with `setType` (ACTIVE/REST), `repetitionCount`, `weight`, `duration`,
  `startTime`, `exercises[]` (category, name, probability). 22 sets for activity 24681964793.
  "Other" activities return `exerciseSets: null`. The activities list payload already carries a
  per-category rollup (`summarizedExerciseSets`). Weights are in grams (see §5).
- Garmin has **no left/right flag** on a step; one-arm exercises are just names.

## Design

### 1. `kind = 'strength'` protocol params (the coach-facing contract)

```json
{
  "warmup": "lap",
  "exercises": [
    {"category": "PULL_UP", "exercise": "WEIGHTED_PULL_UP", "sets": 4, "reps": 3, "weight_kg": 10, "rest_s": 180},
    {"category": "PLANK",   "exercise": "PLANK",            "sets": 3, "time_s": 45, "rest_s": null},
    {"category": "ROW",     "exercise": "DUMBBELL_ROW",     "sets": 3, "reps": 10, "weight_kg": 12, "rest_s": 90, "note": "L"},
    {"category": "ROW",     "exercise": "DUMBBELL_ROW",     "sets": 3, "reps": 10, "weight_kg": 12, "rest_s": 90, "note": "R"}
  ]
}
```

- Each exercise has **exactly one** of `reps` (≥1) or `time_s` (>0); `sets` ≥1; `weight_kg`
  optional (>0); `rest_s` = seconds, or `null` = rest until lap button (default `null`, matching the
  reference); `note` optional, ≤ 50 chars, goes to the exercise step's `description` (cues, "L"/"R").
- `warmup`: `"lap"` (default — warm-up step ending on lap button, as in the reference) or `null`
  (no warm-up step). No cool-down (the reference has none).
- Per-side work = two consecutive entries with `note` "L"/"R". No special feature.
- Out of v1: supersets/circuits (alternating exercises within one repeat group), drop sets,
  per-set varying reps/weight (pyramids) — each would be a new verified shape later.

### 2. `workout_gen.make_strength_workout(name, description, *, exercises, warmup="lap") -> dict`

- Pure function, no I/O, no garminconnect import — same rules as `make_other_workout`.
- Builds the payload with `sportType strength_training (id 5)`, one segment, warm-up step, then per
  exercise a `RepeatGroupDTO` (`numberOfIterations = sets`, `childStepId` = 1-based group index,
  `skipLastRestStep = false`) wrapping the exercise step and its rest step. Step orders unique and
  increasing across nesting (as in the reference).
- Validates the params shape (above) and raises `ValueError` with a clear message.
- Registered as `BUILDERS["strength"]`, so `protocols.build()` dispatches with no other change.
- **Verified field-for-field** (minus server-assigned keys) against the reference reflection
  (rest-on-lap, reps, time, weight, warm-up) and against Bear Essentials for timed rests — tests
  pin both, exactly like the `finger` fixture.

### 3. `exercise_catalog` (read-only reference table, workouts domain)

- Migration `0011_strength.sql` (`-- domain: workouts`):
  `exercise_catalog(category text, exercise text, primary_muscles text[], secondary_muscles text[],
  counterpart text, synced_at, primary key (category, exercise))`.
- Filled by the `workouts` domain sync from the public JSON via stdlib `urllib` (ingestion's
  dependency ceiling forbids `requests`), at most once per 7 days (skipped when fresh). A failed
  fetch logs and keeps the existing rows (never wipes on error/empty).
- Table + column comments tell the coach: pick `(category, exercise)` from here; search with
  `ilike`; muscles help choose antagonists.

### 4. Seed-time validation (the gate before Garmin)

- `seed_workouts.py`, for `kind = 'strength'`: every `(category, exercise)` must exist in
  `exercise_catalog`; otherwise the protocol is **skipped with a note** naming the bad pairs (never
  uploaded). Empty catalogue → skip strength protocols with "catalogue not synced yet".
- Shape errors from the builder are reported the same way (existing per-action error handling).

### 5. `activity_exercise_sets` (per-set results, workouts domain)

- Same migration: `activity_exercise_sets(activity_id bigint primary key, raw jsonb not null,
  synced_at)` — one row per strength activity holding the `get_activity_exercise_sets` payload
  (raw, like every other ingestion table).
- Fetch-if-missing during the `workouts` domain sync: strength activities (`activityType.typeKey =
  'strength_training'`) from the last 30 days with no row yet; politeness sleep between calls;
  an empty/None response stores nothing (retry next sync). No backfill beyond 30 days in v1.
- A view `activity_exercise_set_rows` flattens `raw->'exerciseSets'` into one row per set
  (activity_id, set index, set_type, reps, weight_kg, duration_s, start_time, top exercise category /
  name / probability) so the coach queries it with plain SQL. `weight` arrives in **grams** —
  verified 2026-10-15: `summarizedExerciseSets.maxWeight` 12500 shows as 12.5 kg in the Garmin
  app and `volume` = reps × weight (21 × 12500 = 262500) — so the view exposes
  `weight_kg = weight / 1000.0` (one decimal, matching the app).
- Comments explain: join to `training_plan` via `activities`, ACTIVE vs REST, low-probability
  exercises = watch guessing.

### 6. Coach-facing docs

- `0011` also comments `protocols.kind/params` for `strength` (pointing at `exercise_catalog`) and
  the two new tables.
- README coach-loop section: the strength kind, catalogue, per-set results.
- Project-instructions snippet for the athlete: generate strength sessions with `kind='strength'`;
  read `activity_exercise_set_rows` to progress load/reps.

## Data flow

```
coach: insert protocols(kind='strength', params) + training_plan(source='generated')
owner: plan_admin approve  →  seed: validate vs exercise_catalog → build → upload
enact: schedule  →  watch guides session  →  sync: activities + activity_exercise_sets
coach: run_sql on activity_exercise_set_rows ⋈ activities ⋈ training_plan  →  next prescription
```

## Error handling

- Unknown exercise / bad shape → seed skips that protocol with a named reason; other protocols
  continue. Garmin is never sent an unvalidated strength payload.
- Catalogue fetch failure → keep existing rows; seed still validates against them.
- Exercise-sets fetch failure for one activity → log, continue, retry next sync.
- No change to approval, fingerprint (params are already in `protocol_hash`) or grants beyond
  re-running `apply_coach_grants` on upgrade (new tables get coach/scheduler SELECT by the existing
  deny-list rule).

## Testing

- Unit: field-for-field vs the reference fixture (stripped of ids/owner/name/date) and vs Bear
  Essentials for timed rests; shape validation errors; `BUILDERS["strength"]`; catalogue JSON
  parsing; set-rows flattening logic (incl. grams → kg).
- Smoke (Docker Postgres): migration creates the tables/view/comments; catalogue upsert keeps rows
  on fetch failure; exercise-sets fetch-if-missing with a stub client; seed skips a protocol with an
  unknown exercise and uploads a valid one (fake Garmin).
- Live (gated, ask first): one generated strength workout uploaded, read back via
  `get_workout_by_id` and compared to what was sent (same round-trip check as "Finger repeaters
  7/3"); after the athlete does it, `activity_exercise_set_rows` shows labelled sets.

## Out of scope

Supersets/circuits, pyramids/drop sets, a left/right flag, run/ride generators with pace/HR targets,
backfilling exercise sets older than 30 days, editing Garmin's catalogue.
