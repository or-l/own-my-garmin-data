from datetime import date

from planning import PlanRow, PlanView, fingerprint
import plan_admin

TODAY = date(2026, 10, 15)


def _view(id, day, *, source="generated", slug="s", workout_id=None, workout_name=None, skip=False,
          approved=None, protocol=None, library_name=None):
    row = PlanRow(id=id, scheduled_date=day, source=source, protocol_slug=slug if source == "generated" else None,
                  workout_id=workout_id, workout_name=workout_name, skip=skip, status="planned",
                  schedule_id=None, resolved_workout_id=None, enacted_date=None)
    fp = {"fresh": fingerprint(row, "h"), "stale": "old"}.get(approved)
    return PlanView(row, "h", fp, protocol, library_name)


STRENGTH = {"kind": "strength", "name": "Strength: pull/push basic", "description": None, "params": {"exercises": [
    {"category": "PULL_UP", "exercise": "PULL_UP", "sets": 4, "reps": 5},
    {"category": "BENCH_PRESS", "exercise": "DUMBBELL_BENCH_PRESS", "sets": 3, "reps": 8, "weight_kg": 10},
    {"category": "PLANK", "exercise": "PLANK", "sets": 2, "time_s": 45}]}}
HANG = {"kind": "other", "name": "Finger max hangs 10s", "description": None,
        "params": {"work_s": 10, "rest_s": 180, "reps": 5}}


def test_pending_rows_are_upcoming_unapproved_or_stale_and_not_skipped():
    views = [
        _view(1, date(2026, 10, 14)),                       # past
        _view(2, TODAY, approved="fresh"),                  # already approved
        _view(3, TODAY, approved="stale"),                  # stale -> shown
        _view(4, date(2026, 10, 16)),                       # unapproved -> shown
        _view(5, date(2026, 10, 17), skip=True),            # cancelled
    ]
    assert [v.row.id for v in plan_admin.pending_rows(views, TODAY)] == [3, 4]


def test_labels_are_short_and_readable():
    strength = plan_admin.label(_view(6, date(2026, 10, 18), protocol=STRENGTH))
    assert strength.startswith("  6  Sun 18 Oct  Strength: pull/push basic — 3 exercises")
    assert "PULL_UP 4×5" in strength and "DUMBBELL_BENCH_PRESS 3×8 @10kg" in strength and "{" not in strength
    hang = plan_admin.label(_view(5, date(2026, 10, 16), protocol=HANG, approved="stale"))
    assert "STALE" in hang and "10s on / 180s off ×5" in hang
    lib = plan_admin.label(_view(4, TODAY, source="library", workout_id=1231158443, library_name="Bear Essentials"))
    assert "Bear Essentials (library)" in lib


class _Runner:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return type("R", (), {"returncode": self.codes.pop(0)})()


def test_publish_runs_seed_then_enact(monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATABASE_URL", "postgresql://scheduler@x/db")
    run = _Runner([0, 0])
    assert plan_admin.publish(run=run) == 0
    assert [c[-1].rsplit("/", 1)[-1] for c in run.calls] == ["seed_workouts.py", "sync_schedule.py"]
    assert all(c[:2] == ["uv", "run"] and "--confirm" not in c for c in run.calls), "each script must show its dry run and ask"


def test_publish_stops_if_seed_fails(monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATABASE_URL", "postgresql://scheduler@x/db")
    run = _Runner([1])
    assert plan_admin.publish(run=run) == 1
    assert len(run.calls) == 1, "don't schedule after a failed upload"


def test_publish_needs_the_scheduler_login(monkeypatch):
    monkeypatch.delenv("SCHEDULER_DATABASE_URL", raising=False)
    run = _Runner([])
    assert plan_admin.publish(run=run) == 1 and run.calls == []
