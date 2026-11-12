# Runbook: move pigment-weekly-refresh to Scale infrastructure

Goal: run this job from a repository Scale owns, against the same Pigment workspace, with no
overlap between the old and new runners and no missed weekly snapshot.

Assumption: "Scale infra" means a GitHub organization with GitHub Actions. If Scale runs CI
somewhere else (GitLab, Jenkins, Airflow, Kubernetes cron), see section 9.

All times are UTC unless marked. Pacific is UTC-8 until March 2027.

---

## 0. Decide these before you start

| # | Decision | Recommendation |
|---|----------|----------------|
| D1 | Destination org and repo name | Private repo, e.g. `<scale-org>/pigment-weekly-refresh` |
| D2 | Runner type | GitHub-hosted `ubuntu-24.04` (no change). If self-hosted is required, see 9.1 |
| D3 | Reuse the current Pigment API keys, or issue new ones owned by Scale | Issue new keys. Revoke the old ones after the decommission step |
| D4 | Where the ping goes (`PING_WEBHOOK_URL`) | A Scale-owned chat channel. Payload is `{"text": "..."}` (Slack incoming-webhook format) |
| D5 | Who pushes the workflow file in the new repo | The person who should receive failure emails (see 5.4) |
| D6 | `PUBLISH_ENABLED` for the first week on the new runner | `false` for one week, then `true` (see 6.3) |
| D7 | Cutover moment | Today, before Monday 2026-11-09 08:00 UTC (the default weekly slot) |
| D8 | Who keeps admin access after handover (Zaid / Brant Advisory) | Agree it now; write it in section 8 |

**Blocking question:** is the current `PIGMENT_RUN_CONFIG` value saved anywhere outside GitHub
(password manager, 1Password, notes)? GitHub secrets cannot be read back. If it is not saved,
you must rebuild it from Pigment (section 3.3) before the meeting ends.

---

## 1. What is being moved

Three files, standard library Python only, no dependencies:

- `refresh.py`: the runner
- `.github/workflows/weekly-refresh.yml`: hourly cron at :17, plus manual dispatch
- `README.md`

Configuration that lives **outside** the code and must be recreated by hand:

| Name | Kind | Purpose |
|------|------|---------|
| `PIGMENT_IMPORT_KEY` | secret | Pigment Import API key (triggers, pushes, import status) |
| `PIGMENT_EXPORT_KEY` | secret | Pigment Export API key (view exports) |
| `PIGMENT_RUN_CONFIG` | secret | JSON of view ids and import configuration ids (schema in 3.3) |
| `PING_WEBHOOK_URL` | secret | Chat webhook for result messages |
| `RUN_ENABLED` | variable | `true` turns everything on, including the daily clock roll. Anything else = off (except `mode=check`) |
| `PUBLISH_ENABLED` | variable | `true` lets a clean capture publish on its own |

Nothing changes in Pigment. The schedule, pause switch and run log stay on the workspace admin page.

---

## 2. The one rule: never run both at once

The old and new repos have separate `concurrency` groups, so GitHub will not stop them overlapping.
If both have `RUN_ENABLED=true`:

- **Clock double-roll.** Both ticks at 08:17 see Hub today one day behind and each fires one roll.
  Hub today ends up a day ahead. Every later tick then stops with "a person reseeds it" (exit 2).
- **Double capture.** The "tried" guard in `fire()` re-reads the trigger after writing STARTED. It
  is not a lock: two runners ticking in the same minute can both write STARTED, both read
  `tried=1`, and both fire the copy configurations.

So the order is always: **old off, confirm off, then new on.** Rollback is the same in reverse.

---

## 3. Prep (before or at the start of the meeting)

### 3.1 Access

- [ ] DB can create a repo in the Scale org and manage its secrets and variables
- [ ] Zaid has admin on the old repo `CentrifugalBadger/pigment-weekly-refresh`
- [ ] A Pigment workspace admin is reachable (to issue API keys and view the run log)

### 3.2 Scale org settings to check

- [ ] Actions enabled for the new repo
- [ ] Org "allowed actions" policy permits `actions/checkout` pinned at
      `11d5960a326750d5838078e36cf38b85af677262`. Some orgs allow only internal or approved actions
- [ ] Actions minutes: about 775 runner minutes a month (730 for the hourly tick, about 45 per weekly
      capture). Private repos bill against Scale's plan
- [ ] Any org policy on `ubuntu-24.04` runners, outbound network, or required workflow approvals

### 3.3 Collect secret values

