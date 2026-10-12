# Workout Coach Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manual, approval-gated write side (generate workouts → upload → schedule on the Garmin calendar) whose isolation from the read-only ingestion pipeline is enforced by Postgres roles and GRANTs rather than by code discipline.

**Architecture:** A new read-only `workouts` domain ingests the workout library and the scheduled calendar. Four planning tables (`protocols`, `training_plan`, `generated_workouts`, `plan_approvals`) sit behind two new roles: `coach` (the MCP write tool; authors plans and protocols; no token access and cannot approve) and `scheduler` (the enactor; reads the token, writes the ledger and the enactment columns). Approval is an owner-only row in `plan_approvals` that carries a fingerprint of the approved row, so an edit made after approval voids it. All Garmin write calls live in one module, `garmin_write.py`, and an AST test enforces that.

**Tech Stack:** Python 3.11+, `garminconnect>=0.3.6`, `psycopg[binary]>=3.3`, `python-dotenv`, `uv` inline-script deps (PEP 723), pytest (via `uv run --with`), Postgres 17 in Docker for the role-level smoke tests, GitHub Actions, FastMCP (separate repo).

**Spec:** `docs/superpowers/plans/2026-09-30-workout-coach-loop.spec.md`

## Findings that change the spec (read before any task)

Everything below was verified on 2026-09-30 against this repo, the live DB (through the read MCP) or the probe dumps in `probe_out/`.

1. **No `ingestion` role exists.** `pull.py`/`backfill.py` connect with the owner `DATABASE_URL` (GitHub secret `NEON_DATABASE_URL`). **Deviation:** don't create one. Ingestion stays on the owner URL, and this plan changes nothing about it. Moving it would change an existing install's secret, which breaks invariant 8. The README says this explicitly.
2. **The read role is `bk_garmin_data_mcp_reader`, not `read`** (the MCP repo's docstring still says `analysis_reader`). It has **no** SELECT on `auth_tokens` (verified: `has_table_privilege → false`). **Deviation:** the read-role name is configurable (asked for during opt-in). `apply_coach_grants` grants it SELECT on every table except `auth_tokens`, but only if the role exists. It never revokes anything from the read role.
3. **Part 0a is already answered by `probe_out/scheduled_2026_0{9,10}.json`:**
   - `get_scheduled_workouts(year, month)` takes a **1-based** month (the library subtracts 1).
   - It returns the whole calendar grid, including neighbouring-month days (the October dump contains Sept 24–28) and non-workout items (weight, activity, nap, …). **Dedupe by item `id`; filter `itemType == 'workout'`.**
   - A workout item carries `id`, `workoutId`, `date`, `title`, `sportTypeKey`.
   - **Verified 2026-09-30 (read-only `get_scheduled_workout_by_id(1788349227)`):** the calendar item `id` **is** the `workoutScheduleId`, so unscheduling by the live calendar id is correct. The schedule record also carries `associatedActivityId` (Garmin's own planned→done link), but only on the per-id call, not in the calendar list.
   - The enactor's diff still keys on `(workoutId, date)` (spec invariant 6).
   - **Change marker:** the `get_workouts` list names it `updateDate`; `get_workout_by_id` names it `updatedDate`. Change detection must use the list's `updateDate`.
4. **The activity→workout join works from the list endpoint.** `activities.raw->>'workoutId'` is populated (26 rows; activity 24494258276 → 1709094396), so no per-activity fetch is needed.
5. **Approval hole (spec addition).** With the spec's grants, `coach` can UPDATE an already-approved `training_plan` row (date, slug, name) or a protocol's `params`, and the enactor would then act on content nobody approved. **Addition:**
   - `plan_approvals.fingerprint` stores a hash of the row's authored fields plus, for generated rows, a hash of **everything seed uploads** for the protocol: `kind`, `name`, `description` and `params` (`planning.protocol_hash`, also stored in the ledger's `params_hash` column). Seed and enactor skip any row whose current fingerprint differs ("changed since approval, re-approve").
   - `coach` gets **column-level** INSERT/UPDATE on `training_plan`'s authored columns only.
   - `scheduler` gets column-level UPDATE on the enactor-owned columns only (`status`, `schedule_id`, `resolved_workout_id`, `enacted_date`).
   - **What the owner sees is what gets enacted.** A library row must set **exactly one** of `workout_id` / `workout_name` (a tightened CHECK), so a row can't show one workout by name while pointing at another by id. `plan_admin list` / `approve` show generated rows' protocol name + params, and library-by-id rows' synced library name.
6. **Token save as `scheduler` would fail.** `save_token_to_db` is `INSERT … ON CONFLICT DO UPDATE`, which needs INSERT, and the spec grants only SELECT + UPDATE. **Deviation:** `garmin_write.py` saves with a plain `UPDATE … WHERE provider='garmin'`. The grant is not widened.
7. **Wrapper names.** The AST test flags any attribute access named `upload_workout` etc. outside `garmin_write.py`, so a wrapper with that name would false-positive on `garmin_write.upload_workout(...)`. **Deviation:** the wrappers are `garmin_write.upload / schedule / unschedule / delete`. Likewise `protocols.build(kind, name, description, params)` takes the fields from the `protocols` table, which is the source of truth, instead of `build(slug, params)`.
8. **Provided artifacts didn't exist on disk.** The user chose to have them written here: `workout_gen.py` is derived field-for-field from the verified `finger` workout (`probe_out/template_1709094396.json`), `0008_workouts.sql`, and the workouts domain in `domains.py`.
9. **`probe_workout_link.py`** (staged, uncommitted) calls write endpoints directly. It moves to `scripts/` and routes its writes through `garmin_write`, so the isolation test has exactly one allow-listed file.
10. **Part 11's MCP repo** is `~/Workspace/my-garmin-connect-data` (`analysis/mcp_server.py` + `analysis/db_tools.py`), as chosen by the user.
11. **Cancellation hole (amendment, 2026-10-12 review).** In the original design, nothing ever removed a cancelled session from Garmin. `partition_for_enactment` dropped `status='skipped'` rows, and a coach `DELETE` cascaded the approval away, so the calendar entry was orphaned in both cases. `coach` could also write `status`, including `'scheduled'`. **Amendment:**
    - New coach-authored column `training_plan.skip boolean` holds the coach's intent. `status` becomes **enactor-owned only**: `coach` loses its column grant on `status` and gets one on `skip`.
    - The enactor unschedules every row with `skip = true` that still has an enacted calendar entry (`resolved_workout_id`/`enacted_date` set and live on Garmin), **whatever its approval state**. Removal is the safe direction. It then sets `status='skipped'` and clears the enactment columns.
    - `skip` is **not** in the fingerprint. Un-skipping restores content that was already approved.
    - A `before delete` trigger rejects deleting an enacted row (`resolved_workout_id is not null`) unless the session user owns the table. The coach must skip first, let the enactor remove the entry, then delete.
12. **Same-run double schedule (amendment, 2026-10-12 review).** The unique index keys on `coalesce(slug, name, id::text)`, so a library row by `workout_id = 555` and another by `workout_name = 'Bear Essentials'` can sit on the same day. Both resolve to the same `(workout_id, date)`. **Amendment:** `plan_enact` gives each target one owner per run. A row already recorded in sync owns it; otherwise the first row by `(date, id)` does. Any other row gets a "same workout and day as plan N" note and is never scheduled or recorded.

## Global Constraints

- Ingestion modules (`domains.py`, `upserts.py`, `config.py`, `ingestion/pull.py`, `backfill.py`) import only `garminconnect`, `psycopg`, `python-dotenv` and each other. No `rich`, `questionary` or `requests`, and never `garmin_write`, `seed_workouts`, `sync_schedule` or `plan_admin`.
- Garmin write endpoints (`upload_workout`, `schedule_workout`, `unschedule_workout`, `delete_workout`) are referenced **only** in `garmin_write.py`.
- New migrations are additive (`create … if not exists`) and carry `-- domain: workouts`. `PRE_MIGRATIONS_VERSIONS` in `apply_schema.py` is **never** edited.
- The `workouts` domain has `default_enabled=False` and `backfill=None`.
- No scheduled (cron) workflow writes to Garmin. `enact.yml` is `workflow_dispatch` only.
- `seed_workouts.py` and `sync_schedule.py` dry-run by default. They write only with `--confirm`, or when a TTY user types `yes`.
- `seed_workouts.py`/`sync_schedule.py` connect with `SCHEDULER_DATABASE_URL`. `plan_admin.py` connects with `DATABASE_URL` (owner). The MCP write tool connects with `COACH_DATABASE_URL`.
- Roles are created only by the explicit coach-loop opt-in (`install.py --coach-loop`, or its separate prompt).
- Politeness: `time.sleep(1)` between Garmin calls in loops (module constants, so tests can zero them).
- **Unit test command** (used by every task): `uv run --with pytest --with 'garminconnect>=0.3.6' --with 'psycopg[binary]>=3.3' --with 'python-dotenv>=1.0' pytest tests -q`
- **Smoke DB** (role-level tests need a real Postgres; roles are cluster-wide, so never use the production Neon project):
  ```bash
  docker run -d --rm --name garmin-smoke -e POSTGRES_PASSWORD=smoke -p 55432:5432 postgres:17
  export SMOKE_TEST_DATABASE_URL=postgresql://postgres:smoke@localhost:55432/postgres
  uv run scripts/smoke_test.py
  ```
- **Ask the user before** any Garmin write, any migration or role DDL against the real Neon DB, and before setting any GitHub secret.

## Review Focus

1. **The coach edits a row or protocol after approval.** The enactor must schedule nothing for it and must report "changed since approval". Pinned in Task 9 (`test_enactor_round_trip`, tamper step) and Task 8 (protocol rename).
2. **A crash between `schedule_workout` and the DB status update, then a re-run.** The pair is already on the Garmin calendar, so the re-run must record it without scheduling a duplicate. Pinned in Task 9 (status reset step).
3. **A library `workout_name` that matches 0 or 2+ workouts.** Skip with a note and never schedule a guess. Pinned in Task 9 (ambiguous-name step).
4. **`get_workouts` transiently returns `[]`.** The `workouts` table must not be wiped. Pinned in Task 2 (`test_workouts_sync_keeps_rows_on_empty_listing`).
5. **Running as `scheduler` (UPDATE-only on `auth_tokens`) must still persist a refreshed token, and running without `--confirm` in CI (no TTY) must write nothing.** Pinned in Task 5 (`test_confirmed_*`) and Task 6 (`test_scheduler_token_save`).
6. **The coach cancels an enacted session.** It must disappear from the Garmin calendar on the next enact, and the coach must not be able to `DELETE` it first and orphan the entry. Pinned in Task 9 (skip step) and Task 6 (delete-guard assertion).
7. **Two plan rows resolve to the same workout on the same day.** Exactly one calendar entry, and a note for the other row. Pinned in Task 9 (`dup_id` step).

---

### Task 0: Human probe for multi-set nesting (Part 0b) — async, non-blocking

**Files:** none (the result feeds a later edit to `workout_gen.py`)

This needs the user. Nothing else blocks on it. Until it's done, `workout_gen.py`'s `sets > 1` branch stays marked UNVERIFIED and `DEFAULT_PROTOCOLS` uses single-set protocols only.

- [ ] **Step 1:** Ask the user to hand-build, in Garmin Connect, an "Other" workout: warm-up, then **2 sets** of (repeat ×6: 7 s interval / 3 s recovery) separated by a 180 s rest, then cool-down. They should save it as `probe-multiset`.
- [ ] **Step 2:** Reflect it (read-only): `uv run scripts/probe_workout_link.py reflect <id>` → `probe_out/template_<id>.json`.
- [ ] **Step 3:** Diff it against the generator:
  ```bash
  uv run --with 'garminconnect>=0.3.6' python -c "
  import json,sys; sys.path.insert(0,'.')
  from workout_gen import make_other_workout
  from tests.test_workout_gen import _strip
  got = make_other_workout('probe-multiset', None, work_s=7, rest_s=3, reps=6, sets=2)
  exp = _strip(json.load(open('probe_out/template_<id>.json')))
  print(json.dumps(got, indent=1, sort_keys=True))" > /tmp/got.json
  ```
  Compare it to the reflected template (`jq -S .`). Fix `_multi_set_steps` in `workout_gen.py` until they match. Add the stripped reflection as `tests/fixtures/multiset_workout.json` and a field-for-field test mirroring `test_single_set_matches_finger_field_for_field`. Delete the UNVERIFIED comments.
- [ ] **Step 4:** Commit: `git commit -m "fix: verify multi-set workout nesting against a hand-built reflection"`

---

### Task 1: Test harness, fixtures, and the `Other` workout generator (Part 1)

**Files:**
- Create: `tests/conftest.py`, `tests/fixtures/finger_workout.json`, `tests/fixtures/calendar_2026_09.json`, `workout_gen.py`, `tests/test_workout_gen.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `workout_gen.make_other_workout(name: str, description: str | None = None, *, work_s: float, rest_s: float, reps: int, sets: int = 1, set_rest_s: float = 180.0, warmup_s: float = 300.0, cooldown_s: float = 300.0) -> dict`
- Produces: `workout_gen.BUILDERS: dict[str, Callable[..., dict]]` = `{"other": make_other_workout}` (the `kind` seam)
- Produces: `tests/test_workout_gen._strip(obj)` (removes server-assigned keys)

- [ ] **Step 1: Harness + fixtures + gitignore**

`tests/conftest.py`:
```python
import pathlib
import sys

# Tests import repo-root modules (workout_gen, planning, domains, ...) the
# same way the scripts do: by putting the repo root on sys.path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
```

Build the fixtures from the probe dumps. Strip PII (author/owner, real activity titles):
```bash
mkdir -p tests/fixtures
python3 - <<'EOF'
import json
w = json.load(open("probe_out/template_1709094396.json"))
for k in ("author", "ownerId"):
    w.pop(k, None)
json.dump(w, open("tests/fixtures/finger_workout.json", "w"), indent=2, ensure_ascii=False)

cal = json.load(open("probe_out/scheduled_2026_09.json"))
keep = []
for it in cal["calendarItems"]:
    if it["itemType"] == "workout":
        keep.append(it)
    elif it["itemType"] in ("weight", "activity") and not any(k["itemType"] == it["itemType"] for k in keep):
        it = dict(it, title="redacted", weight=None)
        keep.append(it)
cal["calendarItems"] = keep
json.dump(cal, open("tests/fixtures/calendar_2026_09.json", "w"), indent=2)
EOF
printf 'probe_out/\n' >> .gitignore
```
Expected: `calendar_2026_09.json` has 3 items (one workout, one weight, one activity).

- [ ] **Step 2: Write the failing test** — `tests/test_workout_gen.py`
```python
import json
import pathlib

import pytest

from workout_gen import BUILDERS, make_other_workout

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# Keys Garmin assigns server-side; a create payload must not carry them.
SERVER_ASSIGNED = {"workoutId", "ownerId", "updatedDate", "createdDate", "author", "stepId"}


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in SERVER_ASSIGNED}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def test_single_set_matches_finger_field_for_field():
    expected = _strip(json.loads((FIXTURES / "finger_workout.json").read_text()))
    got = make_other_workout(
        "finger", "20mm edge, 7s/3s repeaters ×6, ~80% max",
        work_s=7, rest_s=3, reps=6,
    )
    assert got == expected


