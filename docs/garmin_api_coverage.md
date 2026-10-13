# Garmin Connect API coverage

Audit of every public method on `garminconnect.Garmin` (v0.3.6, the version
pinned in `ingestion/requirements.txt`), tracking what this project pulls
into Postgres versus what's still untapped.

This file didn't exist before the training/wellness/body-composition
expansion — it was created from scratch by listing `dir(garminconnect.Garmin)`
(136 public methods) and cross-checking against `ingestion/pull.py` and
`backfill.py`. Keep it in sync going forward: when you wire in a new
method, move it out of "Not used yet" and into "Currently ingested".

## Data domains

Every method in "Currently ingested" below is owned by exactly one domain
in `domains.py`, the single source of truth for what gets pulled. Each
domain has its own `sync_incremental` (incremental lookback pull) and,
where it has history to backfill, its own `backfill` routine with
independent skip-detection — see that file for the full mapping. Domains
are enabled per-install via `sync_config.enabled_domains` in the DB, set
by `install.py`'s checklist at first-time setup and adjustable later with
`install.py --upgrade`.

| Domain | Default | Owns |
|---|---|---|
| `activities` | on | `activities` table |
| `wellness` | on | `daily_metrics` (all columns except `weigh_in`) |
| `body_composition` | on | `daily_metrics.weigh_in` |
| `training` | on | `training_insight`, `performance_snapshots`, `personal_records` |
| `challenges` | on | `challenges`, `earned_badges`, `available_badges` |
| `profile` | on | `user_profile`, `goals` |
| `workouts` | off | `workouts`, `scheduled_workouts` (read-only); its migrations also create the coach-loop planning tables `training_plan`, `protocols`, `generated_workouts`, `plan_approvals`, which are only writable after `install.py --coach-loop` |
| `reproductive` | off (not yet implemented) | menstrual calendar / pregnancy data — gated on a live probe, see the plan doc |

Schema for each domain lives in its own `-- domain: <key>`-tagged file
under `schema/migrations/`, applied only when that domain is enabled —
`schema/init.sql` is frozen and no longer executed by any code path.

## Currently ingested

Every field name below was confirmed against a live API response before
being written into `schema/init.sql` — see the git history for the probe
sessions if a shape ever needs re-checking against a live account.

