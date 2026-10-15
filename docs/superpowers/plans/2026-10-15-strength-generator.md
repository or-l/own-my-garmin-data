# Strength Workout Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the coach generate real Garmin strength workouts (`protocols.kind = 'strength'`), validated against Garmin's exercise catalogue, and read back per-set results of strength activities.

**Architecture:** A pure builder `workout_gen.make_strength_workout` registered as `BUILDERS["strength"]`, so `protocols.build()`, seed and enact work unchanged. It is pinned field-for-field to a hand-built reference workout and to Bear Essentials. The read-only `workouts` domain gains two ingests: Garmin's public exercise catalogue (`exercise_catalog`, weekly) and per-set results of recent strength activities (`activity_exercise_sets` + the flattening view `activity_exercise_set_rows`). `seed_workouts.plan_seed` refuses protocols with a bad shape or an unknown exercise before anything reaches Garmin.

**Tech Stack:** Python 3.11+, `garminconnect>=0.3.6`, `psycopg[binary]>=3.3`, stdlib `urllib` (no `requests` in ingestion), pytest via `uv run --with`, Postgres 17 in Docker for smoke tests.

**Spec:** `docs/superpowers/specs/2026-10-15-strength-generator-design.md`

## Global Constraints

- `workout_gen.py` stays pure: no I/O, no `garminconnect` import.
- Ingestion modules (`domains.py`, `upserts.py`, `config.py`, `ingestion/pull.py`, `backfill.py`) import only `garminconnect`, `psycopg`, `python-dotenv`, stdlib and each other. Never `garmin_write`/`seed_workouts`/`sync_schedule`/`plan_admin`/`coach_roles`.
- Garmin write endpoints are referenced only in `garmin_write.py` (`tests/test_write_isolation.py` enforces it).
- New migrations are additive (`create … if not exists`, `create or replace view`) and carry `-- domain: workouts`. `PRE_MIGRATIONS_VERSIONS` in `apply_schema.py` is never edited.
- Catalogue URL: `https://connect.garmin.com/web-data/exercises/Exercises.json`. Refresh at most every 7 days. Never delete or wipe rows on a failed or empty fetch.
- Exercise sets: strength activities (`raw->'activityType'->>'typeKey' = 'strength_training'`) started in the last 30 days that have no row yet. Store nothing for an empty or None `exerciseSets`.
- Per-set `weight` arrives in grams. The view exposes `weight_kg = round(weight / 1000.0, 1)`.
- A `kind='strength'` exercise needs `category`, `exercise`, `sets` ≥ 1, exactly one of `reps` (≥ 1) / `time_s` (> 0), optional `weight_kg` (> 0), optional `rest_s` (> 0, or null = lap button, the default), and an optional `note` (≤ 50 chars). `warmup` is `"lap"` (the default) or `null`.
- Out of scope: supersets/circuits, pyramids/drop sets, left/right flag, run/ride generators, exercise-set backfill beyond 30 days.
- Unit test command: `uv run --with pytest --with 'garminconnect>=0.3.6' --with 'psycopg[binary]>=3.3' --with 'python-dotenv>=1.0' pytest tests -q`
- Smoke command (throwaway DB only, never Neon):
  ```bash
  docker run -d --rm --name garmin-smoke -e POSTGRES_PASSWORD=smoke -p 127.0.0.1:55432:5432 postgres:17
  SMOKE_TEST_DATABASE_URL=postgresql://postgres:smoke@localhost:55432/postgres uv run scripts/smoke_test.py
  ```
- Ask the user before any Neon migration, Garmin write, or GitHub secret change (Task 7 only).

## Review Focus

1. **The exercise name is real but sits under the wrong category** (e.g. `{"category": "ROW", "exercise": "PULL_UP"}`). The seed must reject it: the check is on the (category, exercise) pair, not the name alone. Pinned in Task 5 (`test_seed_validates_strength_protocols`, bad-pair protocol).
2. **A typo'd key** (`"weight": 10` instead of `"weight_kg"`), both or neither of `reps`/`time_s`, or `sets: 0`. These must become a clear seed note naming the problem, never a silently dropped field or a crash, and other protocols must still upload. Pinned in Task 1 (`test_strength_rejects_bad_shapes`) and Task 5 (bad-shape protocol).
3. **The catalogue fetch returns garbage or fails** (network error, HTML, `{}`). The pull must not crash and existing rows must survive. Pinned in Task 3 (`test_exercise_catalog_refresh_keeps_rows_on_failure`).
4. **A strength activity whose `exerciseSets` is null or empty**, and REST sets with null reps/weight. No row is stored (retried next sync), and the view must not error or invent weights. Pinned in Task 4 (null payload) and Task 2 (view over REST sets / null `exerciseSets`).
5. **Views aren't covered by the table grants loop** (`pg_tables` excludes views). The coach and the MCP read role must still be able to SELECT `activity_exercise_set_rows`. Pinned in Task 2 (`test_strength_schema_and_view`, coach SELECT on the view).

---

### Task 1: Strength builder (`make_strength_workout`)

**Files:**
- Create: `tests/fixtures/strength_reference.json`, `tests/fixtures/bear_essentials.json`, `tests/test_workout_gen_strength.py`
- Modify: `workout_gen.py` (append strength section; extend `BUILDERS`)

**Interfaces:**
- Consumes: `workout_gen._STEP_TYPES`, `_LAP_BUTTON`, `_TIME`, `_ITERATIONS`, `_NO_TARGET`, `_NO_STROKE`, `_NO_EQUIPMENT` (existing module constants); `tests/test_workout_gen._strip`.
- Produces: `workout_gen.make_strength_workout(name: str, description: str | None = None, *, exercises: list[dict], warmup: str | None = "lap") -> dict`; `workout_gen.STRENGTH_EXERCISE_KEYS: frozenset[str]`; `BUILDERS["strength"] is make_strength_workout`.

