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
    assert d.tables == ("workouts", "scheduled_workouts", "exercise_catalog", "activity_exercise_sets")
