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

BUILDERS is the `kind` seam protocols.build() dispatches on: "other"
(interval workouts) and "strength" (see the strength section below).
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
