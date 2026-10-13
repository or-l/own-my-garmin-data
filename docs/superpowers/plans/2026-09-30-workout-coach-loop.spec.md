# Claude Code task: workout coach loop — three tiers, four DB roles, hard-gated writes

You are working in the `garmin-data` repo — a read-only pipeline that ingests
Garmin Connect data into Neon Postgres (`domains.py` / `config.py` /
`schema/migrations/*.sql`; `pull.py` incremental, `backfill.py` history; DB-token
auth in `bootstrap/garmin_auth.py`). This task adds a **write side** — generating
workouts, ingesting the workout library, and scheduling workouts onto the Garmin
calendar for a coach loop — WITHOUT compromising the read/write separation that
makes the pipeline safe to run and share.

Read the whole prompt first. The separation model below is the point of the
task; every part serves it. Prefer many small, verifiable commits.

================================================================================
## The model: three write-capable tiers, isolated by Postgres role — not by code

Two things touch Garmin: **ingestion (reads, automatic)** and **the enactor
(writes, manual)**. The MCP never touches Garmin. The "coach" is just you + Claude
+ a scoped SQL write tool. Isolation is enforced by database GRANTs, so even a
prompt-injection riding in on ingested data can't exceed a role's grants.

Actors and their credentials:

| Actor | Garmin | DB role | Trigger | Does |
|---|---|---|---|---|
| Ingestion | read (session) | `ingestion` (existing) | CI schedule | reads Garmin → ingestion tables |
| MCP read tool | none | `read` (existing) | interactive | SELECT everything |
| MCP write tool ("coach") | none | `coach` (new) | interactive | writes `training_plan`, `protocols` |
| Enactor: seed + sync | write (same session) | `scheduler` (new) | **manual** | uploads/schedules to Garmin; writes ledger + status |
| Approval CLI | — | `owner` | **manual** | the ONLY thing that can approve |

Grants (apply idempotently; this table is the spec):

| Table | `coach` | `scheduler` | `owner` |
|---|---|---|---|
| ingestion tables, `workouts`, `scheduled_workouts` | SELECT | SELECT | all |
| `auth_tokens` | **— none —** | SELECT + UPDATE | all |
| `training_plan` | INSERT/UPDATE/DELETE | SELECT + UPDATE | all |
| `protocols` | INSERT/UPDATE/DELETE | SELECT | all |
| `generated_workouts` (ledger) | SELECT | INSERT/UPDATE | all |
| `plan_approvals` | **— none —** | SELECT | INSERT/UPDATE |

The two load-bearing GRANTs: `coach` has **no** access to `auth_tokens` (so the
MCP write tool cannot even load a Garmin-capable credential) and **no** access to
`plan_approvals` (so it cannot approve). Both are DB-enforced, not conventions.

================================================================================
## Provided artifacts — integrate, don't redesign

`workout_gen.py` (pure "Other" generator, verified ids), `0008_workouts.sql`
(workouts read-domain tables), `workouts_domain_snippet.py` (domain funcs +
registry entry). Place/integrate them; verify live where noted.

================================================================================
## Invariants (hard constraints)

1. **Ingestion stays genuinely read-only.** `domains.py`, `pull.py`,
   `backfill.py`, `upserts.py` never import a Garmin write endpoint. ALL Garmin
   writes live only in `garmin_write.py`; add a test asserting no other module
   imports `upload_workout`/`schedule_workout`/`unschedule_workout`/`delete_workout`.
2. **Separation is by DB role, not code.** Each tier connects with its own role
   and connection string; correctness must not depend on a tier "choosing" to
   stay in bounds.
3. **Hard approval.** Approval is a row in `plan_approvals`, which `coach` cannot
   write. Only the owner CLI approves. The enactor acts ONLY on rows that have an
   approval — status is advisory, the approval row is authoritative.
4. **Ledger integrity.** `coach` reads `generated_workouts` but never writes it —
   only the enactor (which actually uploads and thus knows real workoutIds) does.
5. **Two workout sources, first-class; nothing assumes "Other".** The coach
   schedules either a `generated` protocol (seed materializes it) or any existing
   `library` workout of any type (Strength/Run/HIIT/catalog/hand-built), by
   id/name, used as-is.
