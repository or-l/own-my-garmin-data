# garmin-data

Pulls your Garmin Connect data (activities, daily health metrics,
training insight, challenges, badges) into your own private Postgres
database. You gather it, you own it, you query it — with plain SQL,
no special tooling required.

**Important: this is per-person.** Each person needs their own copy of
this repo, their own free Neon database, and their own Garmin login
token. You can't share a database or token between two Garmin accounts
— set it up independently for each person.

---

## What you'll need before starting

- [`uv`](https://docs.astral.sh/uv/) — installs Python itself if needed, no
  separate Python/pip/venv setup:
  - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
- Your own Garmin Connect account (email + password, and your phone/email
  handy if you have two-factor/MFA enabled on it)
- A free [Neon](https://neon.tech) account (Postgres database, no credit
  card needed for the free tier)
- `git` installed, to download the code
- The [`gh`](https://cli.github.com) CLI, logged in (`gh auth login`) — used
  to set your repo's Actions secrets and trigger the initial backfill

Setup takes about 10 minutes the first time.

---

## Setup

```bash
git clone <your-repo-url>
cd garmin-data
uv run install.py
```

This one command: connects to your Neon database, applies the schema, walks
you through Garmin login (masked password, MFA-aware, retries on a wrong
password/code), pushes your GitHub Actions secrets via `gh`, and triggers
the full-history backfill to run on GitHub's servers — nothing long-running
happens on your own machine.

Before running it, create a **Neon project** (any name) — its default
connection string is your `DATABASE_URL`. `uv run install.py` will prompt
for it.

Push this repo to your own GitHub account (make your own copy/fork, not a
shared one) *before* running `install.py`, so it can detect the repo and set
its secrets automatically.

### Choosing what gets synced

`install.py` asks which data domains to sync (activities, daily wellness,
body composition, training & performance, challenges & badges, profile —
all on by default) and stores the choice in the database itself
(`sync_config.enabled_domains`), so both your local wizard and the GitHub
Actions runners see the same selection without a new secret.
`reproductive` health (menstrual calendar, pregnancy) exists in the
registry but isn't implemented yet — it stays off until its endpoints are
verified against a live account, and isn't offered in the checklist until
then.

Non-interactive setup: `uv run install.py --domains activities,wellness,training`
(or set `GARMIN_DOMAINS`).

### Adding domains later

Already set up and want to turn on something new (or pick up new data
that landed inside a domain you already have)? Run:

```bash
uv run install.py --upgrade
```

It shows what's new, lets you adjust your domain selection, applies any
pending schema, and triggers a backfill scoped to just what you added —
existing domains and data are untouched.

### Re-authenticating later

If you ever change your Garmin password (or a token just stops working),
re-run the same login flow on its own:

```bash
uv run install.py --reauth
```

### Prefer to do it by hand?

Each piece the wizard automates is also runnable individually —
`uv run apply_schema.py` (see "Schema" below — the bare command only
applies base tables; use `install.py`/`install.py --upgrade` to also
apply your chosen domains' migrations),
`uv run bootstrap/generate_token.py`,
`uv run bootstrap/load_token_to_db.py`, `uv run ingestion/pull.py` (a
one-off incremental pull, same code the daily sync runs), `uv run
backfill.py` (full history — expect hours, safe to stop/resume), and
`.github/workflows/backfill.yml` (trigger it from the Actions tab).

### Schema

`apply_schema.py` now runs the migration files under `schema/migrations/`
(each tagged to one domain) instead of a single `schema/init.sql` — see
`docs/garmin_api_coverage.md`'s "Data domains" section. Run bare, it
only applies the base migration (`auth_tokens`, `sync_config`,
`schema_migrations`); applying a domain's tables requires passing your
enabled domains, which `install.py` and `install.py --upgrade` do for
you.

---

## Querying your data

This is the whole point: it's just Postgres. Connect with any Postgres
client (`psql "$DATABASE_URL"`, a GUI like TablePlus, or a script) and
query it directly.

Your 10 most recent activities:

```sql
select activity_id, started_at
from activities
order by started_at desc
limit 10;
```

Steps, distance, and resting heart rate for the last two weeks:

```sql
select metric_date,
       stats ->> 'totalSteps'          as steps,
       stats ->> 'totalDistanceMeters' as distance_m,
       stats ->> 'restingHeartRate'    as resting_hr
from daily_metrics
order by metric_date desc
limit 14;
```

Most tables here store Garmin's raw JSON response in a `jsonb` column
(`raw` on `activities`, `stats`/`sleep`/`hrv`/etc. on `daily_metrics`) —
see `schema/migrations/` for the full table list and
`docs/garmin_api_coverage.md` for what each Garmin API method maps to.
Postgres's `->>` (get JSON field as text) and `->` (get JSON field as
JSON) operators let you reach into those payloads from plain SQL.

### Want Claude to be able to query this for you?

See the separate [`postgres-mcp`](#) project (fill in your link) — it's
a small, standalone MCP server you point at this same database, using a
read-only role you create there. It's optional and lives outside this
repo entirely; this repo doesn't need it, describe, or embed any part of
it.

---

## Coach loop (optional)

Off by default. The coach loop lets a coach (you + Claude, through an MCP
write tool) plan workouts in the database, lets **you** approve them, and
then puts the approved ones on your Garmin calendar. It's the only part of
this project that writes to Garmin, and the write is always manual.

Turn it on with:

```bash
uv run install.py --coach-loop
```

The opt-in tells you up front that it **grants a component write access to
your Garmin account and creates two scoped database users** (`coach` and
`scheduler`). It also enables the read-only `workouts` domain, seeds two
default protocols, and writes `COACH_DATABASE_URL` and
`SCHEDULER_DATABASE_URL` to `.env`. Re-running it doesn't rotate existing
passwords. A read-only install never creates these users.

### Who can do what

Isolation is enforced by Postgres roles and GRANTs, not by the code
choosing to behave. Even a prompt injection riding in on synced data can't
make the coach exceed its grants.

| Actor | Garmin | DB credential | Trigger | Does |
|---|---|---|---|---|
| Ingestion | read | owner `DATABASE_URL` (unchanged — no separate ingestion role) | daily CI schedule | reads Garmin → ingestion tables |
| MCP read tool | none | your MCP read role (e.g. `bk_garmin_data_mcp_reader`) | interactive | SELECT (never `auth_tokens`) |
| MCP write tool ("coach") | none | `COACH_DATABASE_URL` | interactive | writes `training_plan`, `protocols` |
| Enactor (`seed_workouts.py`, `sync_schedule.py`) | write | `SCHEDULER_DATABASE_URL` | **manual** | uploads/schedules; writes the ledger + enactment columns |
| Approval CLI (`plan_admin.py`) | — | owner `DATABASE_URL` | **manual** | the only thing that can approve |

| Table | coach | scheduler | owner |
|---|---|---|---|
| every public table except the two below | SELECT | SELECT | all |
| `auth_tokens` | none | SELECT, UPDATE(payload, updated_at) | all |
| `training_plan` | SELECT, INSERT/UPDATE(scheduled_date, source, protocol_slug, workout_id, workout_name, skip, note), DELETE (un-enacted rows only, by trigger), USAGE on id sequence | SELECT, UPDATE(status, schedule_id, resolved_workout_id, enacted_date) | all |
| `protocols` | SELECT, INSERT, UPDATE, DELETE | SELECT | all |
| `generated_workouts` | SELECT | SELECT, INSERT, UPDATE | all |
| `plan_approvals` | none | SELECT | all (via plan_admin.py) |
| read role (if configured and exists) | SELECT on every table except `auth_tokens` | | |

### Two workout sources

- **`generated`** — a row in `protocols` (a recipe: kind + params). The
  seed job builds it and uploads it as a real Garmin workout. Only the
  `other` kind (interval/hangboard repeaters) has a generator today.
- **`library`** — any workout already in your Garmin library, of any type
  (strength, run, catalog, hand-built), used as-is. Give it by `workout_id`
  or by `workout_name` (exactly one). A name that matches 0 or 2+ library
  workouts is skipped, never guessed — many catalog names repeat (e.g.
  "Drill Workout"), so prefer `workout_id`.

### The order things happen in

1. Ingestion syncs your library and calendar (the `workouts` domain).
2. The coach writes `training_plan` / `protocols` through `run_write_sql`.
3. You review and approve: `uv run plan_admin.py list`, then
   `uv run plan_admin.py approve --id N` (or `--date YYYY-MM-DD`). It shows
   exactly what it's about to approve and asks you to type `yes` (or pass
   `--yes`); an edit made after that shows up as STALE.
4. `uv run seed_workouts.py --confirm` uploads any approved generated workouts.
5. `uv run sync_schedule.py --confirm` puts approved rows on the calendar —
   or Actions tab → **garmin-enact** → Run workflow with **confirm** checked.

Both enactor commands are dry runs without `--confirm` (or a typed `yes`
at a terminal), and re-running them never duplicates anything.

- **Approval is fingerprinted.** Editing an approved row or its protocol
  voids the approval — `plan_admin.py list` shows it as STALE and the
  enactor skips it until you approve again.
- **Cancelling.** To cancel a session the coach sets `skip = true`. The
  next enact removes it from the Garmin calendar, with no re-approval
  needed (removal is the safe direction). A row that's on the calendar
  can't be deleted until then. If two rows resolve to the same workout on
  the same day, only one is scheduled.
- **Manual-only enactment.** No cron workflow writes to Garmin. The CI
  path (`garmin-enact`) needs the `SCHEDULER_DATABASE_URL` secret, which is
  a standing Garmin-write-capable credential in CI; running the two
  commands locally keeps it off shared infrastructure.

### Did I do it?

"Done" is derived, never stored — ingestion stays read-only:

```sql
select tp.*, a.activity_id
  from training_plan tp
  join activities a on (a.raw->>'workoutId')::bigint = tp.resolved_workout_id
                   and (a.raw->>'startTimeLocal')::date = tp.enacted_date;
```

Garmin's own planned→done link (`associatedActivityId`) is also exposed by
`get_scheduled_workout_by_id`, as an alternative to that join.

**Data hygiene:** `Other`-typed workout activities carry junk
distance/GPS and `exerciseSets: null`. Read what was actually done from
`splitSummaries`, HR zones and `activityTrainingLoad`, and ignore
distance/pace/GPS. Library workouts of proper types (run, strength) are
self-describing.

**Verified / not yet verified:** the calendar item `id` equals the
`workoutScheduleId` (verified 2026-09-30), so unscheduling by calendar id
is correct. Multi-set nesting (`sets > 1` in the generator) is **not yet
verified** against a hand-built workout, so the default protocols are all
single-set.

---

## Automatic daily syncing (no computer needs to stay on)

This uses GitHub Actions to run the sync automatically every day, on
GitHub's servers rather than your own machine.

`uv run install.py` already pushed this repo's `NEON_DATABASE_URL` (and
optional `NTFY_TOPIC`, for a push notification if a sync fails — subscribe
to that same topic name in the free [ntfy](https://ntfy.sh) app) as repo
secrets, and triggered the initial backfill. From here:

- It runs automatically on its own schedule (currently daily) — nothing
  else to do.
- Trigger it manually any time from the **Actions** tab → `garmin-sync` →
  **Run workflow**.
- Didn't run through the wizard, or want to double-check the secret got
  set? **Settings → Secrets and variables → Actions** in your repo.

### Triggering a backfill manually

The initial history backfill runs automatically as part of `install.py`,
but you can re-trigger it any time — e.g. to pull further back, or to
retry after a failure — from the **Actions** tab → `garmin-backfill` →
**Run workflow** (optionally set a start date). It's safe to re-run:
`backfill.py` skips dates it's already pulled.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `uv: command not found` | Re-run the install line for your OS from "What you'll need," then open a new terminal |
| `KeyError: 'DATABASE_URL'` | `.env` isn't set up — re-run `uv run install.py`, or set `DATABASE_URL` yourself |
| `relation "..." does not exist` | Re-run `uv run install.py --upgrade` to apply any pending migrations for your enabled domains (bare `apply_schema.py` only applies base tables) |
| `429` / rate limited during login | Wait a few minutes before retrying — Garmin temporarily blocks repeated login attempts |
| `MFA Required` | Your account has two-factor enabled; `install.py`/`generate_token.py` will prompt for the code — make sure you're ready to receive it |
| `gh: command not found` / not authenticated | Install `gh` and run `gh auth login`, then re-run `uv run install.py` — or follow the manual secret-setup instructions it prints |

---

## A few honest caveats

- This uses an **unofficial** Python library (`garminconnect`) that mimics
  Garmin's own app, since Garmin's official developer program isn't open
  to individuals. It generally works well but can break if Garmin changes
  something on their end — if a sync suddenly fails, that's the most
  likely cause.
- Keep sync frequency reasonable (this is already set up to run once a
  day) — hammering Garmin's login endpoint risks your account getting
  temporarily flagged.