- [ ] **Step 1: Fixtures (strip personal data)**

```bash
python3 - <<'EOF'
import json
for src, dst in (("probe_out/template_1730744876.json", "tests/fixtures/strength_reference.json"),
                 ("probe_out/template_1231158443.json", "tests/fixtures/bear_essentials.json")):
    w = json.load(open(src))
    for k in ("author", "ownerId"):
        w.pop(k, None)
    json.dump(w, open(dst, "w"), indent=2, ensure_ascii=False)
EOF
grep -c '"author"\|"ownerId"' tests/fixtures/strength_reference.json tests/fixtures/bear_essentials.json
```
Expected: both counts `0`.

- [ ] **Step 2: Write the failing tests:** `tests/test_workout_gen_strength.py`

```python
import json
import pathlib

import pytest

from tests.test_workout_gen import _strip
from workout_gen import BUILDERS, make_strength_workout

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# What the reference workout (hand-built in Connect, 2026-10-15) prescribes.
REFERENCE_EXERCISES = [
    {"category": "PULL_UP", "exercise": "WEIGHTED_PULL_UP", "sets": 3, "reps": 3, "weight_kg": 10},
    {"category": "PLANK", "exercise": "PLANK", "sets": 3, "time_s": 60},
    {"category": "ROW", "exercise": "DUMBBELL_ROW", "sets": 3, "reps": 10, "weight_kg": 10},
]


def _norm(obj):
    """Garmin's own UI leaves two fields unset on some steps that we always
    send explicitly: skipLastRestStep (null on some repeat groups; we send
    false) and an exercise step's description (null on Bear Essentials, ''
    on newer workouts; we send '' when there's no note)."""
    if isinstance(obj, dict):
        out = {k: _norm(v) for k, v in obj.items()}
        if out.get("type") == "RepeatGroupDTO" and out.get("skipLastRestStep") is None:
            out["skipLastRestStep"] = False
        if out.get("type") == "ExecutableStepDTO" and out.get("exerciseName") and out.get("description") is None:
            out["description"] = ""
        return out
    if isinstance(obj, list):
        return [_norm(v) for v in obj]
    return obj


def test_matches_reference_field_for_field():
    expected = _norm(_strip(json.loads((FIXTURES / "strength_reference.json").read_text())))
    got = make_strength_workout("testing workout for coaching ", None, exercises=REFERENCE_EXERCISES)
    assert got == expected


def test_timed_rests_match_bear_essentials():
    bear = _norm(_strip(json.loads((FIXTURES / "bear_essentials.json").read_text())))
    groups = bear["workoutSegments"][0]["workoutSteps"]
    exercises = []
    for g in groups:
        ex, rest = g["workoutSteps"]
        exercises.append({"category": ex["category"], "exercise": ex["exerciseName"], "sets": g["numberOfIterations"],
                          "reps": int(ex["endConditionValue"]), "rest_s": rest["endConditionValue"]})
    got = make_strength_workout("Bear Essentials", None, exercises=exercises, warmup=None)
    assert got["workoutSegments"][0]["workoutSteps"] == groups


def test_note_goes_to_step_description_and_lap_rest_is_default():
    w = make_strength_workout("x", None, exercises=[
        {"category": "ROW", "exercise": "DUMBBELL_ROW", "sets": 2, "reps": 8, "note": "L"}])
    group = w["workoutSegments"][0]["workoutSteps"][1]
    ex, rest = group["workoutSteps"]
    assert ex["description"] == "L"
    assert rest["endCondition"]["conditionTypeKey"] == "lap.button" and rest["endConditionValue"] == 0.0


def _flatten(steps):
    for s in steps:
        yield s
        yield from _flatten(s.get("workoutSteps") or [])


def test_step_orders_unique_increasing_and_child_ids_per_group():
    w = make_strength_workout("x", None, exercises=REFERENCE_EXERCISES * 2)
    steps = w["workoutSegments"][0]["workoutSteps"]
    orders = [s["stepOrder"] for s in _flatten(steps)]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
    groups = [s for s in steps if s["type"] == "RepeatGroupDTO"]
    assert [g["childStepId"] for g in groups] == list(range(1, 7))
    for g in groups:
        assert {c["childStepId"] for c in g["workoutSteps"]} == {g["childStepId"]}


@pytest.mark.parametrize("bad, fragment", [
    ({"exercises": []}, "at least one exercise"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "weight": 10}]}, "weight"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3}]}, "exactly one of reps / time_s"),
    ({"exercises": [{"category": "PLANK", "exercise": "PLANK", "sets": 3, "reps": 5, "time_s": 30}]}, "exactly one of reps / time_s"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 0, "reps": 5}]}, "sets"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "weight_kg": -2}]}, "weight_kg"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "", "sets": 3, "reps": 5}]}, "exercise"),
    ({"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "note": "x" * 51}]}, "note"),
    ({"exercises": REFERENCE_EXERCISES, "warmup": "5min"}, "warmup"),
])
def test_strength_rejects_bad_shapes(bad, fragment):
    with pytest.raises(ValueError, match=fragment):
        make_strength_workout("x", None, **bad)


def test_kind_seam():
    assert BUILDERS["strength"] is make_strength_workout
```

- [ ] **Step 3: Run to verify failure.** Run the unit test command. Expected: `ImportError: cannot import name 'make_strength_workout'`.

- [ ] **Step 4: Implement:** append to `workout_gen.py`, after `make_other_workout` and before `BUILDERS`. Then replace the `BUILDERS` line.