6. **Idempotent.** Seed keys on slug + params_hash; enactor keys on
   (workout_id, date). Re-runs converge, never duplicate.
7. **Manual enactment only.** No scheduled workflow writes to Garmin. Both a local
   command and a GitHub `workflow_dispatch` (manual) exist; both dry-run then
   require confirmation.
8. **Verify live before trusting shapes** (Part 0). **Don't break existing
   installs**: new migrations additive + `-- domain:`-tagged; roles created only
   on explicit coach-loop opt-in.

================================================================================
## Part 0 — Probes (run first)
a. `get_scheduled_workouts` arity/shape → feed `_fetch_scheduled` + the
   `scheduled_workouts` column mapping.
b. Multi-set nesting: human hand-builds one nested workout, reflect it, diff
   against `make_other_workout(..., sets=5)`; fix the generator's `sets>1` branch,
   drop its "UNVERIFIED" caveat. Single-set is already verified.

## Part 1 — Generator
Land `workout_gen.py`; add a test that single-set output matches the `finger`
structure field-for-field. Leave a `kind` seam for a future tier-1 (Strength)
generator; don't build it — tier-1 is reached via the `library` source.

## Part 2 — `workouts` read-domain
Land `0008_workouts.sql` + integrate `workouts_domain_snippet.py` into
`domains.py` (`default_enabled=False`, `backfill=None`). Expand each workout via
`get_workout_by_id` so full steps + description are stored. Tighten
`_fetch_scheduled` from Part 0a. Verify the `activity.workoutId → workouts.workout_id`
join returns the description.

## Part 3 — Planning schema (`schema/migrations/0009_workout_planning.sql`, `-- domain: workouts`)
- `training_plan`: `id`, `scheduled_date`, `source` check `('generated','library')`,
  `protocol_slug`, `workout_id`, `workout_name`, `status` check
  `('planned','scheduled','skipped')` default `planned`, `schedule_id`, `note`,
  timestamps. CHECK: generated ⇒ slug not null; library ⇒ workout_id or name not
  null. Unique `(scheduled_date, coalesce(protocol_slug, workout_name, workout_id::text))`.
  There is NO `approved` status and NO `done` status — "done" is derived via the
  activity→plan join (keeps ingestion read-only; nothing writes it back).
- `protocols`: `slug` PK, `name`, `description`, `kind` default `'other'`, `params`
  jsonb (generator kwargs), timestamps. Promoted from code so the coach authors
  protocols from MCP.
- `generated_workouts` (ledger): `protocol_slug` PK, `workout_id`, `params_hash`,
  timestamps.
- `plan_approvals`: `plan_id` PK (FK training_plan), `approved_by`, `approved_at`
  default now(), optional `note`. The hard gate.

## Part 4 — Protocol defaults (`protocols.py`)
Keep `protocols.py` as pure DEFAULT_PROTOCOLS (slug → name/description/kind/params)
+ `build(slug, params) -> dict` dispatching on `kind` to `make_other_workout`. A
setup step inserts defaults into the `protocols` table only if empty; the table is
source of truth thereafter, `build` stays code.

## Part 5 — `garmin_write.py`
The ONLY module importing Garmin write endpoints. DB-token auth (reuse
`bootstrap.garmin_auth`) + robust wrappers `upload_workout`/`schedule`/
`unschedule`/`delete` (parse ids via recursive first-key search). No idempotency
here.

## Part 6 — Seed job (`seed_workouts.py`, `scheduler` role + Garmin token, manual)
For each generated slug referenced by APPROVED plan rows: resolve params from the
`protocols` table, hash them, consult `generated_workouts`: absent → build +
`upload_workout` + insert ledger; hash changed → re-upload, repoint ledger (leave
old workout unless `--prune-stale` and no activity references it); unchanged →
skip. Never touches library workouts. Politeness sleeps; summary.

## Part 7 — Enactor / sync (`sync_schedule.py`, `scheduler` role + Garmin token, manual)
Select `training_plan` rows that (a) have a `plan_approvals` row, (b) aren't yet
scheduled. Resolve to a real workout_id: `generated` → ledger (missing → log/skip,
seed must run first); `library` → row `workout_id`, else resolve `workout_name`
against `workouts` (ambiguous/none → skip), cache resolved id. Diff against
`scheduled_workouts`/`_fetch_scheduled` so only missing (workout_id, date) pairs
are scheduled. **Dry-run by default: print the plan, require an explicit
`--confirm` (or a typed prompt) before any `schedule_workout`.** On write: set
`status='scheduled'`, store `schedule_id`. Idempotent; summary.