Gather all four values into the meeting's password manager share, not chat or email.

`PIGMENT_RUN_CONFIG` shape (labels are free text; ids come from Pigment):

```json
{
  "views": {
    "trigger": "<view id: T1 trigger string>",
    "state": "<view id: run-state verdict>",
    "live_lam": "<view id: Live Last Actuals Month>",
    "hub_today": "<view id: Hub today>",
    "recon": [{"label": "01", "view": "<view id>"}]
  },
  "configs": {
    "hub_today_roll": "<import config id: roll Hub today one day>",
    "row_create": "<import config id: add the week's snapshot row>",
    "row_publish": "<import config id: push Snapshot Date>",
    "run_log": "<import config id: run-log list>",
    "snap": [{"label": "01-a", "id": "<import config id>", "expect_rows": true}],
    "month_grain": ["<import config id>"]
  }
}
```

`expect_rows` is optional (default `true`). `month_grain` is optional.

If you issue new API keys (D3), confirm the new keys can reach every application referenced above.
I have not verified whether Pigment scopes import configurations to a specific key; test with
`mode=check` and `mode=logtest` before cutover.

---

## 4. Copy the repo (keeps history)

Run from any machine with access to both orgs.

```bash
git clone --bare https://github.com/CentrifugalBadger/pigment-weekly-refresh.git
```

```bash
cd pigment-weekly-refresh.git
```

```bash
git push https://github.com/<scale-org>/pigment-weekly-refresh.git main
```

Use `--bare` and push `main` only. Do not use `--mirror`: it pulls `refs/pull/*`, which GitHub
refuses on push, and it copies working branches you do not need.

- [ ] Default branch in the new repo is `main` (scheduled workflows only run from the default branch)
- [ ] Repo is private

Note: the copied commits keep their original author (Zaid). See 5.4 for who gets failure emails.

---

## 5. Configure the new repo (still switched off)

### 5.1 Secrets

Settings > Secrets and variables > Actions > Secrets:

- [ ] `PIGMENT_IMPORT_KEY`
- [ ] `PIGMENT_EXPORT_KEY`
- [ ] `PIGMENT_RUN_CONFIG` (paste the JSON as one value; validate it with `python3 -m json.tool` first)
- [ ] `PING_WEBHOOK_URL`

### 5.2 Variables

Settings > Secrets and variables > Actions > Variables:

- [ ] `RUN_ENABLED` = `false` (leave off for now)
- [ ] `PUBLISH_ENABLED` = per D6

### 5.3 Read-only check

Actions > weekly-refresh > Run workflow > `mode=check`. This runs even with `RUN_ENABLED=false`.

Pass looks like a green run with log lines for `trigger`, `clock`, `state`, one `recon` per app,
and a `config` line where `snap_configs` and `month_grain` match the old repo's counts.

| Failure | Likely cause |
|---------|--------------|
| `export trigger: HTTP 401/403` | Export key wrong or lacks access |
| `KeyError` / `unexpected error (KeyError)` | `PIGMENT_RUN_CONFIG` missing a key |
| `JSONDecodeError` | `PIGMENT_RUN_CONFIG` is not valid JSON |
| `not in the T1 format` | `views.trigger` points at the wrong view |
| Hub today N days off | Fine if N is 0 or 1 before 08:17. Otherwise see 7.2 |

Also confirm the `selftest (offline)` step passed.

### 5.4 Failure notifications

GitHub emails scheduled-run failures to the user who last modified the `cron:` line. After a history
copy that may still resolve to Zaid. To make it DB (or whoever D5 names), have that person commit a
change to the cron expression in the new repo. I am not sure a comment-only edit counts, so the
safe version is two commits: `17 * * * *` to `18 * * * *`, then back to `17 * * * *`. Exit codes 2 (needs a person) and 75 (deferred) also show as failed runs, so the
recipient will see those too. Confirm with one deliberate failure if unsure.

### 5.5 Test the webhook

The script only pings during a real run or on an error, so test the URL directly:

```bash
curl -sS -X POST -H 'Content-Type: application/json' -d '{"text":"pigment-weekly-refresh webhook test"}' "$PING_WEBHOOK_URL"
```

---

## 6. Cutover

Do not cut over inside a no-write window: **05:00-07:30, 09:45-11:30, 12:00-12:15 UTC**
(21:00-23:30, 01:45-03:30, 04:00-04:15 Pacific). A mid-morning or afternoon Pacific meeting is clear.