```python
# ------------------------------------------------------------------ strength
# Garmin "Strength" workouts. Every id and key below was copied from
# workouts hand-built in Garmin Connect and reflected via get_workout_by_id:
# the "testing workout for coaching" reference (weighted reps, timed hold,
# lap-button rests, CARDIO warm-up; tests/fixtures/strength_reference.json)
# and Bear Essentials (timed rests; tests/fixtures/bear_essentials.json).
# tests/test_workout_gen_strength.py pins both field for field.
#
# One repeat group per exercise: (exercise step, rest step) x sets. Per-side
# work is two consecutive exercises with note "L"/"R" — Garmin has no
# left/right flag. Exercise names must come from Garmin's catalogue; that's
# checked against exercise_catalog at seed time, not here (this stays pure).

SPORT_STRENGTH = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 3}
_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
_KILOGRAM = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
_KILOMETER = {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}

STRENGTH_EXERCISE_KEYS = frozenset({"category", "exercise", "sets", "reps", "time_s", "weight_kg", "rest_s", "note"})
_NOTE_MAX = 50


def _strength_step(order: int, step_type: str, *, child: int | None, category: str | None,
                   exercise: str | None, condition: dict, value: float, weight_kg: float | None = None,
                   description: str | None = None, preferred_unit: dict | None = None) -> dict:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": dict(_STEP_TYPES[step_type]),
        "childStepId": child,
        "description": description,
        "endCondition": dict(condition),
        "endConditionValue": float(value),
        "preferredEndConditionUnit": dict(preferred_unit) if preferred_unit else None,
        "endConditionCompare": None,
        "targetType": dict(_NO_TARGET),
        "targetValueOne": None,
        "targetValueTwo": 0.0,
        "targetValueUnit": None,
        "zoneNumber": None,
        "secondaryTargetType": None,
        "secondaryTargetValueOne": None,
        "secondaryTargetValueTwo": None,
        "secondaryTargetValueUnit": None,
        "secondaryZoneNumber": None,
        "endConditionZone": None,
        "strokeType": dict(_NO_STROKE),
        "equipmentType": dict(_NO_EQUIPMENT),
        "category": category,
        "exerciseName": exercise,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": float(weight_kg) if weight_kg is not None else None,
        "weightUnit": dict(_KILOGRAM),
    }


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_strength(exercises, warmup) -> None:
    if warmup not in ("lap", None):
        raise ValueError(f"warmup must be \"lap\" or null (got {warmup!r})")
    if not isinstance(exercises, list) or not exercises:
        raise ValueError("a strength protocol needs at least one exercise")
    for i, ex in enumerate(exercises, 1):
        where = f"exercise {i}"
        if not isinstance(ex, dict):
            raise ValueError(f"{where}: must be an object")
        unknown = set(ex) - STRENGTH_EXERCISE_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: {sorted(STRENGTH_EXERCISE_KEYS)}")
        for key in ("category", "exercise"):
            if not isinstance(ex.get(key), str) or not ex[key].strip():
                raise ValueError(f"{where}: {key} must be a non-empty Garmin catalogue name")
        if not isinstance(ex.get("sets"), int) or isinstance(ex.get("sets"), bool) or ex["sets"] < 1:
            raise ValueError(f"{where}: sets must be an integer >= 1")
        has_reps, has_time = ex.get("reps") is not None, ex.get("time_s") is not None
        if has_reps == has_time:
            raise ValueError(f"{where}: give exactly one of reps / time_s")
        if has_reps and (not isinstance(ex["reps"], int) or isinstance(ex["reps"], bool) or ex["reps"] < 1):
            raise ValueError(f"{where}: reps must be an integer >= 1")
        if has_time and (not _is_number(ex["time_s"]) or ex["time_s"] <= 0):
            raise ValueError(f"{where}: time_s must be > 0 seconds")
        for key in ("weight_kg", "rest_s"):
            if ex.get(key) is not None and (not _is_number(ex[key]) or ex[key] <= 0):
                raise ValueError(f"{where}: {key} must be > 0 (or omitted)")
        note = ex.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > _NOTE_MAX):
            raise ValueError(f"{where}: note must be text of at most {_NOTE_MAX} characters")


def make_strength_workout(
    name: str,
    description: str | None = None,
    *,
    exercises: list[dict],
    warmup: str | None = "lap",
) -> dict:
    _validate_strength(exercises, warmup)

    steps: list[dict] = []
    order = 0
    if warmup == "lap":
        order += 1
        steps.append(_strength_step(order, "warmup", child=None, category="CARDIO", exercise="",
                                    condition=_LAP_BUTTON, value=0.0))
    for group_no, ex in enumerate(exercises, 1):
        order += 1
        group_order = order
        order += 1
        if ex.get("reps") is not None:
            condition, value = _REPS, ex["reps"]
        else:
            condition, value = _TIME, ex["time_s"]
        work = _strength_step(order, "interval", child=group_no, category=ex["category"], exercise=ex["exercise"],
                              condition=condition, value=value, weight_kg=ex.get("weight_kg"),
                              description=ex.get("note") or "", preferred_unit=_KILOMETER)
        order += 1
        rest_s = ex.get("rest_s")
        rest = _strength_step(order, "rest", child=group_no, category=None, exercise=None,
                              condition=_TIME if rest_s is not None else _LAP_BUTTON,
                              value=rest_s if rest_s is not None else 0.0)
        steps.append({
            "type": "RepeatGroupDTO",
            "stepOrder": group_order,
            "stepType": dict(_STEP_TYPES["repeat"]),
            "childStepId": group_no,
            "numberOfIterations": ex["sets"],
            "workoutSteps": [work, rest],
            "endConditionValue": float(ex["sets"]),
            "preferredEndConditionUnit": None,
            "endConditionCompare": None,
            "endCondition": dict(_ITERATIONS),
            "skipLastRestStep": False,
            "smartRepeat": False,
        })

    segment = {
        "segmentOrder": 1,
        "sportType": dict(SPORT_STRENGTH),
        "poolLengthUnit": None,
        "poolLength": None,
        "avgTrainingSpeed": None,
        "estimatedDurationInSecs": None,
        "estimatedDistanceInMeters": None,
        "estimatedDistanceUnit": None,
        "estimateType": None,
        "description": None,
        "workoutSteps": steps,
    }
    return {
        "workoutName": name,
        "description": description,
        "sportType": dict(SPORT_STRENGTH),
        "subSportType": "GENERIC",
        "trainingPlanId": None,
        "sharedWithUsers": None,
        "estimatedDurationInSecs": 0,
        "estimatedDistanceInMeters": 0.0,
        "workoutSegments": [segment],
        "poolLength": None,
        "poolLengthUnit": None,
        "locale": None,
        "workoutProvider": None,
        "workoutSourceId": None,
        "uploadTimestamp": None,
        "atpPlanId": None,
        "consumer": None,
        "consumerName": None,
        "consumerImageURL": None,
        "consumerWebsiteURL": None,
        "workoutNameI18nKey": None,
        "descriptionI18nKey": None,
        "avgTrainingSpeed": 0.0,
        "estimateType": None,
        "estimatedDistanceUnit": {"unitId": None, "unitKey": None, "factor": None},
        "workoutThumbnailUrl": None,
        "isSessionTransitionEnabled": None,
        "shared": False,
    }


BUILDERS: dict[str, Callable[..., dict]] = {"other": make_other_workout, "strength": make_strength_workout}
```
Also update the module docstring's last paragraph: "BUILDERS is the `kind` seam protocols.build() dispatches on: "other" (interval workouts) and "strength" (see the strength section)."

