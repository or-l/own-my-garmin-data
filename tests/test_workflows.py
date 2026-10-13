import pathlib

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_enact_is_manual_only_and_dry_run_by_default():
    text = (WF / "enact.yml").read_text()
    assert "workflow_dispatch:" in text
    assert "schedule:" not in text and "cron" not in text
    assert "default: false" in text
    assert "SCHEDULER_DATABASE_URL" in text and "NEON_DATABASE_URL" not in text


def test_no_scheduled_workflow_runs_the_write_side():
    for wf in WF.glob("*.yml"):
        text = wf.read_text()
        if "cron" in text:
            for script in ("seed_workouts.py", "sync_schedule.py", "plan_admin.py"):
                assert script not in text, f"{wf.name} runs {script} on a schedule"