| Method | Stored as |
|---|---|
| `get_activities_by_date` | `activities` (upsert by `activityId`) |
| `get_available_badge_challenges` | `challenges` (upsert by `uuid`) |
| `get_earned_badges` | `earned_badges` (upsert by `badgeId`) |
| `get_available_badges` | `available_badges` (upsert by `badgeId`) |
| `get_stats` | `daily_metrics.stats` |
| `get_sleep_data` | `daily_metrics.sleep` |
| `get_all_day_stress` | `daily_metrics.stress` |
| `get_hrv_data` | `daily_metrics.hrv` |
| `get_max_metrics` | `daily_metrics.max_metrics` |
| `get_respiration_data` | `daily_metrics.respiration` |
| `get_spo2_data` | `daily_metrics.spo2` |
| `get_body_battery` | `daily_metrics.body_battery` — one range call per sync window, fanned out per date via the response's `date` field (not looped per-date) |
| `get_body_battery_events` | `daily_metrics.body_battery_events` |
| `get_intensity_minutes_data` | `daily_metrics.intensity_minutes` |
| `get_floors` | `daily_metrics.floors` — 15-minute interval data, not a duplicate of `stats.floorsAscended` (a daily total) |
| `get_steps_data` | `daily_metrics.steps_intraday` — 15-minute interval data, not a duplicate of `stats.totalSteps` |
| `get_heart_rates` | `daily_metrics.heart_rates` — ~2-minute interval time series, richer than `stats`' HR summary fields |
| `get_all_day_events` | `daily_metrics.day_events` |
| `get_weigh_ins` | `daily_metrics.weigh_in` — one range call per sync window, fanned out per date via `dailyWeightSummaries[].summaryDate`; null on days with no scale reading (expected, not a gap). Verified live with real Index S2 scale data (weight/BMI/bodyFat/bodyWater/boneMass/muscleMass under `sourceType: INDEX_SCALE`) — this account only has 27 real weigh-in dates spread across 2 years, not daily readings, which is why most `daily_metrics` rows have `weigh_in = null`. The weigh-ins backfill code path in `backfill.py` was added in `52a3ab5` but never actually re-run against production after that — confirmed via `git log -S upsert_weigh_ins`, and by `daily_metrics` only going back to the date `pull.py`'s rolling window started, not 2 years. Run manually on 2026-08-15 to land the missing history. Caution when reading `weigh_in->'latestWeight'`: it can carry zeroed composition fields on some dates (e.g. `2026-04-25`'s `latestWeight.bodyFat = 0.0` despite that date's `allWeightMetrics` having other entries with real values) — scan `allWeightMetrics` for a non-zero reading rather than trusting `latestWeight` blindly. |
| `get_training_status` | `training_insight.training_status` |
| `get_training_readiness` | `training_insight.training_readiness` |
| `get_morning_training_readiness` | `training_insight.morning_training_readiness` — kept separate from `training_readiness` even though it likely overlaps one entry in that list (by `inputContext=AFTER_WAKEUP_RESET`); direct query is simpler than parsing the list for it |
| `get_endurance_score` | `training_insight.endurance_score` — called with a single date (not a range); the range form returns a coarser multi-day rollup that's harder to key cleanly per day |
| `get_hill_score` | `training_insight.hill_score` — same single-date reasoning as `get_endurance_score` |
| `get_fitnessage_data` | `training_insight.fitness_age` |
| `get_running_tolerance` | `training_insight.running_tolerance` — called with `startdate == enddate`, `aggregation="daily"`; empty on this account/device, but confirmed to not error |
| `get_race_predictions` | `performance_snapshots` (`metric_name = 'race_predictions'`) — called with no arguments, which the library treats as "predictions as of today"; there's no history to loop over |
| `get_cycling_ftp` | `performance_snapshots` (`metric_name = 'cycling_ftp'`) — "most recent known value", not per-date |
| `get_lactate_threshold` | `performance_snapshots` (`metric_name = 'lactate_threshold'`) — called with `latest=True` (the default) |
| `get_personal_record` | `personal_records` (upsert by `id`, verified live) |
| `get_goals` | `goals` — **no verified id field** (this account has zero goals in any status, so the response shape's natural key couldn't be confirmed against real data). Stored via delete-then-insert per status (`active`/`future`/`past`) with a surrogate `bigserial` id, instead of guessing a primary key to upsert on. If you later see real goal data with a stable id field, switch this to an upsert like `personal_records`. |
| `get_user_profile` | `user_profile.profile` (singleton row, `id = 1`) — physiological baseline: weight, height, birthDate, gender, `vo2MaxRunning`/`vo2MaxCycling`, `lactateThresholdSpeed`/`lactateThresholdHeartRate`, HR-zone auto-detection flags, under a `userData` key. Confirmed live: no explicit maxHR field or HR-zone-boundaries array exists in this response — `lactateThresholdHeartRate` and `vo2MaxRunning` are the closest baseline reference points actually present. Note: `userData.lactateThresholdSpeed`/`lactateThresholdHeartRate` here are the same values as `performance_snapshots.lactate_threshold`'s `speed_and_heart_rate.speed`/`.heartRate` — one physiological reading exposed via two endpoints, not two independent ones. |
| `get_userprofile_settings` | `user_profile.settings` (same singleton row) — confirmed live to be purely locale/unit/display preferences (`timeZone`, `measurementSystem`, date/number/hydration formats). No physiological content, despite the similar method name to `get_user_profile` above — kept as a separate jsonb column rather than merged into `profile`, since the two responses don't overlap. |
| `get_workouts` | `workouts` (workouts domain) — paged list; its `updateDate` is the change marker (the detail endpoint calls it `updatedDate`), so unchanged workouts aren't re-expanded. An empty listing never wipes the table. |
| `get_workout_by_id` | `workouts.raw` — the full workout (steps + description) for each new/changed workout. Join: `activities.raw->>'workoutId'`. |
| `get_scheduled_workouts` | `scheduled_workouts` — called per month with a **1-based** month (the library converts). Each response is the whole calendar grid, including neighbouring-month days and non-workout items, so items are filtered to `itemType = 'workout'` and deduped by `id` (which equals `workoutScheduleId`, verified 2026-09-30). Window: start of this month → +62 days. |

## Investigated, deliberately not used (duplicates)

These were checked live and found to be lower-resolution or exact
duplicates of data already captured above — skipped per the "don't add
redundant columns" convention.