- [ ] **Step 5: Run the tests.** Run the unit test command. Expected: all pass (previous 45 + the new ones). If the field-for-field test fails, read pytest's dict diff and fix the builder. **Never edit a fixture to match.** The only sanctioned differences are the two in `_norm`.

- [ ] **Step 6: Commit**
```bash
git add workout_gen.py tests/test_workout_gen_strength.py tests/fixtures/strength_reference.json tests/fixtures/bear_essentials.json
git commit -m "feat: add strength workout builder verified against hand-built workouts"
```

---

### Task 2: Schema `0011_strength.sql` + view grants

**Files:**
- Create: `schema/migrations/0011_strength.sql`
- Modify: `coach_roles.py` (`apply_coach_grants`: also grant SELECT on public views), `domains.py` (workouts domain `tables` tuple), `tests/test_domains_workouts.py`, `scripts/smoke_test.py`

**Interfaces:**
- Produces tables: `exercise_catalog(category text, exercise text, primary_muscles text[], secondary_muscles text[], counterpart text, synced_at timestamptz, primary key (category, exercise))`, `activity_exercise_sets(activity_id bigint primary key, raw jsonb not null, synced_at timestamptz)`.
- Produces view: `activity_exercise_set_rows(activity_id bigint, set_index int, set_type text, reps int, weight_kg numeric, duration_s numeric, start_time_utc timestamp, category text, exercise text, probability numeric)`.
- Produces: the workouts `Domain.tables == ("workouts", "scheduled_workouts", "exercise_catalog", "activity_exercise_sets")`.

- [ ] **Step 1: Failing tests.** In `tests/test_domains_workouts.py`, change the last assertion of `test_workouts_domain_registered_off_by_default` to:
```python
    assert d.tables == ("workouts", "scheduled_workouts", "exercise_catalog", "activity_exercise_sets")
```
In `scripts/smoke_test.py`, add this test and call it from `main()` after `test_coach_tables_are_self_describing()`:
```python
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
```
Add `import json` and `from decimal import Decimal` to the smoke test imports if missing.

- [ ] **Step 2: Run to verify failure.** Unit tests: the `tables` assertion fails. Smoke: `test_strength_schema_and_view` fails (`exercise_catalog` missing).

- [ ] **Step 3: Migration:** `schema/migrations/0011_strength.sql`
```sql
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
```

- [ ] **Step 4: Views in grants.** In `coach_roles.apply_coach_grants`, directly after the `for t in tables:` loop that grants SELECT, add:
```python
    # Views aren't in pg_tables; grant them read access the same way
    # (e.g. activity_exercise_set_rows). The coach may read every view.
    views = [r[0] for r in conn.execute("select viewname from pg_views where schemaname = 'public'").fetchall()]
    for v in views:
        ex("grant select on {t} to {r}", t=sql.Identifier(v), r=sched)
        ex("grant select on {t} to {r}", t=sql.Identifier(v), r=coach)
```
Then in the read-role block at the end, after the `for t in tables:` loop, add:
```python
        for v in views:
            ex("grant select on {t} to {r}", t=sql.Identifier(v), r=sql.Identifier(roles.read))
```
(`revoke all on all tables in schema public` already covers views, so re-running stays convergent.)

- [ ] **Step 5: Domain tables.** In `domains.py`, change the workouts registry entry to `tables=("workouts", "scheduled_workouts", "exercise_catalog", "activity_exercise_sets")`.

- [ ] **Step 6: Run the unit tests and the smoke suite.** Expected: all PASS.

- [ ] **Step 7: Commit**
```bash
git add schema/migrations/0011_strength.sql coach_roles.py domains.py tests/test_domains_workouts.py scripts/smoke_test.py
git commit -m "feat: add exercise catalogue + per-set results schema, readable view grants"
```

