"""
Default workout protocols + the kind -> generator dispatch.

DEFAULT_PROTOCOLS only seeds an empty `protocols` table (at coach-loop
opt-in). After that the table is the source of truth and the coach edits
it through the MCP write tool; build() stays code, fed from table rows.
Defaults are single-set until multi-set nesting is verified (plan Task 0).
"""
from __future__ import annotations

import json

import psycopg

from workout_gen import BUILDERS

DEFAULT_PROTOCOLS: dict[str, dict] = {
    "finger-repeaters-7-3": {
        "name": "Finger repeaters 7/3",
        "description": "20mm edge, 7s on / 3s off x6, ~80% max",
        "kind": "other",
        "params": {"work_s": 7, "rest_s": 3, "reps": 6},
    },
    "finger-max-hangs-10": {
        "name": "Finger max hangs 10s",
        "description": "20mm edge, 10s hang / 3min rest x5, ~90% max",
        "kind": "other",
        "params": {"work_s": 10, "rest_s": 180, "reps": 5},
    },
}


def build(kind: str, name: str, description: str | None, params: dict) -> dict:
    builder = BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"No generator for protocol kind {kind!r} (known: {sorted(BUILDERS)}). "
                         "Schedule it as a `library` workout instead.")
    return builder(name, description, **params)


def seed_default_protocols(conn: psycopg.Connection) -> int:
    if conn.execute("select exists (select 1 from protocols)").fetchone()[0]:
        return 0
    for slug, p in DEFAULT_PROTOCOLS.items():
        conn.execute(
            "insert into protocols (slug, name, description, kind, params) values (%s, %s, %s, %s, %s)",
            (slug, p["name"], p["description"], p["kind"], json.dumps(p["params"])),
        )
    return len(DEFAULT_PROTOCOLS)


def strength_problems(conn: psycopg.Connection, params: dict) -> list[str]:
    """(category, exercise) pairs Garmin won't accept, checked against the
    synced exercise_catalog. An empty catalogue refuses everything rather
    than letting an unvalidated payload reach Garmin."""
    pairs = [(e.get("category"), e.get("exercise")) for e in (params or {}).get("exercises") or [] if isinstance(e, dict)]
    if not pairs:
        return []
    if not conn.execute("select exists (select 1 from exercise_catalog)").fetchone()[0]:
        return ["exercise catalogue not synced yet — run a pull with the workouts domain enabled"]
    known = set(conn.execute(
        "select category, exercise from exercise_catalog where category = any(%s)",
        (sorted({c for c, _ in pairs if c}),),
    ).fetchall())
    return [f"{c}/{e} is not in Garmin's exercise catalogue (see exercise_catalog)"
            for c, e in pairs if (c, e) not in known]


def protocol_problems(conn: psycopg.Connection, kind: str, name: str, description: str | None, params: dict) -> list[str]:
    """Everything that would stop this protocol uploading cleanly, found
    before any Garmin call: shape errors from the builder, then (strength)
    the catalogue check."""
    try:
        build(kind, name, description, params)
    except Exception as e:  # any bad input (incl. OverflowError on absurd numbers) is a note, never a crash
        return [f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e)]
    if kind == "strength":
        return strength_problems(conn, params)
    return []
