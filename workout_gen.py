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