| Method | Why skipped |
|---|---|
| `get_daily_steps` | Range call returning `calendarDate, totalSteps, totalDistance, stepGoal` per day — identical fields to what `stats.totalSteps`/`totalDistanceMeters`/`dailyStepGoal` already provide. `get_steps_data` (kept) provides the genuinely new 15-minute granularity. |
| `get_rhr_day` | Returns the same `restingHeartRate` value already in `stats`, just wrapped in a `metricsMap` structure. No additional information. |
| `get_daily_weigh_ins` | **Re-verified — an earlier note here ("scoped to a single day's `totalAverage` — a strict subset") was wrong.** Live response for a date with 3 real readings has both a full `dateWeightList` (all 3 raw entries, same as `get_weigh_ins`' `allWeightMetrics`) *and* `totalAverage`. Still redundant with `get_weigh_ins`, since it takes one date at a time instead of a range — just not for the reason previously stated. |
| `get_body_composition` | **Re-verified — an earlier note here ("exact same `dateWeightList` entries as `get_weigh_ins`'s `allWeightMetrics`") was wrong.** Live response for the same date with 3 real readings returns only 1 entry in `dateWeightList` (the latest, deduplicated) — tested with a single-day call, an explicit `startdate == enddate` range, and a wider multi-day range spanning that date, all three collapse to 1 entry. Unlike `get_weigh_ins` and `get_daily_weigh_ins`, this endpoint does not preserve multiple same-day readings, so it's a strictly lossier source, not a duplicate. |
| `get_weekly_stress` | Weekly-rollup average stress keyed by week-end date — lower resolution than the daily `stress` data already pulled, and the weekly grain doesn't fit the `metric_date` primary key used everywhere else. |
| `get_weekly_intensity_minutes` | Same reasoning as `get_weekly_stress` — `intensity_minutes_data` already carries `weeklyModerate`/`weeklyVigorous`/`weeklyTotal` per day, so this weekly-grain endpoint adds no new information at daily resolution. |

## Not used yet

Everything else on the client. Grouped by theme; not exhaustive on every
possible use case, just enough to orient a future pass.

**Activity detail** (would need the three-tier fetch-if-missing pattern
described in this project's task notes, since it's one call per activity,
not per date): `get_activity`, `get_activity_details`, `get_activity_splits`,
`get_activity_split_summaries`, `get_activity_typed_splits`,
`get_activity_weather`, `get_activity_hr_in_timezones`,
`get_activity_power_in_timezones`, `get_activity_exercise_sets`,
`get_activity_gear`, `get_activities`, `get_activities_fordate`,
`get_last_activity`, `count_activities`, `download_activity`, `download`.

**Gear**: `get_gear`, `get_gear_activities`, `get_gear_defaults`,
`get_gear_stats`.

**Workouts / training plans** (beyond what the `workouts` domain and the
coach loop use): `get_scheduled_workout_by_id`,
`get_training_plans`, `get_training_plan_by_id`,
`get_adaptive_training_plan_by_id`,
`upload_cycling_workout`,
`upload_hiking_workout`, `upload_running_workout`,
`upload_swimming_workout`, `upload_walking_workout`, `download_workout`.

**Device management**: `get_devices`, `get_device_settings`,
`get_device_alarms`, `get_device_last_used`, `get_device_solar_data`,
`get_primary_training_device`.

**Health tracking outside this project's scope**: `get_blood_pressure`,
`set_blood_pressure`, `delete_blood_pressure`, `get_hydration_data`,
`add_hydration_data`, `get_menstrual_calendar_data`,
`get_menstrual_data_for_date`, `get_pregnancy_summary`,
`get_lifestyle_logging_data`.

**Nutrition**: `get_nutrition_daily_food_log`, `get_nutrition_daily_meals`,
`get_nutrition_daily_settings`.

**Golf**: `get_golf_scorecard`, `get_golf_summary`, `get_golf_shot_data`.

**Challenges (partial coverage)**: `get_adhoc_challenges`,
`get_badge_challenges`, `get_non_completed_badge_challenges`,
`get_in_progress_badges`, `get_inprogress_virtual_challenges` — only
`get_available_badge_challenges` is currently pulled.

**Account / profile / misc**: `get_full_name`,
`get_unit_system`, `get_progress_summary_between_dates`,
`get_stats_and_body`, `get_stress_data`, `get_weekly_steps`,
`query_garmin_graphql`, `request_reload`.

**Write operations**: ingestion is read-only; the only writes are
`upload_workout`, `schedule_workout`, `unschedule_workout` and
`delete_workout`, confined to `garmin_write.py` (an AST test enforces it)
and used only by the manual, approval-gated coach loop (see README).
Everything else below remains unused: `add_weigh_in`, `add_weigh_in_with_timestamps`, `delete_weigh_in`,
`delete_weigh_ins`, `add_body_composition`, `add_gear_to_activity`,
`remove_gear_from_activity`, `set_gear_default`, `create_manual_activity`,
`create_manual_activity_from_json`, `delete_activity`,
`import_activity`, `upload_activity`, `set_activity_name`,
`set_activity_type`, `set_activity_description`,
`set_activity_exercise_sets`.

**Auth / low-level plumbing** (used internally by the library, not
something this project calls directly): `login`, `logout`, `resume_login`,
`connectapi`, `connectwebproxy`.