---

### Task 3: Exercise catalogue sync

**Files:**
- Create: `tests/fixtures/exercise_catalog_sample.json`, `tests/test_exercise_catalog.py`
- Modify: `domains.py` (workouts section), `scripts/smoke_test.py`

**Interfaces:**
- Produces: `domains.CATALOG_URL: str`, `domains.CATALOG_MAX_AGE_DAYS = 7`, `domains.parse_exercise_catalog(doc: dict | None) -> list[tuple[str, str, list[str], list[str], str | None]]` (sorted by category, exercise), `domains._fetch_catalog_doc() -> dict` (network; tests replace it), `domains._refresh_exercise_catalog(conn, today: date) -> int` (rows upserted; 0 when fresh, failed or empty).

- [ ] **Step 1: Fixture**: `tests/fixtures/exercise_catalog_sample.json`
```json
{"categories": {
  "PULL_UP": {"exercises": {
    "WEIGHTED_PULL_UP": {"counterpart": "PULL_UP", "primaryMuscles": ["LATS", "TRAPS"], "secondaryMuscles": ["BICEPS", "FOREARM", "SHOULDERS"]},
    "PULL_UP": {"counterpart": "WEIGHTED_PULL_UP", "primaryMuscles": ["LATS"], "secondaryMuscles": ["BICEPS"]}}},
  "PLANK": {"exercises": {"PLANK": {"primaryMuscles": ["ABS"]}}},
  "EMPTY": {"exercises": {}}
}}
```

- [ ] **Step 2: Failing unit tests:** `tests/test_exercise_catalog.py`
```python
import json
import pathlib

import domains

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def test_parse_exercise_catalog_flattens_and_sorts():
    rows = domains.parse_exercise_catalog(json.loads((FIXTURES / "exercise_catalog_sample.json").read_text()))
    assert rows == [
        ("PLANK", "PLANK", ["ABS"], [], None),
        ("PULL_UP", "PULL_UP", ["LATS"], ["BICEPS"], "WEIGHTED_PULL_UP"),
        ("PULL_UP", "WEIGHTED_PULL_UP", ["LATS", "TRAPS"], ["BICEPS", "FOREARM", "SHOULDERS"], "PULL_UP"),
    ]


def test_parse_exercise_catalog_tolerates_garbage():
    assert domains.parse_exercise_catalog(None) == []
    assert domains.parse_exercise_catalog({}) == []
    assert domains.parse_exercise_catalog({"categories": {"X": {"exercises": None}}}) == []
```
Add to `scripts/smoke_test.py`, called from `main()` after `test_strength_schema_and_view()`:
```python
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
```
Near the top of `scripts/smoke_test.py`, after the imports, add a stub so **no** smoke test ever hits the network (the existing `_workouts_sync` tests now refresh the catalogue too):
```python
def _fake_catalog_doc() -> dict:
    return {"categories": {
        "PULL_UP": {"exercises": {"WEIGHTED_PULL_UP": {"primaryMuscles": ["LATS"]}, "PULL_UP": {}}},
        "PLANK": {"exercises": {"PLANK": {}}},
        "ROW": {"exercises": {"DUMBBELL_ROW": {}}},
    }}


domains._fetch_catalog_doc = _fake_catalog_doc
```

- [ ] **Step 3: Run to verify failure.** Expected: `AttributeError: module 'domains' has no attribute 'parse_exercise_catalog'` (unit). The smoke suite fails at the module-level assignment target or the new test.

- [ ] **Step 4: Implement.** In `domains.py`, add `import urllib.request` with the other imports. Then add these constants and functions to the workouts section, after `_replace_scheduled`:
```python
CATALOG_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
CATALOG_MAX_AGE_DAYS = 7


def parse_exercise_catalog(doc: dict | None) -> list[tuple[str, str, list[str], list[str], str | None]]:
    """Garmin's {"categories": {CAT: {"exercises": {NAME: {...}}}}} -> rows,
    sorted by (category, exercise). Tolerates missing/empty pieces."""
    rows = []
    categories = (doc or {}).get("categories") if isinstance(doc, dict) else None
    for category, body in sorted((categories or {}).items()):
        for exercise, info in sorted(((body or {}).get("exercises") or {}).items()):
            info = info or {}
            rows.append((category, exercise, list(info.get("primaryMuscles") or []),
                         list(info.get("secondaryMuscles") or []), info.get("counterpart")))
    return rows


def _fetch_catalog_doc() -> dict:
    """Public, unauthenticated static file the Connect web app loads.
    stdlib only: the ingestion runner has no `requests`."""
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "garmin-data"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _refresh_exercise_catalog(conn: psycopg.Connection, today: date) -> int:
    last = conn.execute("select max(synced_at) from exercise_catalog").fetchone()[0]
    if last is not None and last.date() > today - timedelta(days=CATALOG_MAX_AGE_DAYS):
        return 0
    try:
        rows = parse_exercise_catalog(_fetch_catalog_doc())
    except Exception as e:  # network, HTTP, bad JSON — keep what we have
        print(f"exercise catalog: fetch failed, keeping existing rows ({e})")
        return 0
    if not rows:
        print("exercise catalog: empty/unrecognised response, keeping existing rows")
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into exercise_catalog (category, exercise, primary_muscles, secondary_muscles, counterpart, synced_at)
            values (%s, %s, %s, %s, %s, now())
                on conflict (category, exercise) do update set
                primary_muscles = excluded.primary_muscles, secondary_muscles = excluded.secondary_muscles,
                counterpart = excluded.counterpart, synced_at = now()
            """,
            rows,
        )
    conn.commit()
    return len(rows)
```
In `_workouts_sync`, replace the final `return` with:
```python
    catalog = _refresh_exercise_catalog(ctx.conn, ctx.today)
    return {"workouts": len(listing), "scheduled_workouts": len(scheduled), "exercise_catalog": catalog}
```