def _flatten(steps):
    for s in steps:
        yield s
        yield from _flatten(s.get("workoutSteps") or [])


def test_multi_set_step_orders_are_unique_and_increasing():
    w = make_other_workout("x", None, work_s=7, rest_s=3, reps=6, sets=3)
    steps = w["workoutSegments"][0]["workoutSteps"]
    orders = [s["stepOrder"] for s in _flatten(steps)]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
    groups = [s for s in steps if s["type"] == "RepeatGroupDTO"]
    assert [g["childStepId"] for g in groups] == [1, 2, 3]
    for g in groups:
        assert {c["childStepId"] for c in g["workoutSteps"]} == {g["childStepId"]}


@pytest.mark.parametrize("bad", [{"reps": 0}, {"sets": 0}])
def test_rejects_nonpositive_counts(bad):
    kwargs = {"work_s": 7, "rest_s": 3, "reps": 6, **bad}
    with pytest.raises(ValueError):
        make_other_workout("x", None, **kwargs)


def test_kind_seam():
    assert BUILDERS["other"] is make_other_workout
```

- [ ] **Step 3: Run it to verify it fails**

Run the unit test command. Expected: `ModuleNotFoundError: No module named 'workout_gen'`.

- [ ] **Step 4: Implement** — `workout_gen.py`
```python
"""
Pure builder for Garmin "Other"-sport interval workouts (hangboard
repeaters and the like). No I/O and no garminconnect import: it returns
the JSON dict that garmin_write.upload() posts.

Every id and key below was copied from a workout hand-built in Garmin
Connect and reflected back via get_workout_by_id (the `finger` workout,
fixture tests/fixtures/finger_workout.json). Single-set output matches it
field-for-field minus server-assigned ids; tests/test_workout_gen.py pins
that.

sets > 1 is UNVERIFIED: it emits `sets` sibling repeat groups separated by
a timed rest step. No hand-built multi-set workout has been reflected yet
to confirm Garmin's shape (plan Task 0).

BUILDERS is the `kind` seam protocols.build() dispatches on. Only "other"
exists; tier-1 kinds (strength etc.) are reached through the `library`
plan source instead of a generator.
"""
from __future__ import annotations

from typing import Callable

SPORT_OTHER = {"sportTypeId": 3, "sportTypeKey": "other", "displayOrder": 13}

_STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},  # UNVERIFIED — only used when sets > 1
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}
_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_NO_STROKE = {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0}
_NO_EQUIPMENT = {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0}


def _executable(order: int, step_type: str, condition: dict, value: float, child_step_id: int | None) -> dict:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": dict(_STEP_TYPES[step_type]),
        "childStepId": child_step_id,
        "description": None,
        "endCondition": dict(condition),
        "endConditionValue": float(value),
        "preferredEndConditionUnit": None,
        "endConditionCompare": None,
        "targetType": dict(_NO_TARGET),
        "targetValueOne": None,
        "targetValueTwo": None,
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
        "category": None,
        "exerciseName": None,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": None,
        "weightUnit": None,
    }


def _repeat(order: int, child_step_id: int, reps: int, steps: list[dict]) -> dict:
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": dict(_STEP_TYPES["repeat"]),
        "childStepId": child_step_id,
        "numberOfIterations": reps,
        "workoutSteps": steps,
        "endConditionValue": float(reps),
        "preferredEndConditionUnit": None,
        "endConditionCompare": None,
        "endCondition": dict(_ITERATIONS),
        "skipLastRestStep": True,
        "smartRepeat": False,
    }


def _multi_set_steps(first_order: int, *, work_s, rest_s, reps, sets, set_rest_s) -> tuple[list[dict], int]:
    """Repeat groups (plus UNVERIFIED between-set rests). Returns (steps, last_order_used)."""
    steps: list[dict] = []
    order = first_order - 1
    for set_no in range(1, sets + 1):
        if set_no > 1:
            order += 1
            steps.append(_executable(order, "rest", _TIME, set_rest_s, None))
        order += 1
        group_order = order
        order += 1
        work = _executable(order, "interval", _TIME, work_s, set_no)
        order += 1
        recovery = _executable(order, "recovery", _TIME, rest_s, set_no)
        steps.append(_repeat(group_order, set_no, reps, [work, recovery]))
    return steps, order


