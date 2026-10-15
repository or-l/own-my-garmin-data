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
                          "reps": int(ex["endConditionValue"]), "rest_s": rest["endConditionValue"],
                          **({"weight_kg": ex["weightValue"]} if ex["weightValue"] is not None else {})})
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
