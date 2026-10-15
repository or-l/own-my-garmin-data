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
