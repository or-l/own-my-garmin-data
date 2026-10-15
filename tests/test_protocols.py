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
