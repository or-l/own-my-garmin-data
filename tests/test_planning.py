from dataclasses import replace
from datetime import date

import pytest

from planning import PlanRow, PlanView, cancellations, fingerprint, partition_for_enactment, protocol_hash

ROW = PlanRow(id=1, scheduled_date=date(2026, 10, 2), source="generated", protocol_slug="finger",
              workout_id=None, workout_name=None, skip=False, status="planned", schedule_id=None,
              resolved_workout_id=None, enacted_date=None)
PROTO = {"kind": "other", "name": "Finger", "description": "d", "params": {"a": 1, "b": 2}}


def test_protocol_hash_is_key_order_independent():
    assert protocol_hash(**PROTO) == protocol_hash(**{**PROTO, "params": {"b": 2, "a": 1}})


@pytest.mark.parametrize("change", [{"kind": "x"}, {"name": "x"}, {"description": "x"}, {"params": {"a": 2}}])
def test_protocol_hash_covers_everything_seed_uploads(change):
    assert protocol_hash(**PROTO) != protocol_hash(**{**PROTO, **change})


@pytest.mark.parametrize("change", [
    {"scheduled_date": date(2026, 10, 3)}, {"source": "library"}, {"protocol_slug": "other"},
    {"workout_id": 5}, {"workout_name": "x"},
])
def test_fingerprint_changes_with_authored_fields(change):
    assert fingerprint(ROW, "h") != fingerprint(replace(ROW, **change), "h")


def test_fingerprint_ignores_enactor_and_status_fields():
    enacted = replace(ROW, status="scheduled", schedule_id=9, resolved_workout_id=8, enacted_date=date(2026, 10, 2))
    assert fingerprint(ROW, "h") == fingerprint(enacted, "h")


def test_fingerprint_ignores_skip():
    # Un-skipping restores already-approved content, so it needs no re-approval.
    assert fingerprint(ROW, "h") == fingerprint(replace(ROW, skip=True), "h")


def test_fingerprint_covers_protocol_for_generated_only():
    assert fingerprint(ROW, "h1") != fingerprint(ROW, "h2")
    lib = replace(ROW, source="library", protocol_slug=None, workout_name="Bear")
    assert fingerprint(lib, "h1") == fingerprint(lib, None)


def test_approval_states_and_partition():
    fresh = PlanView(ROW, "h", fingerprint(ROW, "h"))
    stale = PlanView(replace(ROW, id=2), "h2", fingerprint(replace(ROW, id=2), "h"))
    unapproved = PlanView(replace(ROW, id=3), "h", None)
    skipped = PlanView(replace(ROW, id=4, skip=True), "h", fingerprint(replace(ROW, id=4), "h"))
    assert [v.approval for v in (fresh, stale, unapproved)] == ["approved", "stale", "unapproved"]
    ready, notes = partition_for_enactment([fresh, stale, unapproved, skipped])
    assert [v.row.id for v in ready] == [1]
    assert len(notes) == 1 and "plan 2" in notes[0] and "re-approve" in notes[0]


def test_cancellations_ignore_approval_state():
    enacted = dict(skip=True, resolved_workout_id=8, enacted_date=date(2026, 10, 2))
    views = [
        PlanView(replace(ROW, id=1, **enacted), "h", None),                 # unapproved: still removed
        PlanView(replace(ROW, id=2, **enacted), "h", "old-fingerprint"),    # stale: still removed
        PlanView(replace(ROW, id=3, skip=True), "h", None),                 # never enacted: nothing to remove
        PlanView(replace(ROW, id=4, resolved_workout_id=8, enacted_date=date(2026, 10, 2)), "h", None),  # not skipped
    ]
    assert [v.row.id for v in cancellations(views)] == [1, 2]


def test_describe_shows_what_will_actually_be_scheduled():
    gen = PlanView(ROW, "h", None, protocol=PROTO)
    assert "Finger" in gen.describe() and '"a": 1' in gen.describe()
    lib = replace(ROW, source="library", protocol_slug=None, workout_id=555)
    assert "Bear Essentials" in PlanView(lib, None, None, library_name="Bear Essentials").describe()
    assert "not in synced library" in PlanView(replace(lib, workout_id=9), None, None).describe()