Avoid the minutes around :17 past each hour so an old tick is not in flight when you flip it off.

### 6.1 Turn the old repo off

In `CentrifugalBadger/pigment-weekly-refresh`:

- [ ] Variables: `RUN_ENABLED` = `false`
- [ ] Actions > weekly-refresh > "..." > Disable workflow (belt and braces)
- [ ] Actions tab: no run in progress

### 6.2 Turn the new repo on

In `<scale-org>/pigment-weekly-refresh`:

- [ ] Variables: `RUN_ENABLED` = `true`
- [ ] Run workflow `mode=logtest`. This writes one `TEST` line to the Pigment run log and nothing
      else. It proves the import key and the `run_log` configuration work
- [ ] In Pigment, the run log shows the TEST line with a Run Link pointing at the **new** repo
- [ ] Wait for the next :17 scheduled tick. Expect a green run with `"result": "not due"` (or
      `paused`), and no ping

Record the cutover time (UTC) in section 8.

### 6.3 First weekly run on the new runner

Default slot is Monday 2026-11-09 at the default hour set in Pigment.

- If `PUBLISH_ENABLED=false`: the run captures and reconciles, logs `CAPTURED`, and stops. Someone
  checks the snapshot by hand, then runs `mode=publish`. Flip `PUBLISH_ENABLED=true` after that.
- If `PUBLISH_ENABLED=true`: expect a `PUBLISHED` ping.

Also check that Tuesday's 08:17 tick rolled Hub today (log line `clock` with `lag_days` 1, then 0 the
next tick).

---

## 7. Operating the job

### 7.1 Exit codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Done, or nothing due | None |
| 1 | A step failed | Read the log; the row stays open. Set a new slot on the admin page to retry |
| 2 | Needs a person | Message says what (reseed, paused, unexpected state) |
| 75 | Deferred (no-write window) | Re-run after the window |
| 64 | Bad mode argument | Fix the dispatch input |

A slot fires once. To retry, set a new time on the Pigment admin page; the next tick picks it up.

### 7.2 Reseed Hub today

Triggered by "Hub today is blank" or "Hub today is N days off the runner's date" (N < 0 or N > 31).
In Pigment, set Hub today to today's UTC date by hand, then run `mode=check` to confirm `lag_days` is 0.
The exact Pigment steps belong here; fill them in with DB.

### 7.3 Manual modes

| Mode | Writes? | Use |
|------|---------|-----|
| `check` | No | Health check; runs even when `RUN_ENABLED` is off |
| `logtest` | One run-log line | Prove import access |
| `roll` | Clock only | Catch up Hub today |
| `capture` | Yes | Run the weekly capture now (refused if paused) |
| `publish` | Yes | Publish an OPEN, clean row |
| `auto` | Yes | What the hourly schedule runs |

### 7.4 Rollback

- [ ] New repo: `RUN_ENABLED=false`, disable workflow
- [ ] Old repo: enable workflow, `RUN_ENABLED=true`
- [ ] `mode=check` on the old repo

---

## 8. Handover record

| Item | Value |
|------|-------|
| New repo URL | |
| Cutover time (UTC) | |
| Old Pigment keys revoked (date) | |
| Failure emails go to | |
| Ping channel | |
| Zaid / Brant Advisory access after handover | |
| Owner at Scale | DB |

### Decommission (after two clean weekly runs)

- [ ] Delete the four secrets from the old repo
- [ ] Revoke the old Pigment API keys (if D3 = new keys)
- [ ] Archive the old repo. **Do not delete it**: past run-log lines in Pigment link to its Actions runs
- [ ] Old run links will expire with GitHub's log retention anyway (90 days by default)

---

## 9. If Scale does not use GitHub Actions

### 9.1 Self-hosted GitHub runner

Change `runs-on` to Scale's runner label. The runner needs `python3` (3.9+), outbound HTTPS to
`pigment.app` and to the webhook host, and a clock synced to UTC. `GITHUB_*` variables keep working.

### 9.2 Another scheduler

The script runs anywhere with Python 3 and the six environment variables in section 1:

```bash
python3 -I refresh.py selftest && python3 -I refresh.py auto
```

- Schedule hourly at minute 17
- Only one instance at a time (the workflow's `concurrency` group did this; replicate it)
- Allow up to 75 minutes per run
- Set `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID` so the run-log Run Link points
  somewhere useful, or accept a broken link (`run_url()` in `refresh.py` builds it from those three)
- Treat exit codes 1, 2 and 75 as alerts
