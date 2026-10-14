-- domain: workouts
-- Coach-facing documentation, stored IN the database. The MCP coach only
-- sees the schema (postgres-mcp's list_tables/describe_table surface these
-- comments), so the coach-loop workflow has to be discoverable here rather
-- than only in the README. Comments only — no schema change.

comment on table training_plan is
'COACH PLANNING TABLE — the coach''s write target. To plan a session, INSERT a row here with run_write_sql: one row = one workout on one date. This is the ONLY way a coach puts a workout on the athlete''s Garmin calendar; never write to workouts or scheduled_workouts. Nothing reaches Garmin until the owner approves the row (uv run plan_admin.py approve --id N) and runs the enactor (seed_workouts.py, sync_schedule.py) — after inserting, tell the athlete which row ids to approve. Two sources: source=''library'' reuses an existing Garmin workout (set exactly one of workout_id — preferred, take it from the workouts table since names repeat — or workout_name); source=''generated'' builds one from a protocols recipe (set protocol_slug only). To cancel: UPDATE ... SET skip = true (never DELETE a row that is already scheduled). Editing a row after approval voids the approval. Leave status, schedule_id, resolved_workout_id and enacted_date alone — the enactor owns them. Done or not: join activities on (raw->>''workoutId'')::bigint = resolved_workout_id and (raw->>''startTimeLocal'')::date = enacted_date.';

comment on column training_plan.id is 'Row id. Generated — omit it on INSERT (use RETURNING id). Tell the athlete these ids to approve.';
comment on column training_plan.scheduled_date is 'Day the workout should be done (athlete''s local date).';
comment on column training_plan.source is '''library'' = existing Garmin workout from the workouts table; ''generated'' = built from a protocols recipe.';
comment on column training_plan.protocol_slug is 'For source=''generated'' only: protocols.slug of the recipe to build. Must be null for library rows.';
comment on column training_plan.workout_id is 'For source=''library'': workouts.workout_id to schedule as-is (preferred over workout_name). Set exactly one of workout_id / workout_name.';
comment on column training_plan.workout_name is 'For source=''library'': exact workouts.workout_name, only if that name is unique in the library (many catalog names repeat, e.g. ''Drill Workout'' — use workout_id then).';
comment on column training_plan.skip is 'Coach''s cancel switch. Set true to cancel; the enactor removes the workout from the Garmin calendar on its next run (no re-approval needed). Set back to false to restore.';
comment on column training_plan.status is 'Enactor-owned, read-only for the coach: planned (not on Garmin yet), scheduled (on the Garmin calendar), skipped (cancelled and removed).';
comment on column training_plan.note is 'Free text for the coach: intent, targets, cues (e.g. ''easy, RPE 6''). Not sent to Garmin and not part of the approval.';
comment on column training_plan.schedule_id is 'Enactor-owned: Garmin calendar item id once scheduled.';
comment on column training_plan.resolved_workout_id is 'Enactor-owned: the Garmin workout id actually scheduled (for generated rows, the uploaded workout). Join activities on this to see if it was done.';
comment on column training_plan.enacted_date is 'Enactor-owned: the date it was put on the Garmin calendar.';
comment on column training_plan.created_at is 'Set automatically.';
comment on column training_plan.updated_at is 'Set automatically on every update.';

comment on table protocols is
'Workout recipes the coach may author for source=''generated'' plan rows. Only kind=''other'' exists: an interval workout on Garmin''s ''Other'' sport — warm-up (ends on lap button), then reps × (work_s on / rest_s off), then cool-down (also ends on lap button). params (seconds): {"work_s": 7, "rest_s": 3, "reps": 6} plus optional "warmup_s" (default 300), "cooldown_s" (default 300); keep "sets" at 1 (multi-set not verified yet). name and description are what the athlete sees on the watch. Editing a protocol voids approvals of plan rows that use it. For runs, strength, etc. there is no generator: schedule an existing library workout (training_plan.source=''library'') instead.';

comment on table workouts is
'READ-ONLY mirror of the athlete''s Garmin workout library, refreshed by the daily sync. Never write here. Pick workout_id from this table for training_plan.source=''library''; raw holds the full steps and description. Workouts generated from protocols appear here only after the enactor uploads them and the next sync runs.';

comment on table scheduled_workouts is
'READ-ONLY mirror of workouts on the Garmin calendar (from the start of the current month to ~62 days ahead), refreshed by the daily sync. Never write here — to plan, insert into training_plan; approved rows appear here after the enactor runs and the next sync.';

comment on table generated_workouts is
'READ-ONLY for the coach. Enactor-owned ledger: which uploaded Garmin workout currently materializes each protocol.';

comment on table plan_approvals is
'Owner-only approval gate (plan_admin.py). The coach can neither read nor write it; ask the athlete to approve instead.';