- [ ] **Step 5: Run the unit tests and the smoke suite.** Expected: all PASS.

- [ ] **Step 6: Commit**
```bash
git add domains.py tests/test_exercise_catalog.py tests/fixtures/exercise_catalog_sample.json scripts/smoke_test.py
git commit -m "feat: sync Garmin's exercise catalogue weekly into exercise_catalog"
```

---

### Task 4: Per-set results fetch (`activity_exercise_sets`)

**Files:**
- Modify: `domains.py` (workouts section), `scripts/smoke_test.py`

**Interfaces:**
- Consumes: `activities(activity_id, raw, started_at)`; client method `get_activity_exercise_sets(activity_id) -> dict | None`.
- Produces: `domains.EXERCISE_SETS_LOOKBACK_DAYS = 30`, `domains.EXERCISE_SETS_SLEEP_S = 1.0`, `domains._sync_exercise_sets(conn, client, today: date) -> int` (rows stored). `_workouts_sync` also returns `"activity_exercise_sets": n`.

- [ ] **Step 1: Failing smoke test.** Add to `scripts/smoke_test.py` and call it from `main()` after `test_exercise_catalog_refresh_keeps_rows_on_failure()`:
```python
class _SetsStub:
    def __init__(self, payloads: dict):
        self.payloads, self.calls = payloads, []

    def get_activity_exercise_sets(self, activity_id):
        self.calls.append(activity_id)
        payload = self.payloads.get(activity_id)
        if isinstance(payload, Exception):
            raise payload
        return payload


def test_exercise_sets_fetch_if_missing() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    apply_migrations(DB_URL, enabled_domains={"activities", "workouts"})
    domains.EXERCISE_SETS_SLEEP_S = 0
    today = date.today()
    recent = today - timedelta(days=3)
    with psycopg.connect(DB_URL) as conn:
        for aid, kind, started in ((10, "strength_training", recent), (11, "strength_training", recent),
                                   (12, "strength_training", recent), (13, "running", recent),
                                   (14, "strength_training", today - timedelta(days=45))):
            conn.execute("insert into activities (activity_id, raw, started_at) values (%s, %s, %s)",
                         (aid, json.dumps({"activityType": {"typeKey": kind}}), started))
        conn.commit()
        stub = _SetsStub({10: SAMPLE_EXERCISE_SETS, 11: {"activityId": 11, "exerciseSets": None},
                          12: RuntimeError("garmin 500")})
        assert domains._sync_exercise_sets(conn, stub, today) == 1
        assert sorted(stub.calls) == [10, 11, 12], "only recent strength activities without a row"
        stored = [r[0] for r in conn.execute("select activity_id from activity_exercise_sets").fetchall()]
        assert stored == [10], "null payloads and errors store nothing (retried next sync)"
        stub.calls.clear()
        domains._sync_exercise_sets(conn, stub, today)
        assert sorted(stub.calls) == [11, 12], "stored activities are not refetched"
    print("test_exercise_sets_fetch_if_missing: PASS")
```
Also add to the existing `_WorkoutsStub` class (used by `_workouts_sync` smoke tests):
```python
    def get_activity_exercise_sets(self, activity_id):
        return None
```

- [ ] **Step 2: Run to verify failure.** Expected: `AttributeError: module 'domains' has no attribute '_sync_exercise_sets'`.

- [ ] **Step 3: Implement.** In `domains.py`'s workouts section, after `_refresh_exercise_catalog`:
```python
EXERCISE_SETS_LOOKBACK_DAYS = 30
EXERCISE_SETS_SLEEP_S = 1.0


def _sync_exercise_sets(conn: psycopg.Connection, client, today: date) -> int:
    """Fetch-if-missing: per-set results for recent strength activities that
    don't have a row yet. An empty/None payload or an error stores nothing,
    so the activity is retried on the next sync."""
    if conn.execute("select to_regclass('activities')").fetchone()[0] is None:
        return 0
    ids = [r[0] for r in conn.execute(
        """
        select a.activity_id from activities a
          left join activity_exercise_sets s on s.activity_id = a.activity_id
         where s.activity_id is null
           and a.raw->'activityType'->>'typeKey' = 'strength_training'
           and a.started_at >= %s
         order by a.started_at
        """,
        (today - timedelta(days=EXERCISE_SETS_LOOKBACK_DAYS),),
    ).fetchall()]
    stored = 0
    for activity_id in ids:
        try:
            payload = client.get_activity_exercise_sets(activity_id)
        except Exception as e:
            print(f"exercise sets for activity {activity_id}: fetch failed, will retry ({e})")
            payload = None
        if (payload or {}).get("exerciseSets"):
            conn.execute(
                """
                insert into activity_exercise_sets (activity_id, raw, synced_at) values (%s, %s, now())
                    on conflict (activity_id) do update set raw = excluded.raw, synced_at = now()
                """,
                (activity_id, json.dumps(payload)),
            )
            conn.commit()
            stored += 1
        time.sleep(EXERCISE_SETS_SLEEP_S)
    return stored
```
In `_workouts_sync`, replace the final `return` with:
```python
    catalog = _refresh_exercise_catalog(ctx.conn, ctx.today)
    sets = _sync_exercise_sets(ctx.conn, ctx.client, ctx.today)
    return {"workouts": len(listing), "scheduled_workouts": len(scheduled),
            "exercise_catalog": catalog, "activity_exercise_sets": sets}
```

