"""
Shared read model for the coach loop: plan rows, their approval state, and
the approval fingerprint. Used by plan_admin.py (owner, writes approvals),
seed_workouts.py and sync_schedule.py (scheduler, act on approvals).

The fingerprint is what makes approval a hard gate rather than a flag: it
hashes every coach-authored field of the row plus (generated rows) a hash
of everything seed will upload for the protocol. The coach role can UPDATE
training_plan and protocols but can't touch plan_approvals, so any edit
after approval makes the stored fingerprint stop matching and the row is
skipped until the owner re-approves. describe() shows the owner exactly
what would be enacted (protocol name + params, or the synced library name).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

import psycopg


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def protocol_hash(kind: str, name: str, description: str | None, params: dict) -> str:
    """Hash of everything seed uploads for a protocol. Stored in the ledger's
    params_hash column and folded into the approval fingerprint."""
    return _sha({"kind": kind, "name": name, "description": description, "params": params})


@dataclass(frozen=True)
class PlanRow:
    id: int
    scheduled_date: date
    source: str
    protocol_slug: str | None
    workout_id: int | None
    workout_name: str | None
    skip: bool
    status: str
    schedule_id: int | None
    resolved_workout_id: int | None
    enacted_date: date | None


def fingerprint(row: PlanRow, proto_hash: str | None) -> str:
    return _sha({
        "id": row.id,
        "date": row.scheduled_date.isoformat(),
        "source": row.source,
        "slug": row.protocol_slug,
        "workout_id": row.workout_id,
        "workout_name": row.workout_name,
        "protocol": proto_hash if row.source == "generated" else None,
    })


@dataclass(frozen=True)
class PlanView:
    row: PlanRow
    proto_hash: str | None
    approved_fingerprint: str | None
    protocol: dict | None = None  # {"kind", "name", "description", "params"} for generated rows
    library_name: str | None = None  # workouts.workout_name for library rows given by id

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.row, self.proto_hash)

    @property
    def approval(self) -> str:
        if self.approved_fingerprint is None:
            return "unapproved"
        return "approved" if self.approved_fingerprint == self.fingerprint else "stale"

    def describe(self) -> str:
        r = self.row
        if r.source == "generated":
            p = self.protocol or {}
            what = f"{r.protocol_slug} — {p.get('name')} {json.dumps(p.get('params'), sort_keys=True)}"
        elif r.workout_id is not None:
            what = f"workout {r.workout_id} ({self.library_name or 'not in synced library'})"
        else:
            what = f"{r.workout_name!r} (by name)"
        return f"{r.scheduled_date} {r.source}:{what}"


_SELECT = """
select tp.id, tp.scheduled_date, tp.source, tp.protocol_slug, tp.workout_id, tp.workout_name,
       tp.skip, tp.status, tp.schedule_id, tp.resolved_workout_id, tp.enacted_date,
       p.kind, p.name, p.description, p.params, w.workout_name, pa.fingerprint
  from training_plan tp
  left join protocols p on p.slug = tp.protocol_slug
  left join workouts w on w.workout_id = tp.workout_id
  left join plan_approvals pa on pa.plan_id = tp.id
"""


def load_plan(conn: psycopg.Connection, *, since: date | None = None, ids: list[int] | None = None) -> list[PlanView]:
    where, args = [], []
    if since is not None:
        # Also rows whose calendar entry is still ahead even though the coach
        # moved the row into the past, so a later skip can still remove it.
        where.append("(tp.scheduled_date >= %s or tp.enacted_date >= %s)")
        args += [since, since]
    if ids is not None:
        where.append("tp.id = any(%s::bigint[])")
        args.append(list(ids))
    query = _SELECT + (" where " + " and ".join(where) if where else "") + " order by tp.scheduled_date, tp.id"
    views = []
    for (*cols, kind, name, description, params, library_name, approved_fp) in conn.execute(query, args).fetchall():
        protocol = None
        if kind is not None:
            protocol = {"kind": kind, "name": name, "description": description, "params": params}
        views.append(PlanView(
            PlanRow(*cols),
            protocol_hash(**protocol) if protocol else None,
            approved_fp,
            protocol,
            library_name,
        ))
    return views


def partition_for_enactment(views: list[PlanView]) -> tuple[list[PlanView], list[str]]:
    ready, notes = [], []
    for v in views:
        if v.row.skip:
            continue
        if v.approval == "approved":
            ready.append(v)
        elif v.approval == "stale":
            notes.append(f"plan {v.row.id} ({v.describe()}): changed since approval — re-approve with plan_admin.py")
    return ready, notes


def cancellations(views: list[PlanView]) -> list[PlanView]:
    """Skipped rows that still hold an enacted calendar entry. Approval state
    is deliberately ignored: removing a workout is the safe direction, and a
    stale or revoked approval must not strand an entry on the calendar."""
    return [v for v in views if v.row.skip and v.row.resolved_workout_id is not None]
