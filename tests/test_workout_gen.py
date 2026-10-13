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