## Part 8 — Approval CLI (`plan_admin.py`, `owner` role, manual)
`plan list` (show planned rows + whether approved — read), `plan approve --id/--date`
(INSERT `plan_approvals`), `plan reject/unapprove` (DELETE the approval row).
Owner connection only. This is the hard gate's only writer.

## Part 9 — Enactor triggers
- Local: `uv run seed_workouts.py` then `uv run sync_schedule.py --confirm`.
- `.github/workflows/enact.yml`: **`workflow_dispatch` ONLY** (no schedule), a
  `confirm` boolean input, runs seed then sync. Uses `SCHEDULER_DATABASE_URL`
  from secrets (the Garmin token is loaded from `auth_tokens` via that DB URL — no
  separate Garmin secret). Document that this places a standing scheduler
  credential in CI; the local path keeps it off shared infra.

## Part 10 — `install.py` coach-loop opt-in (SEPARATE from the read-domain checklist)
Read domains (incl. the read-only `workouts` domain) stay the plain checklist. Add
a distinct, explicitly-labeled coach-loop opt-in whose consent text states it
**grants a component write access to your Garmin account and creates two scoped
database users**. On opt-in (owner connection): idempotently `CREATE ROLE coach`,
`CREATE ROLE scheduler`, apply the GRANTs table above via a re-runnable
`apply_coach_grants()` (so later tables get re-granted), seed the `protocols`
defaults, and emit `COACH_DATABASE_URL` + `SCHEDULER_DATABASE_URL`. Approval CLI
uses the existing owner URL. Never runs for read-only installs.

## Part 11 — MCP write tool (SEPARATE codebase: the fastmcp server, not this repo)
Deliver as a clearly-marked separate change. Add a `run_write_sql` tool on a
second connection using `COACH_DATABASE_URL` (coach role); keep the existing
`run_sql` on the read connection. **Generic SQL primitive only — no domain verbs
like "schedule_plan".** The coach role's GRANTs are the fence; the tool itself
stays dumb. If bespoke verbs are ever wanted, they belong in a future separate
coach MCP, never on the generic server.

## Part 12 — Docs, hygiene, tests
- Coverage doc + README: the actor/role table, two sources, seed→approve→enact
  ordering, opt-in coach loop, manual-only enactment.
- Data-hygiene note: `Other`-typed workout activities carry junk distance/GPS and
  `exerciseSets: null`; read execution from `splitSummaries` + HR zones +
  `activityTrainingLoad`, ignore distance/pace/GPS. Library workouts of proper
  types are self-describing.
- `scripts/smoke_test.py` (scratch DB, mocked Garmin writes): coach role CANNOT
  read `auth_tokens` or write `plan_approvals` (assert permission denied);
  enactor schedules only approved rows; generated round-trips seed→enact without
  dupes on re-run; library source schedules without seeding; enabling `workouts`
  creates the four tables, disabling leaves them.

================================================================================
## Acceptance criteria
1. Connecting as `coach`, `select * from auth_tokens` and `insert into
   plan_approvals …` both raise permission-denied. Connecting as `scheduler`,
   both succeed as granted.
2. An unapproved plan row is never scheduled; approving it (owner CLI) then
   running the enactor with `--confirm` schedules it; re-running schedules nothing.
3. Ingestion path imports zero Garmin write endpoints (test passes).
4. Generated + library sources both schedule; ledger only ever written by the
   enactor.
5. Read-only install unaffected; coach roles exist only after the explicit opt-in.

## Commit sequence
Part 0 probes → generator+test (1) → workouts domain (2) → planning schema (3) →
protocols (4) → garmin_write (5) → seed (6) → enactor (7) → approval CLI (8) →
triggers (9) → install opt-in + roles/grants (10) → docs+tests (12); MCP change
(11) is a separate commit in the fastmcp repo.

Ask before running any write job or role DDL against the real account/DB. Keep
every step re-runnable.
