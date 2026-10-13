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
