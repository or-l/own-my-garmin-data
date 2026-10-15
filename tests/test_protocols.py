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
    with pytest.raises(ValueError, match="running"):
        build("running", "x", None, {})


def test_defaults_are_single_set_until_multiset_verified():
    assert all(p["params"].get("sets", 1) == 1 for p in DEFAULT_PROTOCOLS.values())


def test_strength_builds_through_dispatch():
    params = {"exercises": [{"category": "PLANK", "exercise": "PLANK", "sets": 3, "time_s": 45}]}
    w = build("strength", "Core", "plank day", params)
    assert w["sportType"]["sportTypeKey"] == "strength_training" and w["workoutName"] == "Core"


def test_protocol_problems_never_raises_on_absurd_numbers():
    # 10**400 is valid jsonb and loads as a Python int; float() of it overflows.
    from protocols import protocol_problems
    params = {"exercises": [{"category": "PULL_UP", "exercise": "PULL_UP", "sets": 10**400, "reps": 5}]}
    problems = protocol_problems(None, "strength", "x", None, params)
    assert problems and isinstance(problems[0], str)