- [ ] **Step 4: Run the unit tests and the smoke suite.** Expected: all PASS, including `test_write_isolation` (ingestion still imports no write side).

- [ ] **Step 5: Commit**
```bash
git add domains.py scripts/smoke_test.py
git commit -m "feat: fetch per-set results of recent strength activities"
```

---

### Task 5: Seed-time validation of strength protocols

**Files:**
- Modify: `protocols.py`, `seed_workouts.py`, `tests/test_protocols.py`, `scripts/smoke_test.py`

**Interfaces:**
- Consumes: `workout_gen.make_strength_workout` (via `protocols.build`), `exercise_catalog`.
- Produces: `protocols.strength_problems(conn, params: dict) -> list[str]` (unknown pairs, or "catalogue not synced"; `[]` if fine) and `protocols.protocol_problems(conn, kind: str, name: str, description: str | None, params: dict) -> list[str]` (shape errors from `build` + catalogue check). `seed_workouts.plan_seed` notes and skips any protocol with problems.

- [ ] **Step 1: Failing tests.** Append to `tests/test_protocols.py`:
```python
def test_strength_builds_through_dispatch():
    params = {"exercises": [{"category": "PLANK", "exercise": "PLANK", "sets": 3, "time_s": 45}]}
    w = build("strength", "Core", "plank day", params)
    assert w["sportType"]["sportTypeKey"] == "strength_training" and w["workoutName"] == "Core"
```
Add to `scripts/smoke_test.py` and call it from `main()` after `test_exercise_sets_fetch_if_missing()`:
```python
def test_seed_validates_strength_protocols() -> None:
    import plan_admin
    coach_url, sched_url = _setup_coach_loop()
    seed_workouts.POLITENESS_SLEEP_S = 0
    tomorrow = date.today() + timedelta(days=1)
    good = {"exercises": [{"category": "PULL_UP", "exercise": "WEIGHTED_PULL_UP", "sets": 4, "reps": 3, "weight_kg": 10, "rest_s": 180}]}
    bad_pair = {"exercises": [{"category": "ROW", "exercise": "PULL_UP", "sets": 3, "reps": 5}]}
    bad_shape = {"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 3, "reps": 5, "weight": 10}]}
    with psycopg.connect(DB_URL) as conn:  # catalogue as ingestion would have it
        conn.execute("insert into exercise_catalog (category, exercise) values ('PULL_UP','WEIGHTED_PULL_UP'), ('PULL_UP','PULL_UP')")
        conn.commit()
    with psycopg.connect(coach_url) as conn:
        ids = []
        for slug, params, day in (("s-good", good, tomorrow), ("s-pair", bad_pair, tomorrow + timedelta(days=1)),
                                  ("s-shape", bad_shape, tomorrow + timedelta(days=2))):
            conn.execute("insert into protocols (slug, name, kind, params) values (%s, %s, 'strength', %s)",
                         (slug, slug, json.dumps(params)))
            ids.append(conn.execute(
                "insert into training_plan (scheduled_date, source, protocol_slug) values (%s, 'generated', %s) returning id",
                (day, slug)).fetchone()[0])
    with psycopg.connect(DB_URL) as conn:
        plan_admin.approve(conn, ids, None)
    with psycopg.connect(sched_url) as conn:
        actions, notes = seed_workouts.plan_seed(conn, date.today())
        assert [a.slug for a in actions] == ["s-good"], actions
        joined = " | ".join(notes)
        assert "s-pair" in joined and "ROW/PULL_UP" in joined, notes
        assert "s-shape" in joined and "weight" in joined, notes
        fake = FakeGarmin()
        seed_workouts.run(conn, lambda: contextlib.nullcontext(fake), today=date.today(), confirm=_yes)
    assert len(fake.uploads) == 1
    uploaded = fake.library[fake.uploads[0]]
    assert uploaded["sportType"]["sportTypeKey"] == "strength_training"
    ex = uploaded["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
    assert (ex["exerciseName"], ex["weightValue"]) == ("WEIGHTED_PULL_UP", 10.0)

    with psycopg.connect(DB_URL) as conn:  # empty catalogue -> refuse every strength protocol
        conn.execute("delete from exercise_catalog")
        conn.execute("delete from generated_workouts")
        conn.commit()
    with psycopg.connect(sched_url) as conn:
        actions, notes = seed_workouts.plan_seed(conn, date.today())
    assert actions == [] and any("not synced" in n for n in notes), notes
    print("test_seed_validates_strength_protocols: PASS")
```

- [ ] **Step 2: Run to verify failure.** Expected: `plan_seed` returns all three slugs as actions (assertion error), or `strength_problems` missing.

