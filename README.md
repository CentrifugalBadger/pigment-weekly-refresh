# pigment-weekly-refresh

A scheduled GitHub Actions job that takes a weekly snapshot in a Pigment workspace through the Pigment
Import and Export APIs (Python standard library only). **When it runs is decided in Pigment**, on the
workspace's admin page, not in this repository.

- **Every hour (at :17):** reads one trigger value from the workspace: the next scheduled run (a date
  and a UTC hour), whether it was already tried, and a pause switch. Nothing else happens unless a run
  is due.
- **Once a day (from 08:00 UTC):** moves the workspace's "today" clock to the runner's UTC date. The
  pause switch does not stop this.
- **When the scheduled run is due** (and not tried, not paused, and an hour clear of the workspace's
  no-write windows): records the start in the workspace's run log, adds the week's snapshot row, fires
  the saved copy configurations, reads a reconciliation verdict per application, and publishes the row
  only when every copy landed and every verdict is clean. It records the result in the run log and
  sends one chat message. A slot is tried once; to retry, set a new time on the admin page.

Nothing tenant-specific lives in this repository. Keys, ids and the webhook are Actions secrets; the
repository variables `RUN_ENABLED` (the full off switch, including the clock) and `PUBLISH_ENABLED`
switch the job and the publish step on. `workflow_dispatch` with `mode=check` is read-only;
`mode=logtest` writes one test line to the run log and nothing else. Logs carry step names and state
words only.

Cost: about 730 runner minutes a month for the hourly tick (each tick bills one minute) plus about
45 minutes per weekly capture.