def make_other_workout(
    name: str,
    description: str | None = None,
    *,
    work_s: float,
    rest_s: float,
    reps: int,
    sets: int = 1,
    set_rest_s: float = 180.0,
    warmup_s: float = 300.0,
    cooldown_s: float = 300.0,
) -> dict:
    if reps < 1 or sets < 1:
        raise ValueError(f"reps and sets must be >= 1 (got reps={reps}, sets={sets})")

    steps = [_executable(1, "warmup", _LAP_BUTTON, warmup_s, None)]
    body, last = _multi_set_steps(2, work_s=work_s, rest_s=rest_s, reps=reps, sets=sets, set_rest_s=set_rest_s)
    steps += body
    steps.append(_executable(last + 1, "cooldown", _LAP_BUTTON, cooldown_s, None))

    segment = {
        "segmentOrder": 1,
        "sportType": dict(SPORT_OTHER),
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
        "sportType": dict(SPORT_OTHER),
        "subSportType": None,
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


BUILDERS: dict[str, Callable[..., dict]] = {"other": make_other_workout}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run the unit test command. Expected: 5 passed. If the field-for-field test fails, read pytest's dict diff and fix the generator. **Never edit the fixture to match.**

- [ ] **Step 6: Commit**
```bash
git add tests/ workout_gen.py .gitignore
git commit -m "feat: add pure Other-sport workout generator verified against the finger workout"
```

---

### Task 2: `workouts` read-domain (Part 2 + Part 0a)

**Files:**
- Create: `schema/migrations/0008_workouts.sql`, `tests/test_domains_workouts.py`
- Modify: `domains.py` (new section before `# ---- registry`, plus a registry entry), `scripts/smoke_test.py` (new tests + PEP 723 deps)

**Interfaces:**
- Produces: `domains.parse_scheduled(calendar: dict) -> list[dict]`: workout calendar items only.
- Produces: `domains._fetch_scheduled(client, start: date, end: date) -> dict[int, dict]`: calendar item id → item, over every month from `start`'s month to `end`'s month, deduped.
- Produces: `domains.month_window(start: date, end: date) -> tuple[date, date]`: first day of `start`'s month, last day of `end`'s month.
- Produces: `domains.SCHEDULE_FETCH_SLEEP_S: float` (tests set it to 0), `domains.SCHEDULE_LOOKAHEAD_DAYS = 62`
- Produces tables: `workouts(workout_id bigint pk, workout_name text, updated_date text, raw jsonb, synced_at)` and `scheduled_workouts(schedule_id bigint pk, workout_id bigint, scheduled_date date, raw jsonb, synced_at)`

- [ ] **Step 1: Write the failing unit tests** — `tests/test_domains_workouts.py`
```python
import json
import pathlib
from datetime import date

import domains

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def test_parse_scheduled_keeps_only_workout_items():
    cal = json.loads((FIXTURES / "calendar_2026_09.json").read_text())
    items = domains.parse_scheduled(cal)
    assert [(i["workoutId"], i["date"]) for i in items] == [(1709094396, "2026-09-25")]


def test_parse_scheduled_tolerates_empty():
    assert domains.parse_scheduled({}) == []
    assert domains.parse_scheduled(None) == []


class _CalendarStub:
    def __init__(self):
        self.calls = []

    def get_scheduled_workouts(self, year, month):
        self.calls.append((year, month))
        # Same item returned by two adjacent months (grid overlap) -> must dedupe.
        return {"calendarItems": [
            {"id": 7, "itemType": "workout", "workoutId": 1, "date": "2026-12-30"},
            {"id": 8, "itemType": "weight", "date": "2026-12-30"},
        ]}


def test_fetch_scheduled_walks_months_across_year_end_and_dedupes(monkeypatch):
    monkeypatch.setattr(domains, "SCHEDULE_FETCH_SLEEP_S", 0)
    stub = _CalendarStub()
    items = domains._fetch_scheduled(stub, date(2026, 11, 15), date(2027, 1, 2))
    assert stub.calls == [(2026, 11), (2026, 12), (2027, 1)]
    assert list(items) == [7]


def test_month_window():
    assert domains.month_window(date(2026, 9, 30), date(2026, 11, 2)) == (date(2026, 9, 1), date(2026, 11, 30))
    assert domains.month_window(date(2026, 12, 5), date(2026, 12, 5)) == (date(2026, 12, 1), date(2026, 12, 31))


def test_workouts_domain_registered_off_by_default():
    d = domains.DOMAINS_BY_KEY["workouts"]
    assert d.default_enabled is False and d.backfill is None
    assert d.tables == ("workouts", "scheduled_workouts")
```

- [ ] **Step 2: Run to verify failure.** Expected: `AttributeError: module 'domains' has no attribute 'parse_scheduled'`.

- [ ] **Step 3: Migration** — `schema/migrations/0008_workouts.sql`
```sql
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
```

- [ ] **Step 4: Domain code.** In `domains.py`, add a `workouts` section immediately above `# --------------------------------------------------------------------- registry`:
```python
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
```
Add this entry to `DOMAINS`, after `profile`:
```python
    Domain(
        key="workouts", category="Workouts", label="Workout library & calendar",
        description="Your workout library (fully expanded steps + description) and workouts scheduled on your Garmin calendar. Read-only.",
        default_enabled=False, sync_incremental=_workouts_sync, backfill=None,
        tables=("workouts", "scheduled_workouts"),
    ),
```

- [ ] **Step 5: Run the unit tests.** Expected: all pass.

- [ ] **Step 6: Smoke tests (DB).** In `scripts/smoke_test.py`:
  - Add `"python-dotenv>=1.0"` to the PEP 723 dependency list (later tasks import scripts that call `load_dotenv`).
  - Add `import domains` next to the existing `from domains import DOMAINS, SyncContext`.
  - Add these functions and call them from `main()` after `test_disabled_domain_not_pulled()`:
```python
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
        for t in ("workouts", "scheduled_workouts"):
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
        assert _table_exists(conn, "workouts")
    print("test_workouts_domain_tables_and_join: PASS")


def test_workouts_sync_keeps_rows_on_empty_listing() -> None:
    with psycopg.connect(DB_URL) as conn:
        ctx = SyncContext(client=_WorkoutsStub([]), conn=conn, window_start=date.today(), today=date.today())
        domains._workouts_sync(ctx)
        assert conn.execute("select count(*) from workouts").fetchone()[0] == 1
    print("test_workouts_sync_keeps_rows_on_empty_listing: PASS")
```
  Run the smoke suite. Expected: all PASS.

- [ ] **Step 7: Commit**
```bash
git add schema/migrations/0008_workouts.sql domains.py tests/test_domains_workouts.py scripts/smoke_test.py
git commit -m "feat: add read-only workouts domain (library + scheduled calendar)"
```

---

### Task 3: Planning schema + approval fingerprint (Part 3)

**Files:**
- Create: `schema/migrations/0009_workout_planning.sql`, `planning.py`, `tests/test_planning.py`

**Interfaces:**
- Produces: `planning.protocol_hash(kind: str, name: str, description: str | None, params: dict) -> str` (sha256 hex of canonical JSON of all four; stored as the ledger's `params_hash`)
- Produces: `planning.PlanRow` (frozen dataclass: `id:int, scheduled_date:date, source:str, protocol_slug:str|None, workout_id:int|None, workout_name:str|None, skip:bool, status:str, schedule_id:int|None, resolved_workout_id:int|None, enacted_date:date|None`)
- Produces: `planning.fingerprint(row: PlanRow, proto_hash: str | None) -> str`
- Produces: `planning.PlanView` (frozen: `row: PlanRow, proto_hash: str|None, approved_fingerprint: str|None, protocol: dict|None = None, library_name: str|None = None`; properties `.fingerprint -> str`, `.approval -> "approved"|"stale"|"unapproved"`, `.describe() -> str`)
- Produces: `planning.load_plan(conn, *, since: date | None = None, ids: list[int] | None = None) -> list[PlanView]` (ordered by date, id)
- Produces: `planning.partition_for_enactment(views) -> tuple[list[PlanView], list[str]]`: (approved and not skipped, notes for stale rows)
- Produces: `planning.cancellations(views) -> list[PlanView]`: rows with `skip = true` that still hold an enacted entry, whatever their approval state

- [ ] **Step 1: Failing tests** — `tests/test_planning.py`
```python
from dataclasses import replace
from datetime import date

import pytest

from planning import PlanRow, PlanView, cancellations, fingerprint, partition_for_enactment, protocol_hash

ROW = PlanRow(id=1, scheduled_date=date(2026, 10, 2), source="generated", protocol_slug="finger",
              workout_id=None, workout_name=None, skip=False, status="planned", schedule_id=None,
              resolved_workout_id=None, enacted_date=None)
PROTO = {"kind": "other", "name": "Finger", "description": "d", "params": {"a": 1, "b": 2}}


def test_protocol_hash_is_key_order_independent():
    assert protocol_hash(**PROTO) == protocol_hash(**{**PROTO, "params": {"b": 2, "a": 1}})


@pytest.mark.parametrize("change", [{"kind": "x"}, {"name": "x"}, {"description": "x"}, {"params": {"a": 2}}])
def test_protocol_hash_covers_everything_seed_uploads(change):
    assert protocol_hash(**PROTO) != protocol_hash(**{**PROTO, **change})


@pytest.mark.parametrize("change", [
    {"scheduled_date": date(2026, 10, 3)}, {"source": "library"}, {"protocol_slug": "other"},
    {"workout_id": 5}, {"workout_name": "x"},
])
def test_fingerprint_changes_with_authored_fields(change):
    assert fingerprint(ROW, "h") != fingerprint(replace(ROW, **change), "h")


def test_fingerprint_ignores_enactor_and_status_fields():
    enacted = replace(ROW, status="scheduled", schedule_id=9, resolved_workout_id=8, enacted_date=date(2026, 10, 2))
    assert fingerprint(ROW, "h") == fingerprint(enacted, "h")


def test_fingerprint_ignores_skip():
    # Un-skipping restores already-approved content, so it needs no re-approval.
    assert fingerprint(ROW, "h") == fingerprint(replace(ROW, skip=True), "h")


def test_fingerprint_covers_protocol_for_generated_only():
    assert fingerprint(ROW, "h1") != fingerprint(ROW, "h2")
    lib = replace(ROW, source="library", protocol_slug=None, workout_name="Bear")
    assert fingerprint(lib, "h1") == fingerprint(lib, None)


def test_approval_states_and_partition():
    fresh = PlanView(ROW, "h", fingerprint(ROW, "h"))
    stale = PlanView(replace(ROW, id=2), "h2", fingerprint(replace(ROW, id=2), "h"))
    unapproved = PlanView(replace(ROW, id=3), "h", None)
    skipped = PlanView(replace(ROW, id=4, skip=True), "h", fingerprint(replace(ROW, id=4), "h"))
    assert [v.approval for v in (fresh, stale, unapproved)] == ["approved", "stale", "unapproved"]
    ready, notes = partition_for_enactment([fresh, stale, unapproved, skipped])
    assert [v.row.id for v in ready] == [1]
    assert len(notes) == 1 and "plan 2" in notes[0] and "re-approve" in notes[0]


def test_cancellations_ignore_approval_state():
    enacted = dict(skip=True, resolved_workout_id=8, enacted_date=date(2026, 10, 2))
    views = [
        PlanView(replace(ROW, id=1, **enacted), "h", None),                 # unapproved: still removed
        PlanView(replace(ROW, id=2, **enacted), "h", "old-fingerprint"),    # stale: still removed
        PlanView(replace(ROW, id=3, skip=True), "h", None),                 # never enacted: nothing to remove
        PlanView(replace(ROW, id=4, resolved_workout_id=8, enacted_date=date(2026, 10, 2)), "h", None),  # not skipped
    ]
    assert [v.row.id for v in cancellations(views)] == [1, 2]


def test_describe_shows_what_will_actually_be_scheduled():
    gen = PlanView(ROW, "h", None, protocol=PROTO)
    assert "Finger" in gen.describe() and '"a": 1' in gen.describe()
    lib = replace(ROW, source="library", protocol_slug=None, workout_id=555)
    assert "Bear Essentials" in PlanView(lib, None, None, library_name="Bear Essentials").describe()
    assert "not in synced library" in PlanView(replace(lib, workout_id=9), None, None).describe()
```

- [ ] **Step 2: Run to verify failure.** Expected: `ModuleNotFoundError: No module named 'planning'`.

- [ ] **Step 3: Migration** — `schema/migrations/0009_workout_planning.sql`
```sql
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
```

- [ ] **Step 4: Implement** — `planning.py`
```python
"""
Shared read model for the coach loop: plan rows, their approval state, and
the approval fingerprint. Used by plan_admin.py (owner, writes approvals),
seed_workouts.py and sync_schedule.py (scheduler, act on approvals).

The fingerprint is what makes approval a hard gate rather than a flag: it
hashes every coach-authored field of the row plus (generated rows) a hash
of everything seed will upload for the protocol. The coach role can UPDATE
training_plan and protocols but can't touch plan_approvals, so any edit
after approval makes the stored fingerprint stop matching and the row is
skipped until the owner re-approves. describe() shows the owner exactly
what would be enacted (protocol name + params, or the synced library name).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

import psycopg


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def protocol_hash(kind: str, name: str, description: str | None, params: dict) -> str:
    """Hash of everything seed uploads for a protocol. Stored in the ledger's
    params_hash column and folded into the approval fingerprint."""
    return _sha({"kind": kind, "name": name, "description": description, "params": params})


@dataclass(frozen=True)
class PlanRow:
    id: int
    scheduled_date: date
    source: str
    protocol_slug: str | None
    workout_id: int | None
    workout_name: str | None
    skip: bool
    status: str
    schedule_id: int | None
    resolved_workout_id: int | None
    enacted_date: date | None


def fingerprint(row: PlanRow, proto_hash: str | None) -> str:
    return _sha({
        "id": row.id,
        "date": row.scheduled_date.isoformat(),
        "source": row.source,
        "slug": row.protocol_slug,
        "workout_id": row.workout_id,
        "workout_name": row.workout_name,
        "protocol": proto_hash if row.source == "generated" else None,
    })


@dataclass(frozen=True)
class PlanView:
    row: PlanRow
    proto_hash: str | None
    approved_fingerprint: str | None
    protocol: dict | None = None  # {"kind", "name", "description", "params"} for generated rows
    library_name: str | None = None  # workouts.workout_name for library rows given by id

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.row, self.proto_hash)

    @property
    def approval(self) -> str:
        if self.approved_fingerprint is None:
            return "unapproved"
        return "approved" if self.approved_fingerprint == self.fingerprint else "stale"

    def describe(self) -> str:
        r = self.row
        if r.source == "generated":
            p = self.protocol or {}
            what = f"{r.protocol_slug} — {p.get('name')} {json.dumps(p.get('params'), sort_keys=True)}"
        elif r.workout_id is not None:
            what = f"workout {r.workout_id} ({self.library_name or 'not in synced library'})"
        else:
            what = f"{r.workout_name!r} (by name)"
        return f"{r.scheduled_date} {r.source}:{what}"


_SELECT = """
select tp.id, tp.scheduled_date, tp.source, tp.protocol_slug, tp.workout_id, tp.workout_name,
       tp.skip, tp.status, tp.schedule_id, tp.resolved_workout_id, tp.enacted_date,
       p.kind, p.name, p.description, p.params, w.workout_name, pa.fingerprint
  from training_plan tp
  left join protocols p on p.slug = tp.protocol_slug
  left join workouts w on w.workout_id = tp.workout_id
  left join plan_approvals pa on pa.plan_id = tp.id
"""


def load_plan(conn: psycopg.Connection, *, since: date | None = None, ids: list[int] | None = None) -> list[PlanView]:
    where, args = [], []
    if since is not None:
        where.append("tp.scheduled_date >= %s")
        args.append(since)
    if ids is not None:
        where.append("tp.id = any(%s::bigint[])")
        args.append(list(ids))
    query = _SELECT + (" where " + " and ".join(where) if where else "") + " order by tp.scheduled_date, tp.id"
    views = []
    for (*cols, kind, name, description, params, library_name, approved_fp) in conn.execute(query, args).fetchall():
        protocol = None
        if kind is not None:
            protocol = {"kind": kind, "name": name, "description": description, "params": params}
        views.append(PlanView(
            PlanRow(*cols),
            protocol_hash(**protocol) if protocol else None,
            approved_fp,
            protocol,
            library_name,
        ))
    return views


def partition_for_enactment(views: list[PlanView]) -> tuple[list[PlanView], list[str]]:
    ready, notes = [], []
    for v in views:
        if v.row.skip:
            continue
        if v.approval == "approved":
            ready.append(v)
        elif v.approval == "stale":
            notes.append(f"plan {v.row.id} ({v.describe()}): changed since approval — re-approve with plan_admin.py")
    return ready, notes


def cancellations(views: list[PlanView]) -> list[PlanView]:
    """Skipped rows that still hold an enacted calendar entry. Approval state
    is deliberately ignored: removing a workout is the safe direction, and a
    stale or revoked approval must not strand an entry on the calendar."""
    return [v for v in views if v.row.skip and v.row.resolved_workout_id is not None]
```

- [ ] **Step 5: Extend the Task 2 smoke test.** In `test_workouts_domain_tables_and_join`, change the table loop to cover the planning tables too, and assert that disabling keeps them:
```python
        for t in ("workouts", "scheduled_workouts", "training_plan", "protocols", "generated_workouts", "plan_approvals"):
```
```python
    with psycopg.connect(DB_URL) as conn:
        assert _table_exists(conn, "workouts") and _table_exists(conn, "training_plan")
```
Run the unit tests + the smoke suite. Expected: all PASS.

- [ ] **Step 6: Commit**
```bash
git add schema/migrations/0009_workout_planning.sql planning.py tests/test_planning.py scripts/smoke_test.py
git commit -m "feat: add coach-loop planning schema and approval fingerprint"
```

---

### Task 4: Protocol defaults (Part 4)

**Files:**
- Create: `protocols.py`, `tests/test_protocols.py`

**Interfaces:**
- Consumes: `workout_gen.BUILDERS`
- Produces: `protocols.DEFAULT_PROTOCOLS: dict[str, dict]` (slug → `{"name","description","kind","params"}`)
- Produces: `protocols.build(kind: str, name: str, description: str | None, params: dict) -> dict`
- Produces: `protocols.seed_default_protocols(conn) -> int` (rows inserted; 0 if the table already has any row; doesn't commit)

- [ ] **Step 1: Failing tests** — `tests/test_protocols.py`
```python
import pytest

from protocols import DEFAULT_PROTOCOLS, build
from workout_gen import make_other_workout


def test_every_default_builds():
    for slug, p in DEFAULT_PROTOCOLS.items():
        w = build(p["kind"], p["name"], p["description"], p["params"])
        assert w["workoutName"] == p["name"], slug


def test_build_dispatches_on_kind():
    p = DEFAULT_PROTOCOLS["finger-repeaters-7-3"]
    assert build("other", p["name"], p["description"], p["params"]) == make_other_workout(
        p["name"], p["description"], **p["params"])


def test_unknown_kind_is_a_clear_error():
    with pytest.raises(ValueError, match="strength"):
        build("strength", "x", None, {})


def test_defaults_are_single_set_until_multiset_verified():
    assert all(p["params"].get("sets", 1) == 1 for p in DEFAULT_PROTOCOLS.values())
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — `protocols.py`
```python
"""
Default workout protocols + the kind -> generator dispatch.

DEFAULT_PROTOCOLS only seeds an empty `protocols` table (at coach-loop
opt-in). After that the table is the source of truth and the coach edits
it through the MCP write tool; build() stays code, fed from table rows.
Defaults are single-set until multi-set nesting is verified (plan Task 0).
"""
from __future__ import annotations

import json

import psycopg

from workout_gen import BUILDERS

DEFAULT_PROTOCOLS: dict[str, dict] = {
    "finger-repeaters-7-3": {
        "name": "Finger repeaters 7/3",
        "description": "20mm edge, 7s on / 3s off x6, ~80% max",
        "kind": "other",
        "params": {"work_s": 7, "rest_s": 3, "reps": 6},
    },
    "finger-max-hangs-10": {
        "name": "Finger max hangs 10s",
        "description": "20mm edge, 10s hang / 3min rest x5, ~90% max",
        "kind": "other",
        "params": {"work_s": 10, "rest_s": 180, "reps": 5},
    },
}


def build(kind: str, name: str, description: str | None, params: dict) -> dict:
    builder = BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"No generator for protocol kind {kind!r} (known: {sorted(BUILDERS)}). "
                         "Schedule it as a `library` workout instead.")
    return builder(name, description, **params)


def seed_default_protocols(conn: psycopg.Connection) -> int:
    if conn.execute("select exists (select 1 from protocols)").fetchone()[0]:
        return 0
    for slug, p in DEFAULT_PROTOCOLS.items():
        conn.execute(
            "insert into protocols (slug, name, description, kind, params) values (%s, %s, %s, %s, %s)",
            (slug, p["name"], p["description"], p["kind"], json.dumps(p["params"])),
        )
    return len(DEFAULT_PROTOCOLS)
```

- [ ] **Step 4: Run the tests.** Expected: pass.

- [ ] **Step 5: Commit**
```bash
git add protocols.py tests/test_protocols.py
git commit -m "feat: add protocol defaults and kind dispatch"
```

---

### Task 5: `garmin_write.py`, the isolation test, and the probe move (Part 5)

**Files:**
- Create: `garmin_write.py`, `tests/test_write_isolation.py`, `tests/test_garmin_write.py`
- Move + modify: `probe_workout_link.py` → `scripts/probe_workout_link.py`

**Interfaces:**
- Produces: `garmin_write.find_first(obj, key) -> Any`
- Produces: `garmin_write.save_token_update_only(conn) -> None` (UPDATE-only; raises if there's no garmin row)
- Produces: `garmin_write.garmin_session(conn) -> ContextManager[Garmin]` (loads token, logs in, saves the token on exit)
- Produces: `garmin_write.upload(client, payload: dict) -> int`, `schedule(client, workout_id: int, day: date) -> int | None`, `unschedule(client, schedule_id: int) -> None`, `delete(client, workout_id: int) -> None`
- Produces: `garmin_write.confirmed(confirm_flag: bool, summary: str) -> bool`

- [ ] **Step 1: Failing tests**

`tests/test_write_isolation.py`:
```python
"""Invariant 1: Garmin write endpoints are referenced only in garmin_write.py,
and the ingestion path never imports the write side. The endpoints are
methods on the Garmin client, not importable names, so this scans the AST
for attribute accesses / string constants, not just imports."""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WRITE_ENDPOINTS = {"upload_workout", "schedule_workout", "unschedule_workout", "delete_workout"}
ALLOWED = {"garmin_write.py"}
SKIP_DIRS = {".venv", ".claude", ".git", "tests", "probe_out"}
INGESTION = ["domains.py", "upserts.py", "config.py", "backfill.py", "ingestion/pull.py", "apply_schema.py"]
WRITE_SIDE_MODULES = {"garmin_write", "seed_workouts", "sync_schedule", "plan_admin", "coach_roles"}


def _python_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in SKIP_DIRS:
            continue
        yield rel.as_posix(), path


def _endpoint_refs(tree) -> set[str]:
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in WRITE_ENDPOINTS:
            refs.add(node.attr)
        elif isinstance(node, ast.Constant) and node.value in WRITE_ENDPOINTS:
            refs.add(node.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            refs |= {a.name for a in node.names} & WRITE_ENDPOINTS
    return refs


def test_scan_actually_sees_garmin_write():
    tree = ast.parse((ROOT / "garmin_write.py").read_text())
    assert _endpoint_refs(tree) == WRITE_ENDPOINTS


def test_only_garmin_write_references_write_endpoints():
    offenders = {}
    for rel, path in _python_files():
        if rel in ALLOWED:
            continue
        refs = _endpoint_refs(ast.parse(path.read_text()))
        if refs:
            offenders[rel] = sorted(refs)
    assert offenders == {}


def test_ingestion_never_imports_write_side():
    for rel in INGESTION:
        tree = ast.parse((ROOT / rel).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & WRITE_SIDE_MODULES), f"{rel} imports {imported & WRITE_SIDE_MODULES}"
```

`tests/test_garmin_write.py`:
```python
from datetime import date

import pytest

import garmin_write


def test_find_first_recurses():
    assert garmin_write.find_first({"a": [{"b": {"workoutId": 5}}]}, "workoutId") == 5
    assert garmin_write.find_first({"workoutId": None, "x": {"workoutId": 7}}, "workoutId") == 7
    assert garmin_write.find_first({}, "workoutId") is None


class _Client:
    def __init__(self, upload_resp=None, schedule_resp=None):
        self.upload_resp, self.schedule_resp = upload_resp, schedule_resp
        self.calls = []

    def upload_workout(self, payload):
        self.calls.append(("upload", payload))
        return self.upload_resp

    def schedule_workout(self, workout_id, date_str):
        self.calls.append(("schedule", workout_id, date_str))
        return self.schedule_resp


def test_upload_parses_id_and_raises_without_one():
    assert garmin_write.upload(_Client(upload_resp={"workoutId": "123"}), {}) == 123
    with pytest.raises(RuntimeError, match="no workoutId"):
        garmin_write.upload(_Client(upload_resp={"oops": 1}), {})


def test_schedule_passes_iso_date_and_parses_id():
    c = _Client(schedule_resp={"workoutScheduleId": 99, "workout": {"workoutId": 1}})
    assert garmin_write.schedule(c, 1, date(2026, 10, 2)) == 99
    assert c.calls == [("schedule", 1, "2026-10-02")]
    assert garmin_write.schedule(_Client(schedule_resp={"id": 5}), 1, date(2026, 10, 2)) == 5
    assert garmin_write.schedule(_Client(schedule_resp=None), 1, date(2026, 10, 2)) is None


def test_confirmed_flag_wins():
    assert garmin_write.confirmed(True, "x") is True


def test_confirmed_without_tty_is_dry_run(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert garmin_write.confirmed(False, "x") is False


def test_confirmed_requires_typed_yes(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert garmin_write.confirmed(False, "x") is False
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert garmin_write.confirmed(False, "x") is True
```

- [ ] **Step 2: Run to verify failure.** Expected: `FileNotFoundError` / `ModuleNotFoundError` for `garmin_write`.

- [ ] **Step 3: Implement** — `garmin_write.py`
```python
"""
The ONLY module that calls Garmin write endpoints (upload_workout,
schedule_workout, unschedule_workout, delete_workout).
tests/test_write_isolation.py fails if any other file references them, and
if the ingestion path imports this module.

Callers (seed_workouts.py, sync_schedule.py) connect with
SCHEDULER_DATABASE_URL. The scheduler role has SELECT + column UPDATE on
auth_tokens but no INSERT, so the token is saved back with a plain UPDATE
(save_token_update_only) rather than bootstrap.garmin_auth.save_token_to_db's
upsert.

No idempotency here — that's the callers' job (ledger / (workout_id, date) diff).
"""
from __future__ import annotations

import contextlib
import json
import sys
from datetime import date
from typing import Any, Iterator

import psycopg
from garminconnect import Garmin

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db

# The complete set of Garmin write endpoints this project uses. Other
# modules that need the names (the probe's preflight) import this tuple
# rather than spelling them — the isolation test flags string literals too.
WRITE_ENDPOINTS = ("upload_workout", "schedule_workout", "unschedule_workout", "delete_workout")


def find_first(obj: Any, key: str) -> Any:
    """First non-empty value for `key` anywhere in a nested dict/list (depth-first)."""
    if isinstance(obj, dict):
        if obj.get(key) not in (None, ""):
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find_first(child, key)
        if found is not None:
            return found
    return None


def save_token_update_only(conn: psycopg.Connection) -> None:
    payload = {f.name: f.read_text() for f in TOKEN_DIR.glob("*.json")}
    cur = conn.execute(
        "update auth_tokens set payload = %s, updated_at = now() where provider = 'garmin'",
        (json.dumps(payload),),
    )
    if cur.rowcount != 1:
        raise RuntimeError("No garmin row in auth_tokens to update — run `uv run install.py --reauth` first.")
    conn.commit()


@contextlib.contextmanager
def garmin_session(conn: psycopg.Connection) -> Iterator[Garmin]:
    load_token_from_db(conn)
    client = Garmin()
    client.login(tokenstore=str(TOKEN_DIR))
    try:
        yield client
    finally:
        conn.rollback()  # a failed statement mid-run would otherwise block the token save
        save_token_update_only(conn)


def upload(client: Garmin, payload: dict) -> int:
    resp = client.upload_workout(payload)
    workout_id = find_first(resp, "workoutId")
    if workout_id is None:
        raise RuntimeError(f"upload_workout returned no workoutId: {str(resp)[:300]}")
    return int(workout_id)


def schedule(client: Garmin, workout_id: int, day: date) -> int | None:
    resp = client.schedule_workout(workout_id, day.isoformat())
    for key in ("workoutScheduleId", "scheduleId"):
        found = find_first(resp, key)
        if found is not None:
            return int(found)
    if isinstance(resp, dict) and resp.get("id") is not None:
        return int(resp["id"])
    return None


def unschedule(client: Garmin, schedule_id: int) -> None:
    client.unschedule_workout(schedule_id)


def delete(client: Garmin, workout_id: int) -> None:
    client.delete_workout(workout_id)


def confirmed(confirm_flag: bool, summary: str) -> bool:
    """--confirm, or a typed 'yes' at a terminal. Anything else is a dry run."""
    if confirm_flag:
        return True
    if not sys.stdin.isatty():
        print("Dry run — nothing written to Garmin. Re-run with --confirm to write.")
        return False
    answer = input(f"{summary}\nType 'yes' to write this to Garmin: ")
    return answer.strip().lower() == "yes"
```

- [ ] **Step 4: Move the probe and route its writes through `garmin_write`**
```bash
git mv probe_workout_link.py scripts/probe_workout_link.py
```
In `scripts/probe_workout_link.py`:
  - Change `sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))` to `…resolve().parent.parent`.
  - Change `OUT_DIR = pathlib.Path(__file__).resolve().parent / "probe_out"` to `…resolve().parent.parent / "probe_out"`.
  - Add `import garmin_write` after the `bootstrap.garmin_auth` import.
  - In `cmd_schedule`, replace
    ```python
            resp = client.upload_workout(payload)
            workout_id = _find_first(resp, "workoutId")
            if not workout_id:
                _dump(resp, OUT_DIR / "upload_response.json")
                sys.exit("Upload returned no workoutId — see probe_out/upload_response.json.")
    ```
    with `        workout_id = garmin_write.upload(client, payload)`, and replace
    ```python
            sched = client.schedule_workout(workout_id, when)
            schedule_id = _find_first(sched, "scheduleId") or _find_first(sched, "id")
    ```
    with `        schedule_id = garmin_write.schedule(client, workout_id, date.fromisoformat(when))`.
  - In `cmd_cleanup`, replace `client.delete_workout(X)` with `garmin_write.delete(client, X)` (two places), and `client.unschedule_workout(X)` with `garmin_write.unschedule(client, X)`.
  - Update the docstring's "Lives at repo root" line to say it lives in `scripts/` and does its writes through `garmin_write`.
  - The preflight lists spell endpoint names as strings, which the isolation test flags. Change them to
    ```python
    REQUIRED = ["get_workouts", "get_workout_by_id", "get_activities", *garmin_write.WRITE_ENDPOINTS[:2]]
    OPTIONAL = [*garmin_write.WRITE_ENDPOINTS[2:], "get_activity_exercise_sets", "get_scheduled_workouts"]
    ```
  - Keep the module's own `session()`. It connects as owner, which is fine for a manual probe.

- [ ] **Step 5: Run the unit tests.** Expected: all pass, including `test_only_garmin_write_references_write_endpoints` (so the probe is clean).

- [ ] **Step 6: Commit**
```bash
git add garmin_write.py tests/test_write_isolation.py tests/test_garmin_write.py scripts/probe_workout_link.py
git commit -m "feat: confine Garmin write endpoints to garmin_write.py with an AST isolation test"
```

---

### Task 6: Roles + GRANTs (`coach_roles.py`) + coach-loop config (Part 10, DB half)

**Files:**
- Create: `coach_roles.py`, `tests/test_coach_roles.py`
- Modify: `config.py` (append two functions), `scripts/smoke_test.py` (isolation tests)

**Interfaces:**
- Produces: `coach_roles.CoachLoopRoles(coach="coach", scheduler="scheduler", read=None)` with `.to_config() -> dict` and `CoachLoopRoles.from_config(cfg: dict)`
- Produces: `coach_roles.generate_password() -> str`, `role_exists(conn, role) -> bool`, `ensure_role(conn, role, password) -> bool` (True if created; always sets the password)
- Produces: `coach_roles.apply_coach_grants(conn, roles: CoachLoopRoles) -> None` (doesn't commit; re-runnable)
- Produces: `coach_roles.assert_role_isolated(conn, role) -> None` (raises RuntimeError on any membership or elevated attribute)
- Produces: `coach_roles.url_for_role(owner_url, role, password) -> str`, `role_of_url(url: str | None) -> str | None`
- Produces: `config.get_coach_loop(conn) -> dict | None`, `config.set_coach_loop(conn, value: dict) -> None` (commits)

**Grants this task implements** (the plan's authoritative version of the spec table, with deviations 1, 5 and 6 applied):

| Table | coach | scheduler | owner |
|---|---|---|---|
| every public table except the two below | SELECT | SELECT | all |
| `auth_tokens` | none | SELECT, UPDATE(payload, updated_at) | all |
| `training_plan` | SELECT, INSERT/UPDATE(scheduled_date, source, protocol_slug, workout_id, workout_name, skip, note), DELETE (un-enacted rows only, by trigger), USAGE on id sequence | SELECT, UPDATE(status, schedule_id, resolved_workout_id, enacted_date) | all |
| `protocols` | SELECT, INSERT, UPDATE, DELETE | SELECT | all |
| `generated_workouts` | SELECT | SELECT, INSERT, UPDATE | all |
| `plan_approvals` | none | SELECT | all (via plan_admin.py) |
| read role (if configured and exists) | SELECT on every table except `auth_tokens` | | |

- [ ] **Step 1: Failing unit tests** — `tests/test_coach_roles.py`
```python
from coach_roles import CoachLoopRoles, generate_password, role_of_url, url_for_role


def test_url_for_role_swaps_credentials_and_keeps_host_and_query():
    owner = "postgresql://owner:secret@ep-x.eu.aws.neon.tech/neondb?sslmode=require"
    url = url_for_role(owner, "coach", "p@ss/word")
    assert url == "postgresql://coach:p%40ss%2Fword@ep-x.eu.aws.neon.tech/neondb?sslmode=require"
    assert role_of_url(url) == "coach"


def test_role_of_url_handles_missing():
    assert role_of_url(None) is None
    assert role_of_url("postgresql://host/db") is None


def test_generated_passwords_are_long_and_distinct():
    a, b = generate_password(), generate_password()
    assert a != b and len(a) >= 40


def test_roles_config_round_trip():
    r = CoachLoopRoles(coach="c", scheduler="s", read="bk_reader")
    assert CoachLoopRoles.from_config(r.to_config()) == r
    assert CoachLoopRoles.from_config({}) == CoachLoopRoles()
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — `coach_roles.py`
```python
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
```

Append to `config.py`:
```python


def get_coach_loop(conn: psycopg.Connection) -> dict | None:
    """Coach-loop role names, present only after the explicit opt-in
    (install.py --coach-loop). None means a read-only install."""
    row = conn.execute("select value from sync_config where key = 'coach_loop'").fetchone()
    return row[0] if row else None


def set_coach_loop(conn: psycopg.Connection, value: dict) -> None:
    conn.execute(
        """
        insert into sync_config (key, value, updated_at)
        values ('coach_loop', %s, now())
            on conflict (key) do update set value = excluded.value, updated_at = now()
        """,
        (json.dumps(value),),
    )
    conn.commit()
```

- [ ] **Step 4: Run the unit tests.** Expected: pass.

- [ ] **Step 5: Smoke tests for acceptance criterion 1 + the token save.** In `scripts/smoke_test.py`, add these imports:
```python
import garmin_write
from coach_roles import CoachLoopRoles, apply_coach_grants, assert_role_isolated, ensure_role, url_for_role
from protocols import seed_default_protocols
```
Add the helpers and tests, and call both tests from `main()` after the workouts tests:
```python
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
```
**Careful:** `test_scheduler_token_save` writes whatever `*.json` sits in the local `~/.garminconnect` into the **smoke** DB only. That's fine, since it's never the production DB.

- [ ] **Step 6: Run the smoke suite.** Expected: all PASS.

- [ ] **Step 7: Commit**
```bash
git add coach_roles.py config.py tests/test_coach_roles.py scripts/smoke_test.py
git commit -m "feat: add coach/scheduler roles and re-runnable coach-loop grants"
```

---

### Task 7: Approval CLI — `plan_admin.py` (Part 8)

**Files:**
- Create: `plan_admin.py`

**Interfaces:**
- Consumes: `planning.load_plan`, `PlanView.fingerprint`, `PlanView.describe`
- Produces: `plan_admin.approve(conn, plan_ids: list[int], note: str | None) -> list[str]` (commits)
- Produces: `plan_admin.unapprove(conn, plan_ids: list[int]) -> int` (commits; rows removed)
- Produces: `plan_admin.ids_for_date(conn, day: date) -> list[int]`

- [ ] **Step 1: Failing smoke test.** Add `import planning` at the top of `scripts/smoke_test.py`, then this test, and call it from `main()` after the Task 6 tests:
```python
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
```

- [ ] **Step 2: Run to verify failure** (`ModuleNotFoundError: plan_admin`).

- [ ] **Step 3: Implement** — `plan_admin.py`
```python
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
    uv run plan_admin.py approve --id 12 --id 13 [--note "..."]
    uv run plan_admin.py approve --date 2026-10-02
    uv run plan_admin.py unapprove --id 12       # alias: reject

Approval records a fingerprint of exactly what you saw (planning.py). If
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


def approve(conn: psycopg.Connection, plan_ids: list[int], note: str | None) -> list[str]:
    views = {v.row.id: v for v in planning.load_plan(conn, ids=plan_ids)}
    missing = sorted(set(plan_ids) - set(views))
    if missing:
        raise SystemExit(f"No training_plan row(s) with id {missing}")
    lines = []
    for pid in plan_ids:
        v = views[pid]
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
            for line in approve(conn, ids, args.note):
                print(line)
        else:
            print(f"Removed {unapprove(conn, ids)} approval(s).")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the full smoke suite + unit tests.** Expected: every test PASS.

- [ ] **Step 5: Commit**
```bash
git add plan_admin.py scripts/smoke_test.py
git commit -m "feat: add owner-only approval CLI writing fingerprinted plan approvals"
```

---

### Task 8: Seed job — `seed_workouts.py` (Part 6)

**Files:**
- Create: `seed_workouts.py`
- Modify: `scripts/smoke_test.py` (a seed-only test; the full round trip is Task 9)

**Interfaces:**
- Consumes: `planning.load_plan`, `planning.partition_for_enactment`, `planning.protocol_hash`, `plan_admin.approve` (tests), `protocols.build`, `garmin_write.upload/delete/confirmed/garmin_session`
- Produces: `seed_workouts.SeedAction(slug: str, kind: "upload"|"reupload", params_hash: str, old_workout_id: int | None)`
- Produces: `seed_workouts.plan_seed(conn, today: date) -> tuple[list[SeedAction], list[str]]`
- Produces: `seed_workouts.execute_seed(conn, client, actions, *, prune_stale: bool) -> tuple[list[str], int]` (lines, error count)
- Produces: `seed_workouts.run(conn, open_client: Callable[[], ContextManager], *, today: date, confirm: Callable[[str], bool], prune_stale: bool = False) -> int` (exit code)
- Produces: `seed_workouts.POLITENESS_SLEEP_S = 1.0`

- [ ] **Step 1: Failing smoke test.** Add to `scripts/smoke_test.py` (with `import contextlib` and `import seed_workouts` at the top) and call it from `main()`:
```python
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
```
Run it: fails with `ModuleNotFoundError: seed_workouts`.

- [ ] **Step 2: Implement** — `seed_workouts.py`
```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Seed job: materialize `generated` protocols referenced by APPROVED plan
rows as real Garmin workouts. Manual only; runs as the scheduler role.

    uv run seed_workouts.py              # dry run: prints what would upload
    uv run seed_workouts.py --confirm    # uploads
    uv run seed_workouts.py --confirm --prune-stale

Per slug: not in generated_workouts -> build + upload + insert ledger;
protocol hash changed -> re-upload + repoint ledger (old workout left alone
unless --prune-stale and no activity references it); unchanged -> skip.
Library workouts are never touched. A row whose approval went stale (edited
after approval) is skipped until re-approved — see planning.py.

Env: SCHEDULER_DATABASE_URL (the Garmin token is read from auth_tokens through it).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, ContextManager

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import garmin_write
import planning
import protocols

POLITENESS_SLEEP_S = 1.0


@dataclass(frozen=True)
class SeedAction:
    slug: str
    kind: str  # "upload" | "reupload"
    params_hash: str  # planning.protocol_hash of what gets uploaded (ledger column name)
    old_workout_id: int | None = None


def plan_seed(conn: psycopg.Connection, today: date) -> tuple[list[SeedAction], list[str]]:
    ready, notes = planning.partition_for_enactment(planning.load_plan(conn, since=today))
    wanted = {v.row.protocol_slug: v.proto_hash for v in ready if v.row.source == "generated"}
    ledger = {slug: (wid, h) for slug, wid, h in
              conn.execute("select protocol_slug, workout_id, params_hash from generated_workouts").fetchall()}
    actions = []
    for slug, current in sorted(wanted.items()):
        entry = ledger.get(slug)
        if entry is None:
            actions.append(SeedAction(slug, "upload", current))
        elif entry[1] != current:
            actions.append(SeedAction(slug, "reupload", current, old_workout_id=entry[0]))
    return actions, notes


def _activity_references(conn: psycopg.Connection, workout_id: int) -> bool:
    if conn.execute("select to_regclass('activities')").fetchone()[0] is None:
        return True  # can't tell -> treat as referenced, never delete
    return conn.execute(
        "select exists (select 1 from activities where raw->>'workoutId' = %s)", (str(workout_id),)
    ).fetchone()[0]


def execute_seed(conn: psycopg.Connection, client, actions: list[SeedAction], *, prune_stale: bool) -> tuple[list[str], int]:
    lines, errors = [], 0
    for a in actions:
        try:
            name, description, kind, params = conn.execute(
                "select name, description, kind, params from protocols where slug = %s", (a.slug,)
            ).fetchone()
            if planning.protocol_hash(kind, name, description, params) != a.params_hash:
                raise RuntimeError("protocol changed while seeding — re-run")
            workout_id = garmin_write.upload(client, protocols.build(kind, name, description, params))
            conn.execute(
                """
                insert into generated_workouts (protocol_slug, workout_id, params_hash) values (%s, %s, %s)
                    on conflict (protocol_slug) do update set workout_id = excluded.workout_id, params_hash = excluded.params_hash
                """,
                (a.slug, workout_id, a.params_hash),
            )
            conn.commit()
            lines.append(f"{a.kind} {a.slug} -> workout {workout_id}")
            if a.kind == "reupload" and prune_stale:
                if _activity_references(conn, a.old_workout_id):
                    lines.append(f"  kept old workout {a.old_workout_id} (an activity references it)")
                else:
                    garmin_write.delete(client, a.old_workout_id)
                    lines.append(f"  deleted stale workout {a.old_workout_id}")
        except Exception as e:  # keep going; report at the end
            conn.rollback()
            errors += 1
            lines.append(f"{a.slug}: FAILED — {e}")
        time.sleep(POLITENESS_SLEEP_S)
    return lines, errors


def run(conn: psycopg.Connection, open_client: Callable[[], ContextManager], *, today: date,
        confirm: Callable[[str], bool], prune_stale: bool = False) -> int:
    actions, notes = plan_seed(conn, today)
    for n in notes:
        print(f"  note: {n}")
    if not actions:
        print("Seed: nothing to upload.")
        return 0
    print("Seed plan:")
    for a in actions:
        print(f"  {a.kind:9} {a.slug}" + (f" (replaces workout {a.old_workout_id})" if a.old_workout_id else ""))
    if not confirm(f"{len(actions)} workout(s) to upload."):
        return 0
    with open_client() as client:
        lines, errors = execute_seed(conn, client, actions, prune_stale=prune_stale)
    for line in lines:
        print(f"  {line}")
    print(f"Seed done: {len(actions) - errors} ok, {errors} failed.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="Actually upload (default: dry run).")
    parser.add_argument("--prune-stale", action="store_true", help="Delete a replaced workout if no activity references it.")
    args = parser.parse_args()
    load_dotenv()
    with psycopg.connect(os.environ["SCHEDULER_DATABASE_URL"]) as conn:
        sys.exit(run(conn, lambda: garmin_write.garmin_session(conn), today=date.today(),
                     confirm=lambda s: garmin_write.confirmed(args.confirm, s), prune_stale=args.prune_stale))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the unit tests** (isolation must still pass) **and the smoke suite.** Expected: PASS.

- [ ] **Step 4: Commit**
```bash
git add seed_workouts.py scripts/smoke_test.py
git commit -m "feat: add manual seed job materializing approved generated protocols"
```

---

### Task 9: Enactor — `sync_schedule.py` (Part 7)

**Files:**
- Create: `sync_schedule.py`
- Modify: `scripts/smoke_test.py`

**Interfaces:**
- Consumes: `planning.*`, `domains._fetch_scheduled`, `garmin_write.schedule/unschedule/confirmed/garmin_session`
- Produces: `sync_schedule.EnactAction(plan_id: int, day: date, workout_id: int, kind: "schedule"|"reschedule"|"record", old_schedule_id: int | None = None, live_schedule_id: int | None = None)`
- Produces: `sync_schedule.resolve_workout_id(conn, view, ledger: dict[str, tuple[int, str]]) -> tuple[int | None, str | None]`
- Produces: `sync_schedule.plan_enact(conn, views, live_items: dict[int, dict], today: date) -> tuple[list[EnactAction], list[str]]`
- Produces: `sync_schedule.execute_enact(conn, client, actions) -> tuple[list[str], int]`
- Produces: `sync_schedule.run(conn, open_client, *, today, confirm) -> int`, `POLITENESS_SLEEP_S = 1.0`

- [ ] **Step 1: Failing smoke test** (acceptance 2 and 4, plus Review Focus 1–3). Add `import sync_schedule` at the top of `scripts/smoke_test.py`, then this test, and call it from `main()`:
```python
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
```

- [ ] **Step 2: Run to verify failure.** Expected: `ModuleNotFoundError: sync_schedule`.

- [ ] **Step 3: Implement** — `sync_schedule.py`
```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Enactor: put APPROVED training_plan rows on the Garmin calendar. Manual
only; runs as the scheduler role. Run seed_workouts.py first.

    uv run sync_schedule.py              # dry run: prints what would be scheduled
    uv run sync_schedule.py --confirm    # schedules

Only rows with a plan_approvals row whose fingerprint still matches are
considered (status is advisory; the approval row is authoritative). Each is
resolved to a real workout id — generated -> the seed ledger; library ->
the row's workout_id, else a unique workouts.workout_name match — and
diffed against the LIVE Garmin calendar on (workout_id, date), so re-runs
and crash recovery never double-schedule. Each (workout_id, date) has one
owning row per run; other rows resolving to it are noted, never scheduled.

Rows the coach marked skip = true are unscheduled from Garmin whatever their
approval state (removal is the safe direction), then marked 'skipped'.

Env: SCHEDULER_DATABASE_URL.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, ContextManager

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import garmin_write
import planning
from domains import _fetch_scheduled

POLITENESS_SLEEP_S = 1.0


@dataclass(frozen=True)
class EnactAction:
    plan_id: int
    day: date
    workout_id: int
    kind: str  # "schedule" | "reschedule" | "record" | "unschedule"
    old_schedule_id: int | None = None
    live_schedule_id: int | None = None


def resolve_workout_id(conn: psycopg.Connection, view: planning.PlanView,
                       ledger: dict[str, tuple[int, str]]) -> tuple[int | None, str | None]:
    row = view.row
    if row.source == "generated":
        entry = ledger.get(row.protocol_slug)
        if entry is None:
            return None, "not seeded yet — run seed_workouts.py first"
        if entry[1] != view.proto_hash:
            return None, "seeded workout is out of date — run seed_workouts.py first"
        return entry[0], None
    if row.workout_id is not None:
        return row.workout_id, None
    ids = [r[0] for r in conn.execute(
        "select workout_id from workouts where workout_name = %s", (row.workout_name,)).fetchall()]
    if not ids:
        return None, f"no library workout named {row.workout_name!r} (has ingestion synced it yet?)"
    if len(ids) > 1:
        return None, f"{len(ids)} library workouts named {row.workout_name!r} — set workout_id instead"
    return ids[0], None


def plan_enact(conn: psycopg.Connection, views: list[planning.PlanView], live_items: dict[int, dict],
               today: date) -> tuple[list[EnactAction], list[str]]:
    ledger = {slug: (wid, h) for slug, wid, h in
              conn.execute("select protocol_slug, workout_id, params_hash from generated_workouts").fetchall()}
    live = {(int(i["workoutId"]), date.fromisoformat(i["date"])): sid for sid, i in live_items.items()}
    actions: list[EnactAction] = []
    for v in planning.cancellations(views):
        row = v.row
        if row.enacted_date < today:
            continue  # history; leave past calendar entries alone
        # None when it's already gone from Garmin (removed in Connect, or a crash
        # after unschedule): then it's a DB-only cleanup.
        actions.append(EnactAction(row.id, row.enacted_date, row.resolved_workout_id, "unschedule",
                                   old_schedule_id=live.get((row.resolved_workout_id, row.enacted_date))))

    ready, notes = planning.partition_for_enactment(views)
    resolved = []
    for v in ready:
        workout_id, why = resolve_workout_id(conn, v, ledger)
        if workout_id is None:
            notes.append(f"plan {v.row.id} ({v.describe()}): skipped — {why}")
        else:
            resolved.append((v, workout_id))

    def recorded(row, workout_id) -> bool:
        return (row.status == "scheduled" and row.resolved_workout_id == workout_id
                and row.enacted_date == row.scheduled_date)

    # One owner per (workout_id, date) per run: a row already recorded against
    # it wins, else the first by (date, id). Two rows can collide when one names
    # a library workout by id and another by name; the unique index can't see that.
    owner = {(wid, v.row.scheduled_date): v.row.id for v, wid in resolved if recorded(v.row, wid)}
    for v, workout_id in resolved:
        row = v.row
        target = (workout_id, row.scheduled_date)
        if owner.setdefault(target, row.id) != row.id:
            notes.append(f"plan {row.id} ({v.describe()}): skipped — same workout and day as plan {owner[target]}")
            continue
        if target in live:
            in_sync = recorded(row, workout_id)
            if not in_sync:
                actions.append(EnactAction(row.id, row.scheduled_date, workout_id, "record", live_schedule_id=live[target]))
            continue
        if row.scheduled_date < today:
            notes.append(f"plan {row.id} ({v.describe()}): in the past — not scheduling")
            continue
        old = None
        if row.resolved_workout_id is not None and row.enacted_date is not None:
            old = live.get((row.resolved_workout_id, row.enacted_date))
        actions.append(EnactAction(row.id, row.scheduled_date, workout_id,
                                   "reschedule" if old else "schedule", old_schedule_id=old))
    return actions, notes


def execute_enact(conn: psycopg.Connection, client, actions: list[EnactAction]) -> tuple[list[str], int]:
    lines, errors = [], 0
    for a in actions:
        try:
            schedule_id = a.live_schedule_id
            if a.kind == "unschedule":
                if a.old_schedule_id is not None:
                    garmin_write.unschedule(client, a.old_schedule_id)
                    time.sleep(POLITENESS_SLEEP_S)
                conn.execute(
                    "update training_plan set status = 'skipped', schedule_id = null, resolved_workout_id = null, enacted_date = null where id = %s",
                    (a.plan_id,),
                )
                conn.commit()
                lines.append(f"unschedule plan {a.plan_id}: workout {a.workout_id} on {a.day}")
                continue
            if a.kind == "reschedule":
                garmin_write.unschedule(client, a.old_schedule_id)
            if a.kind in ("schedule", "reschedule"):
                schedule_id = garmin_write.schedule(client, a.workout_id, a.day)
                time.sleep(POLITENESS_SLEEP_S)
            conn.execute(
                "update training_plan set status = 'scheduled', schedule_id = %s, resolved_workout_id = %s, enacted_date = %s where id = %s",
                (schedule_id, a.workout_id, a.day, a.plan_id),
            )
            conn.commit()
            lines.append(f"{a.kind} plan {a.plan_id}: workout {a.workout_id} on {a.day} (schedule {schedule_id})")
        except Exception as e:
            conn.rollback()
            errors += 1
            lines.append(f"plan {a.plan_id}: FAILED — {e}")
    return lines, errors


def run(conn: psycopg.Connection, open_client: Callable[[], ContextManager], *, today: date,
        confirm: Callable[[str], bool]) -> int:
    views = planning.load_plan(conn, since=today)
    ready, _ = planning.partition_for_enactment(views)
    cancels = [v for v in planning.cancellations(views) if v.row.enacted_date >= today]
    if not ready and not cancels:
        for n in planning.partition_for_enactment(views)[1]:
            print(f"  note: {n}")
        print("Enactor: no approved or cancelled plan rows from today on.")
        return 0
    end = max([v.row.scheduled_date for v in ready] + [v.row.enacted_date for v in cancels])
    with open_client() as client:
        live = _fetch_scheduled(client, today, end)
        actions, notes = plan_enact(conn, views, live, today)
        for n in notes:
            print(f"  note: {n}")
        if not actions:
            print("Enactor: calendar already matches the approved plan.")
            return 0
        print("Enactor plan:")
        for a in actions:
            print(f"  {a.kind:10} plan {a.plan_id}: workout {a.workout_id} on {a.day}")
        garmin_writes = sum(a.kind in ("schedule", "reschedule") or (a.kind == "unschedule" and a.old_schedule_id is not None)
                            for a in actions)
        if not confirm(f"{garmin_writes} Garmin calendar write(s), {len(actions) - garmin_writes} DB-only update(s)."):
            return 0
        lines, errors = execute_enact(conn, client, actions)
    for line in lines:
        print(f"  {line}")
    print(f"Enactor done: {len(actions) - errors} ok, {errors} failed.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="Actually schedule (default: dry run).")
    args = parser.parse_args()
    load_dotenv()
    with psycopg.connect(os.environ["SCHEDULER_DATABASE_URL"]) as conn:
        sys.exit(run(conn, lambda: garmin_write.garmin_session(conn), today=date.today(),
                     confirm=lambda s: garmin_write.confirmed(args.confirm, s)))


if __name__ == "__main__":
    main()
```
**Dependency note:** `sync_schedule` imports `domains`, which is fine (write side importing read side). The reverse direction is what the isolation test forbids.

- [ ] **Step 4: Run the unit tests + the smoke suite.** Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add sync_schedule.py scripts/smoke_test.py
git commit -m "feat: add manual enactor scheduling approved plan rows, diffed against the live calendar"
```

---

### Task 10: Manual enactor workflow (Part 9)

**Files:**
- Create: `.github/workflows/enact.yml`, `tests/test_workflows.py`

- [ ] **Step 1: Failing test** — `tests/test_workflows.py` (no YAML dep; a text check is enough for these invariants):
```python
import pathlib

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_enact_is_manual_only_and_dry_run_by_default():
    text = (WF / "enact.yml").read_text()
    assert "workflow_dispatch:" in text
    assert "schedule:" not in text and "cron" not in text
    assert "default: false" in text
    assert "SCHEDULER_DATABASE_URL" in text and "NEON_DATABASE_URL" not in text


def test_no_scheduled_workflow_runs_the_write_side():
    for wf in WF.glob("*.yml"):
        text = wf.read_text()
        if "cron" in text:
            for script in ("seed_workouts.py", "sync_schedule.py", "plan_admin.py"):
                assert script not in text, f"{wf.name} runs {script} on a schedule"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — `.github/workflows/enact.yml`
```yaml
name: garmin-enact

# MANUAL ONLY — never add a timed trigger. This is the one workflow that
# writes to Garmin (uploads generated workouts, schedules approved plan rows).
# It needs the SCHEDULER_DATABASE_URL secret, which is a standing
# Garmin-write-capable credential in CI; running `uv run seed_workouts.py`
# and `uv run sync_schedule.py --confirm` locally keeps it off shared infra.
on:
  workflow_dispatch:
    inputs:
      confirm:
        description: "Write to Garmin. Unchecked = dry run (prints what would be uploaded/scheduled)."
        type: boolean
        default: false

permissions:
  contents: read

concurrency:
  group: garmin-enact
  cancel-in-progress: false

jobs:
  enact:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    env:
      SCHEDULER_DATABASE_URL: ${{ secrets.SCHEDULER_DATABASE_URL }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r ingestion/requirements.txt

      - name: Seed generated workouts
        run: python seed_workouts.py ${{ inputs.confirm && '--confirm' || '' }}

      - name: Schedule approved plan rows
        run: python sync_schedule.py ${{ inputs.confirm && '--confirm' || '' }}
```

- [ ] **Step 4: Run the tests.** Expected: pass.

- [ ] **Step 5: Commit**
```bash
git add .github/workflows/enact.yml tests/test_workflows.py
git commit -m "feat: add manual-only garmin-enact workflow (dry run unless confirmed)"
```

---

### Task 11: `install.py` coach-loop opt-in (Part 10, UX half)

**Files:**
- Modify: `install.py`

**Interfaces:**
- Consumes: `coach_roles.*`, `config.get_coach_loop/set_coach_loop`, `protocols.seed_default_protocols`
- Produces: `install.run_coach_loop_optin(database_url: str, existing: dict[str, str]) -> None`, `install._offer_coach_loop() -> None`, and the CLI flag `--coach-loop`

- [ ] **Step 1: Imports.** In `install.py`, add:
```python
from coach_roles import (
    CoachLoopRoles, apply_coach_grants, assert_role_isolated, ensure_role,
    generate_password, role_exists, role_of_url, url_for_role,
)
from config import get_coach_loop, set_coach_loop
from protocols import seed_default_protocols
```
(merge `get_coach_loop, set_coach_loop` into the existing `from config import …` line).

- [ ] **Step 2: Add the opt-in** (place above `def main()`):
```python
COACH_LOOP_CONSENT = (
    "[bold]Coach loop — optional, off by default.[/bold]\n\n"
    "Turning this on [bold]grants a component write access to your Garmin account[/bold] "
    "(the enactor — seed_workouts.py / sync_schedule.py — can upload workouts to your "
    "library and put them on your calendar) and [bold]creates two scoped database users[/bold]:\n\n"
    "  • coach — for the MCP write tool. Can edit training_plan and protocols. "
    "Cannot read your Garmin token and cannot approve anything.\n"
    "  • scheduler — for the enactor. Can read your Garmin token and write the workout "
    "ledger. Acts only on plan rows you approve with plan_admin.py.\n\n"
    "Nothing is written to Garmin until you approve a plan row AND run the enactor by "
    "hand. A read-only install never needs this."
)


def run_coach_loop_optin(database_url: str, existing: dict[str, str]) -> None:
    console.print(Panel(COACH_LOOP_CONSENT, title="Coach loop", border_style="magenta"))
    if not questionary.confirm("Enable the coach loop?", default=False).ask():
        console.print("Coach loop not enabled.")
        return

    with psycopg.connect(database_url) as conn:
        enabled = get_enabled_domains(conn)
        previous = get_coach_loop(conn) or {}
    if "workouts" not in enabled:
        console.print("The coach loop needs the read-only [bold]workouts[/bold] domain — enabling it.")
        enabled = enabled | {"workouts"}
        apply_migrations(database_url, enabled_domains=enabled)
        with psycopg.connect(database_url) as conn:
            set_enabled_domains(conn, enabled)

    read_role = questionary.text(
        "Your MCP read-only DB role, so it can SELECT the new tables (blank to skip):",
        default=previous.get("read_role") or "",
    ).ask() or ""
    roles = CoachLoopRoles(read=read_role.strip() or None)

    env_updates: dict[str, str] = {}
    with psycopg.connect(database_url) as conn:
        for role, env_key in ((roles.coach, "COACH_DATABASE_URL"), (roles.scheduler, "SCHEDULER_DATABASE_URL")):
            if role_exists(conn, role) and role_of_url(existing.get(env_key)) == role:
                continue  # already set up and we hold its URL — don't rotate the password
            password = generate_password()
            ensure_role(conn, role, password)
            env_updates[env_key] = url_for_role(database_url, role, password)
        apply_coach_grants(conn, roles)
        inserted = seed_default_protocols(conn)
        conn.commit()
        for role in (roles.coach, roles.scheduler):
            assert_role_isolated(conn, role)
        set_coach_loop(conn, roles.to_config())

    console.print(f"[green]✓[/green] Roles + grants applied; {inserted} default protocol(s) seeded.")
    if env_updates:
        _write_env(env_updates)
        console.print(Panel(
            "Written to .env:\n  " + "\n  ".join(env_updates) + "\n\n"
            "• COACH_DATABASE_URL → set it on your MCP server (run_write_sql).\n"
            "• SCHEDULER_DATABASE_URL → used by seed_workouts.py / sync_schedule.py.\n"
            "• Approvals use your existing DATABASE_URL: uv run plan_admin.py list",
            title="Coach loop credentials", border_style="green",
        ))

    scheduler_url = env_updates.get("SCHEDULER_DATABASE_URL") or existing.get("SCHEDULER_DATABASE_URL")
    repo = _detect_github_repo()
    if scheduler_url and repo and _gh_available()[0] and questionary.confirm(
        "Also store SCHEDULER_DATABASE_URL as a GitHub Actions secret so the manual "
        "garmin-enact workflow can run? This places a standing Garmin-write-capable "
        "credential in CI (the local commands don't need it).",
        default=False,
    ).ask():
        _set_gh_secret(repo, "SCHEDULER_DATABASE_URL", scheduler_url)
        console.print("[green]✓[/green] Set SCHEDULER_DATABASE_URL secret.")


def _offer_coach_loop() -> None:
    existing = _read_existing_env()
    database_url = existing.get("DATABASE_URL")
    if not database_url:
        return
    with psycopg.connect(database_url) as conn:
        if get_coach_loop(conn):
            return
    run_coach_loop_optin(database_url, existing)
```

- [ ] **Step 3: Re-grant on upgrade.** In `run_upgrade`, directly after the `set_enabled_domains(conn, selected_domains)` block, add:
```python
    with psycopg.connect(database_url) as conn:
        coach_cfg = get_coach_loop(conn)
        if coach_cfg:
            apply_coach_grants(conn, CoachLoopRoles.from_config(coach_cfg))
            conn.commit()
            console.print("[green]✓[/green] Re-applied coach-loop grants (covers tables added by this upgrade).")
```

- [ ] **Step 4: Wire the CLI.** In `main()`, add the argument:
```python
    parser.add_argument(
        "--coach-loop", action="store_true",
        help="Opt in to the coach loop: creates the coach/scheduler DB roles and grants a component Garmin write access.",
    )
```
and replace the dispatch `try:` body with:
```python
        if args.reauth:
            run_reauth()
        elif args.coach_loop:
            existing = _read_existing_env()
            database_url = existing.get("DATABASE_URL") or _prompt_db_url(
                "Neon connection string (DATABASE_URL)", "DATABASE_URL", existing)
            _check_connection(database_url, "DATABASE_URL")
            run_coach_loop_optin(database_url, existing)
        elif args.upgrade:
            run_upgrade(domains_override=domains_override)
            _offer_coach_loop()
        else:
            run_setup(domains_override=domains_override)
            _offer_coach_loop()
```
Update the module docstring's usage block with a `uv run install.py --coach-loop` line: "opt in to the coach loop (creates two scoped DB roles; grants a component Garmin write access)".

- [ ] **Step 5: Smoke test acceptance 5** (a read-only install has no coach roles). Add to `scripts/smoke_test.py` and call it from `main()`. Roles are cluster-wide, so earlier smoke runs leave `smoke_*` roles behind; this test checks the install-level signal (no `coach_loop` config, no planning tables), which is what the opt-in gates:
```python
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
```
Also verify manually: `uv run install.py --help` shows `--coach-loop`, and `python -c "import ast; ast.parse(open('install.py').read())"` succeeds.

- [ ] **Step 6: Run the unit tests + the smoke suite.** Expected: PASS.

- [ ] **Step 7: Commit**
```bash
git add install.py scripts/smoke_test.py
git commit -m "feat: add explicit coach-loop opt-in to install.py and re-grant on upgrade"
```

---

### Task 12: Docs (Part 12)

**Files:**
- Modify: `README.md`, `docs/garmin_api_coverage.md`, `scripts/smoke_test.py` (docstring)

- [ ] **Step 1: README — add a "Coach loop (optional)" section** after the MCP/Claude section. It must contain:
  - The actor table. It's the spec's table, corrected: the Ingestion row says "owner `DATABASE_URL` (unchanged — no separate ingestion role)", and the MCP read row says "your MCP read role (e.g. `bk_garmin_data_mcp_reader`)".
  - The grants table from Task 6, verbatim.
  - Two sources: `generated` (a protocol the seed materializes) vs `library` (any existing workout of any type, by `workout_id` or unique `workout_name`, used as-is).
  - The ordering, as numbered steps:
    1. Ingestion syncs the library (enable the `workouts` domain).
    2. The coach writes `training_plan`/`protocols` through `run_write_sql`.
    3. The owner runs `uv run plan_admin.py list` then `approve`.
    4. `uv run seed_workouts.py --confirm`.
    5. `uv run sync_schedule.py --confirm`, or the Actions tab → garmin-enact → Run workflow with confirm checked.
  - "Approval is fingerprinted: editing an approved row or its protocol voids the approval (shows STALE)."
  - "To cancel a session, the coach sets `skip = true`. The next enact removes it from the Garmin calendar, with no re-approval needed. An enacted row can't be deleted until then. If two rows resolve to the same workout and day, only one is scheduled."
  - "Manual-only enactment; no cron writes to Garmin. The CI path needs the `SCHEDULER_DATABASE_URL` secret, a standing write-capable credential; the local path doesn't."
  - "Done" is derived, not stored:
    ```sql
    select tp.*, a.activity_id from training_plan tp
      join activities a on (a.raw->>'workoutId')::bigint = tp.resolved_workout_id
                       and (a.raw->>'startTimeLocal')::date = tp.enacted_date;
    ```
  - Opt-in: `uv run install.py --coach-loop`, with the consent text summarized.
  - The data-hygiene note: `Other`-typed workout activities carry junk distance/GPS and `exerciseSets: null`. Read execution from `splitSummaries` + HR zones + `activityTrainingLoad` and ignore distance/pace/GPS. Library workouts of proper types are self-describing.
  - The one unverified item: the `sets > 1` nesting (Task 0). Record as verified that the calendar item `id` equals `workoutScheduleId` (2026-09-30), and that `get_scheduled_workout_by_id` exposes `associatedActivityId`, Garmin's own planned→done link, as an alternative to the activity join.
- [ ] **Step 2: Coverage doc.** In `docs/garmin_api_coverage.md`:
  - Move `get_workouts`, `get_workout_by_id` and `get_scheduled_workouts` into the covered list under a new **workouts** domain entry, noting the 1-based month and the grid-overlap dedupe.
  - Replace the "Write operations (out of scope — read-only)" framing with: "Write operations: ingestion is read-only; the only writes are `upload_workout`, `schedule_workout`, `unschedule_workout` and `delete_workout`, confined to `garmin_write.py` and used only by the manual, approval-gated coach loop (see README). Everything else below remains unused."
- [ ] **Step 3:** Update `scripts/smoke_test.py`'s docstring. It now needs a throwaway Postgres (roles are cluster-wide, so never point it at the production Neon project), gives the Docker one-liner, and lists the new coverage.
- [ ] **Step 4:** Run the unit tests + the smoke suite one final time. Expected: all PASS.
- [ ] **Step 5: Commit**
```bash
git add README.md docs/garmin_api_coverage.md scripts/smoke_test.py
git commit -m "docs: document the coach loop, roles, approval gate, and Other-activity hygiene"
```

---

### Task 13: MCP write tool (Part 11) — SEPARATE REPO `~/Workspace/my-garmin-connect-data`

**Files (in that repo):**
- Modify: `analysis/db_tools.py`, `analysis/mcp_server.py`

Branch first: `git -C ~/Workspace/my-garmin-connect-data switch -c feat/run-write-sql`.

- [ ] **Step 1: Add `run_write_sql` to `analysis/db_tools.py`** (below `run_sql`):
```python
COACH_DB_URL = os.environ.get("COACH_DATABASE_URL")


def run_write_sql(query: str) -> dict:
    """Execute ONE SQL statement as the `coach` role and commit. No keyword
    filtering on purpose: the coach role's GRANTs (created by garmin-data's
    `install.py --coach-loop`) are the fence — it can write training_plan's
    authored columns and protocols, and cannot read auth_tokens or touch
    plan_approvals. Returns {"rowcount", "rows"} (rows only for RETURNING /
    SELECT, capped at MAX_ROWS)."""
    if not COACH_DB_URL:
        raise RuntimeError("COACH_DATABASE_URL is not set — the coach loop isn't enabled for this server.")
    with psycopg.connect(COACH_DB_URL) as conn:
        conn.execute(f"set statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
        cur = conn.execute(query)
        rows = []
        if cur.description is not None:
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(MAX_ROWS)]
        conn.commit()
        return {"rowcount": cur.rowcount, "rows": rows}
```
Update the module docstring's security model. Add a third bullet: "`run_write_sql` uses a SEPARATE connection (`COACH_DATABASE_URL`, the `coach` role). Its DB grants are the only fence: it deliberately doesn't filter keywords." Remove the sentence claiming everything here is read-only.

- [ ] **Step 2: Register the tool in `analysis/mcp_server.py`**. Import `run_write_sql as _run_write_sql` and add:
```python
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def run_write_sql(query: str) -> dict:
    """Run ONE SQL statement with the coach role and commit it. Use this to
    plan training: insert/update/delete rows in `training_plan` (columns
    scheduled_date, source 'generated'|'library', protocol_slug, workout_id
    OR workout_name (exactly one, for library rows), skip, note) and author `protocols`
    (slug, name, description, kind, params jsonb). You cannot approve plans
    or touch Garmin: the owner approves with plan_admin.py and a manual
    enactor schedules approved rows. Editing a row after approval voids the
    approval. To cancel a session, set skip = true (the enactor removes it
    from Garmin). Don't DELETE rows that are already on the calendar. Permission-denied errors mean the table/column is outside the
    coach role — don't retry."""
    return _run_write_sql(query)
```
Keep `run_sql` unchanged on the read connection. **Don't** add domain verbs (no `schedule_plan` etc.).

- [ ] **Step 3: Verify:** `cd ~/Workspace/my-garmin-connect-data && python -c "import ast,sys; [ast.parse(open(f).read()) for f in ('analysis/db_tools.py','analysis/mcp_server.py')]"`. Then, with `COACH_DATABASE_URL` pointing at the smoke DB's `smoke_coach` URL (`postgresql://smoke_coach:smoke-coach-pw@localhost:55432/postgres`) and `ANALYSIS_DATABASE_URL` set to anything valid:
```bash
uv run --with psycopg[binary] --with python-dotenv python -c "
from analysis.db_tools import run_write_sql
print(run_write_sql(\"insert into protocols (slug, name, params) values ('mcp-test','t','{}'::jsonb) returning slug\"))
try: run_write_sql('select * from auth_tokens')
except Exception as e: print('denied as expected:', type(e).__name__)"
```
Expected: `{'rowcount': 1, 'rows': [{'slug': 'mcp-test'}]}`, then `denied as expected: InsufficientPrivilege`.

- [ ] **Step 4: Commit (in that repo)**
```bash
git -C ~/Workspace/my-garmin-connect-data add analysis/db_tools.py analysis/mcp_server.py
git -C ~/Workspace/my-garmin-connect-data commit -m "feat: add generic run_write_sql tool on a separate coach-role connection"
```
Deploying it (setting `COACH_DATABASE_URL` on the hosted server) is the user's call. Don't push or deploy without asking.

---

### Task 14: Live acceptance — GATED, ask the user before each step

Nothing here runs without explicit approval. Each step touches the real Neon DB or the real Garmin account.

- [ ] **Step 1 (migrations on Neon):** Ask, then run `uv run install.py --upgrade --domains <current>,workouts`. Verify with `uv run ingestion/pull.py` (read-only): `workouts` has rows, and `scheduled_workouts` contains the `finger` item on 2026-09-25. Run `pull.py` a second time and confirm it doesn't re-expand every workout (the `updateDate` change marker works; add a temporary count print if needed). Then verify spec Part 2's join through the read MCP: `select a.activity_id, w.raw->>'description' from activities a join workouts w on w.workout_id = (a.raw->>'workoutId')::bigint where a.activity_id = 24494258276` returns "20mm edge, 7s/3s repeaters ×6, ~80% max".
- [ ] **Step 2 (role DDL on Neon):** Ask, then run `uv run install.py --coach-loop` (read role: `bk_garmin_data_mcp_reader`). `assert_role_isolated` runs inside it. That's the Neon-specific membership check (e.g. no `neon_superuser`).
- [ ] **Step 3 (acceptance 1 on Neon, read-only probes):**
  ```bash
  psql "$COACH_DATABASE_URL" -c 'select * from auth_tokens'                                            # expect: permission denied
  psql "$COACH_DATABASE_URL" -c "insert into plan_approvals (plan_id, fingerprint) values (0,'x')"    # expect: permission denied
  psql "$SCHEDULER_DATABASE_URL" -c 'select count(*) from auth_tokens'                                # expect: 1
  psql "$SCHEDULER_DATABASE_URL" -c 'select count(*) from plan_approvals'                             # expect: 0
  ```
- [ ] **Step 4 (first real write):** Ask. Through `COACH_DATABASE_URL`, insert one `generated` row for tomorrow (`finger-repeaters-7-3`), then run `uv run plan_admin.py approve --id N`, then `uv run seed_workouts.py` (dry run) and `--confirm`, then `uv run sync_schedule.py` (dry run) and `--confirm`.
- [ ] **Step 5 (id cross-check):** The calendar-id = `workoutScheduleId` equality was verified read-only on 2026-09-30. Confirm the enactor stored the same value: `select schedule_id from training_plan where id = N` equals `select schedule_id from scheduled_workouts where workout_id = … and scheduled_date = …` after the next `uv run ingestion/pull.py`.
- [ ] **Step 6:** Re-run both enactor commands with `--confirm` and confirm they report "nothing to upload" and "calendar already matches". Then clean up the test row: `uv run plan_admin.py unapprove --id N`, unschedule in Connect or keep it, and delete the row through coach.
- [ ] **Step 7:** Ask whether to open a PR (branch → `main`), using the attribution lines from the session instructions.