- [ ] **Step 3: Implement.** Append to `protocols.py`:
```python
def strength_problems(conn: psycopg.Connection, params: dict) -> list[str]:
    """(category, exercise) pairs Garmin won't accept, checked against the
    synced exercise_catalog. An empty catalogue refuses everything rather
    than letting an unvalidated payload reach Garmin."""
    pairs = [(e.get("category"), e.get("exercise")) for e in (params or {}).get("exercises") or [] if isinstance(e, dict)]
    if not pairs:
        return []
    if not conn.execute("select exists (select 1 from exercise_catalog)").fetchone()[0]:
        return ["exercise catalogue not synced yet — run a pull with the workouts domain enabled"]
    known = set(conn.execute(
        "select category, exercise from exercise_catalog where category = any(%s)",
        (sorted({c for c, _ in pairs if c}),),
    ).fetchall())
    return [f"{c}/{e} is not in Garmin's exercise catalogue (see exercise_catalog)"
            for c, e in pairs if (c, e) not in known]


def protocol_problems(conn: psycopg.Connection, kind: str, name: str, description: str | None, params: dict) -> list[str]:
    """Everything that would stop this protocol uploading cleanly, found
    before any Garmin call: shape errors from the builder, then (strength)
    the catalogue check."""
    try:
        build(kind, name, description, params)
    except (ValueError, TypeError) as e:
        return [str(e)]
    if kind == "strength":
        return strength_problems(conn, params)
    return []
```
In `seed_workouts.plan_seed`, replace the loop
```python
    actions = []
    for slug, current in sorted(wanted.items()):
```
with
```python
    protos = {slug: (kind, name, description, params) for slug, kind, name, description, params in conn.execute(
        "select slug, kind, name, description, params from protocols where slug = any(%s)", (sorted(wanted),)
    ).fetchall()}
    actions = []
    for slug, current in sorted(wanted.items()):
        problems = protocols.protocol_problems(conn, *protos[slug])
        if problems:
            notes.append(f"protocol {slug}: not uploaded — {'; '.join(problems)}")
            continue
```
(The rest of the loop body, `entry = ledger.get(slug)` onward, is unchanged. `seed_workouts` already imports `protocols`.) The scheduler role can SELECT `exercise_catalog` through the existing every-table grant, and the smoke test proves it.

- [ ] **Step 4: Run the unit tests and the smoke suite.** Expected: all PASS, including the existing `test_seed_only_approved_and_rehash` and `test_enactor_round_trip` (hangboard protocols are unaffected).

- [ ] **Step 5: Commit**
```bash
git add protocols.py seed_workouts.py tests/test_protocols.py scripts/smoke_test.py
git commit -m "feat: validate strength protocols against the exercise catalogue before upload"
```

---

### Task 6: Docs

**Files:**
- Modify: `README.md` (Coach loop section), `docs/garmin_api_coverage.md`

- [ ] **Step 1: README.** In "Coach loop (optional)", after "Two workout sources", add a subsection **"Strength workouts"** containing:
  - The `kind='strength'` params example from the `protocols` comment in `0011_strength.sql`, verbatim.
  - "Exercise names come from `exercise_catalog` (Garmin's public catalogue, refreshed weekly by the `workouts` sync). The seed refuses any protocol with an unknown (category, exercise) pair or a malformed exercise, and says why in its output."
  - "After a strength session and a sync, `activity_exercise_set_rows` has one row per set (reps, `weight_kg`, duration, detected exercise). Weights arrive from Garmin in grams and are converted to kg."
  - A "Coach Project instructions — add this" block:
    ```
    Strength sessions: don't reuse a library workout unless it genuinely matches. Generate one: INSERT a protocols row with kind='strength' (format in the protocols table description), taking every (category, exercise) from exercise_catalog, then plan it with source='generated'. After the session, read activity_exercise_set_rows (reps, weight_kg per set) to set the next session's load and reps.
    ```
- [ ] **Step 2: Coverage doc.** In `docs/garmin_api_coverage.md`, add rows to "Currently ingested" for `get_activity_exercise_sets` → `activity_exercise_sets` (workouts domain, strength activities from the last 30 days, fetch-if-missing, weight in grams) and for the public catalogue URL → `exercise_catalog` (not a `Garmin` method; weekly, stdlib urllib). Remove `get_activity_exercise_sets` from "Not used yet → Activity detail". Mention `upload_strength_workout` under "Write operations" as deliberately unused: we upload our own verified payload via `upload_workout`.
- [ ] **Step 3: Run the unit tests and the smoke suite** one final time. Expected: all PASS.
- [ ] **Step 4: Commit**
```bash
git add README.md docs/garmin_api_coverage.md
git commit -m "docs: document strength workouts, exercise catalogue and per-set results"
```

---

### Task 7: Live acceptance — GATED, ask before each step

Nothing here runs without the user's explicit OK. Each step touches Neon or Garmin.

- [ ] **Step 1 (migration + re-grant on Neon):** Ask, then run `uv run install.py --upgrade --domains activities,body_composition,challenges,profile,training,wellness,workouts`. Expected: "Applied 1 migration(s): 0011_strength.sql" and "Re-applied coach-loop grants". Verify read-only that `coach` can `select count(*) from activity_exercise_set_rows`, and that `bk_garmin_data_mcp_reader` has SELECT on the view (`has_table_privilege`).
- [ ] **Step 2 (ingest):** Ask, then run `uv run ingestion/pull.py`. Expected: `exercise_catalog` ≈ 1531 rows; `activity_exercise_sets` holds the strength activities from the last 30 days. Spot-check the view against the Garmin app for activity 24571171847 (1 Oct): a BENCH_PRESS set should show `weight_kg = 12.5`.
- [ ] **Step 3 (first strength upload):** Ask, and say up front that seed uploads a NEW strength workout to the Garmin library. Have the coach, or `COACH_DATABASE_URL` via psql, insert a small `kind='strength'` protocol (e.g. `WEIGHTED_PULL_UP` 3×3 @ 10 kg, rest 180; `PLANK` 3×45 s) and a `training_plan` row. The user approves (`plan_admin.py approve --id N`). Run seed and enact as dry runs, then `--confirm`.
- [ ] **Step 4 (round trip):** After the next pull, compare `workouts.raw` for the uploaded workout id with `protocols.build(...)` for that protocol, minus server-assigned keys, using the same `_strip`/`_norm` as the tests. Expected: identical.
- [ ] **Step 5 (after the session):** Once the user has done it and synced, `activity_exercise_set_rows` for that activity shows ACTIVE sets labelled `WEIGHTED_PULL_UP` / `PLANK` with the right reps/weights.
- [ ] **Step 6:** Ask whether to mark PR #4 ready to merge.
