#!/usr/bin/env python3
"""Weekly Week Version snapshot runner (GitHub Actions), driven by a trigger held in Pigment.

The workflow ticks every hour. Each tick reads one trigger value from the Hub (one export):
Hub today, the pause switch, the next scheduled run (a date and a UTC hour) and whether that
slot has already been tried. Then:

  - once a day, from ROLL_HOUR_UTC, it rolls the Hub's "today" clock to the runner's UTC date
    (the pause switch does not stop this: Hub today is the true date)
  - when now >= the scheduled slot, the slot has not been tried, the Hub is not paused and the
    next CAPTURE_MINUTES are clear of every no-write window, it fires the slot:
      1. writes a STARTED line to the Hub's run log and re-reads the trigger: the slot must now
         read as tried, so a slot can never fire twice
      2. reads the Hub's run-state verdict; acts only on NO ROW (add this week's row) or OPEN
      3. fires every snapshot copy configuration (Live -> the open row)
      4. reads each app's capture recon (cells off Live; 0 = clean)
      5. publishes the row (pushes its Snapshot Date) only if every copy landed and every recon is 0
      6. writes the result to the run log and pings it
  - after a publish the Hub moves the next slot on by itself (a formula: the Monday after the
    newest snapshot, or the admin's override while its week is unpublished)

This repository holds no tenant data and no ids. Everything tenant-specific comes from
Actions secrets: PIGMENT_IMPORT_KEY, PIGMENT_EXPORT_KEY, PIGMENT_RUN_CONFIG (JSON of
opaque labels and ids), PING_WEBHOOK_URL. The log and the ping carry step names, state
words, labels and counts only: never an amount, a response body or an id.

Modes: auto (the hourly schedule), roll, capture, publish, check (read-only), logtest (one
run-log line, nothing else), selftest (offline).
Exit codes: 0 done or nothing due; 1 a step failed; 2 a person is needed; 75 deferred.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://pigment.app/api/v1"
QUIET_UTC = [("05:00", "07:30"), ("09:45", "11:30"), ("12:00", "12:15")]
MAX_CATCH_UP_DAYS = 31
ROLL_HOUR_UTC = 8          # the daily clock roll waits for this hour (as the Mac job did)
CAPTURE_MINUTES = 60       # a slot fires only if this long a stretch is clear of every no-write window
EPOCH = dt.date(1970, 1, 1)
WORDS = {1: "FAILED", 2: "NEEDS A PERSON", 75: "DEFERRED"}


class Stop(Exception):
    """A step failed; the message is safe to log and ping."""

    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def log(step, **kw):
    # One line per step. Only words, labels and counts are ever passed in here.
    print(json.dumps({"utc": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "step": step, **kw}, sort_keys=True),
          flush=True)


def monday_of(day):
    return day - dt.timedelta(days=day.weekday())


def quiet_window(t):
    hm = t.strftime("%H:%M")
    return next((w for w in QUIET_UTC if w[0] <= hm < w[1]), None)


def clear_for(t, minutes):
    """True if no no-write window touches [t, t + minutes)."""
    end = t + dt.timedelta(minutes=minutes)
    for a, b in QUIET_UTC:
        for day in {t.date(), end.date()}:
            ws = dt.datetime.combine(day, dt.time.fromisoformat(a), t.tzinfo)
            we = dt.datetime.combine(day, dt.time.fromisoformat(b), t.tzinfo)
            if ws < end and t < we:
                return False
    return True


def last_cell(csv_text):
    """The value of a one-value view export: the last field of the last non-empty row."""
    rows = [r for r in csv.reader(io.StringIO(csv_text)) if any(f.strip() for f in r)]
    if len(rows) < 2:
        return None
    return rows[-1][-1].strip() or None


def parse_date(text):
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y %H:%M:%S %z", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise Stop("a date read from Pigment is in an unexpected format")


def parse_trigger(text):
    """'T1|hub|paused|slot|hour|tried' (days since 1970-01-01, 0/1 flags) -> dict; blanks -> None."""
    parts = (text or "").split("|")
    if len(parts) != 6 or parts[0] != "T1":
        raise Stop("the run trigger read from Pigment is not in the T1 format", 2)

    def day(s):
        return EPOCH + dt.timedelta(days=int(s)) if s.strip() else None

    try:
        hour = int(parts[4]) if parts[4].strip() else None
        t = {"hub": day(parts[1]), "paused": parts[2] == "1", "slot_date": day(parts[3]), "slot_hour": hour,
             "tried": parts[5] == "1"}
    except ValueError:
        raise Stop("the run trigger read from Pigment is not in the T1 format", 2)
    if hour is not None and not 0 <= hour <= 23:
        raise Stop("the scheduled hour read from Pigment is outside 0-23", 2)
    t["slot"] = (dt.datetime.combine(t["slot_date"], dt.time(hour), dt.timezone.utc)
                 if t["slot_date"] and hour is not None else None)
    return t


def state_word(text):
    return (text or "").split(" - ", 1)[0].strip().upper() or "BLANK"


def clean(text, n=240):
    """A run-log field: one line, no '|' (the Hub's last-run summary splits on it), bounded."""
    return " ".join(str(text).replace("|", "/").split())[:n]


def csv_line(*fields):
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerow(fields)
    return out.getvalue()


class Pigment:
    def __init__(self, env):
        self.import_key = env["PIGMENT_IMPORT_KEY"]
        self.export_key = env["PIGMENT_EXPORT_KEY"]

    def _call(self, method, path, key, body=None, ctype=None):
        headers = {"Authorization": "Bearer " + key}
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except urllib.error.URLError:
            return 0, ""

    def export(self, view_id, label):
        code, text = self._call("GET", f"/export/view/{view_id}", self.export_key)
        if code != 200:
            raise Stop(f"export {label}: HTTP {code}")
        return text

    def _wait(self, import_id, label, timeout_s=900):
        if not import_id:
            raise Stop(f"{label}: no import id returned")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            code, text = self._call("GET", f"/import/{import_id}/status?includedDetailedReport=true",
                                    self.import_key)
            if code == 200:
                data = json.loads(text or "{}")
                status = str(data.get("importStatus", "")).lower()
                if status == "completed":
                    s = (data.get("detailedReport") or {}).get("summary") or {}
                    return (s.get("insertedCount") or 0) + (s.get("updatedCount") or 0), s.get("skippedCount") or 0
                if status == "failed":
                    raise Stop(f"{label}: import failed")
            time.sleep(10)
        raise Stop(f"{label}: no final import status in {timeout_s // 60} min")

    def trigger(self, config_id, label):
        code, text = self._call("POST", f"/import/trigger?configurationId={config_id}", self.import_key)
        if code != 200:
            raise Stop(f"{label}: trigger HTTP {code}")
        return self._wait(json.loads(text or "{}").get("importId"), label)

    def push(self, config_id, csv_text, label):
        code, text = self._call("POST", f"/import/push/csv?configurationId={config_id}", self.import_key,
                                csv_text.encode("utf-8"), "text/csv")
        if code != 200:
            raise Stop(f"{label}: push HTTP {code}")
        return self._wait(json.loads(text or "{}").get("importId"), label)


class Run:
    def __init__(self, env, mode):
        self.env, self.mode = env, mode
        self.cfg = json.loads(env["PIGMENT_RUN_CONFIG"])
        self.p = Pigment(env)
        self.now = utcnow()
        self.today = self.now.date()
        self.monday = monday_of(self.today)
        self.loud = mode != "auto" or self.now.hour == ROLL_HOUR_UTC  # idle ticks never ping
        self.entry = None    # the run-log line of this run, once started
        self.recon = {}

    def ping(self, msg):
        log("ping", message=msg)
        url = self.env.get("PING_WEBHOOK_URL")
        if not url or not self.loud:
            return
        body = json.dumps({"text": f"Weekly snapshot {self.monday.isoformat()}: {msg} ({self.run_url()})"}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except (urllib.error.URLError, OSError):
            log("ping", result="webhook refused")

    def run_url(self):
        return "{}/{}/actions/runs/{}".format(self.env.get("GITHUB_SERVER_URL", ""),
                                             self.env.get("GITHUB_REPOSITORY", ""), self.env.get("GITHUB_RUN_ID", ""))

    def guard_write(self):
        w = quiet_window(utcnow())
        if w:
            raise Stop(f"deferred: {w[0]}-{w[1]}Z is a no-write window; re-run after it", 75)

    def read(self, key, label):
        return last_cell(self.p.export(self.cfg["views"][key], label))

    def trig(self):
        t = parse_trigger(self.read("trigger", "trigger"))
        log("trigger", paused=t["paused"], tried=t["tried"],
            slot=t["slot"].strftime("%Y-%m-%dT%HZ") if t["slot"] else None,
            hub_lag_days=(self.today - t["hub"]).days if t["hub"] else None)
        return t

    def state(self):
        word = state_word(self.read("state", "run state"))
        log("state", word=word)
        return word

    # -- the run log (Hub list, one line per fired slot or dispatch) ------------------------------
    def write_log(self, result, detail=""):
        e = self.entry
        recon = ", ".join(f"{k} {'unread' if v is None else v}" for k, v in sorted(self.recon.items()))
        self.guard_write()
        landed, _ = self.p.push(self.cfg["configs"]["run_log"],
                                "Name,Slot Date,Slot Hour,Run Seq,Result,Recon,Detail,Run Link\n"
                                + csv_line(e["name"], e["slot_date"], e["slot_hour"], e["seq"], result,
                                           clean(recon), clean(detail), self.run_url()), "run log")
        if landed < 1:
            raise Stop("the run-log push landed nothing")
        log("run log", result=result)

    def start(self, slot):
        started = utcnow()
        what = f"slot {slot.strftime('%Y-%m-%d %H')}Z" if slot else f"dispatch {self.mode}"
        self.entry = {"name": f"run {started.strftime('%Y-%m-%d %H:%M')}Z ({what})",
                      "slot_date": slot.date().isoformat() if slot else "",
                      "slot_hour": str(slot.hour) if slot else "",
                      "seq": str(int((started - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)).total_seconds()
                                     // 60))}
        self.write_log("STARTED")

    # -- the clock ------------------------------------------------------------------------------
    def roll(self, hub, write=True):
        if hub is None:
            raise Stop("Hub today is blank: a person reseeds it (runbook)", 2)
        lag = (self.today - hub).days
        log("clock", lag_days=lag)
        if lag < 0 or lag > MAX_CATCH_UP_DAYS:
            raise Stop(f"Hub today is {lag} days off the runner's date: a person reseeds it (runbook)", 2)
        if not write or lag == 0:
            return
        for i in range(lag):
            self.guard_write()
            self.p.trigger(self.cfg["configs"]["hub_today_roll"], f"roll {i + 1}/{lag}")
            time.sleep(30)
        if parse_date(self.read("hub_today", "hub today")) != self.today:
            raise Stop("Hub today did not reach the runner's date after the roll")

    # -- the capture ----------------------------------------------------------------------------
    def capture(self):
        word = self.state()
        if word == "PUBLISHED":
            return "NOTHING TO DO", "this week is already published"
        if word == "NO ROW":
            self.guard_write()
            lam = self.read("live_lam", "live boundary")
            if not lam:
                raise Stop("Live's Last Actuals Month read blank: nothing added", 2)
            day = self.monday.isoformat()
            landed, _ = self.p.push(self.cfg["configs"]["row_create"],
                                    "Name,Capture Date,Last Actuals Month,Notes\n"
                                    + csv_line(day, day, lam, "Weekly snapshot of Live (R-113), added by the weekly run"),
                                    "add row")
            if landed < 1:
                raise Stop("the row push landed nothing")
            time.sleep(60)
            word = self.state()
        if word != "OPEN":
            raise Stop(f"run state {word}: nothing captured; a person is needed (board 05)", 2)
        failed = []
        for c in self.cfg["configs"]["snap"]:
            self.guard_write()
            try:
                landed, skipped = self.p.trigger(c["id"], c["label"])
                if skipped or (c.get("expect_rows", True) and landed < 1):
                    failed.append(c["label"])
            except Stop as e:
                log("copy", label=c["label"], result=str(e))
                failed.append(c["label"])
        log("copies", fired=len(self.cfg["configs"]["snap"]), failed=len(failed))
        if failed:
            raise Stop("copies did not land: " + ", ".join(failed) + "; the row stays open this week")
        time.sleep(120)
        return self.publish()

    def recon_dirty(self):
        dirty = []
        for r in self.cfg["views"]["recon"]:
            v = self.read_recon(r)
            self.recon[r["label"]] = v
            log("recon", label=r["label"], off=v)
            if v != 0:
                dirty.append(r["label"])
        return dirty

    def read_recon(self, r):
        text = last_cell(self.p.export(r["view"], "recon " + r["label"]))
        try:
            return int(float(text)) if text else None
        except ValueError:
            return None

    def publish(self):
        if self.state() != "OPEN":
            raise Stop("the row is not open: not published", 2)
        dirty = self.recon_dirty()
        if dirty:
            raise Stop("recon off Live in " + ", ".join(dirty) + ": not published; the row stays open this week")
        if self.env.get("PUBLISH_ENABLED", "").lower() != "true" and self.mode != "publish":
            return "CAPTURED", "captured and reconciled, NOT published (hand-checked week): dispatch mode=publish"
        self.guard_write()
        day = self.monday.isoformat()
        self.p.push(self.cfg["configs"]["row_publish"], "Name,Snapshot Date\n" + csv_line(day, day), "publish")
        time.sleep(60)
        if self.state() != "PUBLISHED":
            raise Stop("the publish push ran but the run state is not PUBLISHED")
        for i, cid in enumerate(self.cfg["configs"].get("month_grain", [])):
            try:
                self.guard_write()
                self.p.trigger(cid, f"month-grain {i + 1}")
            except Stop as e:
                return "PUBLISHED", f"published; month-grain copy {i + 1} failed ({e})"
        return "PUBLISHED", "published"

    def fire(self, slot, work):
        """Start the run-log line, then do the work; the log always gets the outcome."""
        self.start(slot)
        if slot:
            t = self.trig()
            if not t["tried"] or t["slot"] != slot:
                raise Stop("the run log did not mark this slot as tried: not fired", 2)
        try:
            result, detail = work()
        except Stop as e:
            self.finish(WORDS.get(e.code, "FAILED"), str(e))
            raise
        except Exception as e:  # noqa: BLE001 - the class name only: a message could carry data
            self.finish("FAILED", f"unexpected error ({type(e).__name__})")
            raise
        self.finish(result, detail)
        self.ping(detail)
        return 1 if "failed" in detail else 0

    def finish(self, result, detail):
        try:
            self.write_log(result, detail)
        except Stop as e:
            self.ping(f"{result}: {detail}; the run log was not written ({e})")

    # -- modes ------------------------------------------------------------------------------------
    def tick(self):
        t = self.trig()
        due = (t["slot"] is not None and self.now >= t["slot"] and not t["tried"] and not t["paused"])
        if due:
            self.loud = True
        if t["hub"] != self.today and (self.now.hour >= ROLL_HOUR_UTC or due):
            self.roll(t["hub"])  # a failure pings at the ROLL_HOUR_UTC tick or a due slot, not hourly
        if t["paused"]:
            log("tick", result="paused in Pigment (the clock still rolls)")
            return 0
        if t["slot"] is None:
            log("tick", result="no scheduled run in Pigment")
            return 0
        if self.now < t["slot"]:
            log("tick", result="not due")
            return 0
        if t["tried"]:
            log("tick", result="this slot was already tried; set a new time to retry")
            return 0
        if not clear_for(self.now, CAPTURE_MINUTES):
            log("tick", result="due; waiting for an hour clear of the no-write windows")
            return 0
        return self.fire(t["slot"], self.capture)

    def check(self):
        t = self.trig()
        self.roll(t["hub"], write=False)
        self.state()
        for r in self.cfg["views"]["recon"]:
            log("recon", label=r["label"], off=self.read_recon(r))
        log("config", snap_configs=len(self.cfg["configs"]["snap"]),
            month_grain=len(self.cfg["configs"].get("month_grain", [])),
            run_log=bool(self.cfg["configs"].get("run_log")),
            publish_enabled=self.env.get("PUBLISH_ENABLED", "") == "true")
        return 0

    def go(self):
        if self.mode == "check":
            return self.check()
        if self.mode == "auto":
            return self.tick()
        if self.mode == "logtest":
            self.start(None)
            self.write_log("TEST", "run-log test line written by mode=logtest; nothing else ran")
            return 0
        t = self.trig()
        if self.mode in ("capture", "publish") and t["paused"]:
            raise Stop("paused in Pigment: unpause on the admin Frame first", 2)
        self.roll(t["hub"])
        if self.mode == "roll":
            return 0
        if not clear_for(utcnow(), CAPTURE_MINUTES if self.mode == "capture" else 10):
            raise Stop("a no-write window starts too soon; re-run after it", 75)
        return self.fire(None, self.capture if self.mode == "capture" else self.publish)


def selftest():
    utc = dt.timezone.utc
    assert monday_of(dt.date(2026, 10, 21)) == dt.date(2026, 10, 19)
    assert monday_of(dt.date(2026, 10, 19)) == dt.date(2026, 10, 19)
    assert quiet_window(dt.datetime(2026, 10, 19, 10, 0)) == ("09:45", "11:30")
    assert quiet_window(dt.datetime(2026, 10, 19, 8, 17)) is None
    assert clear_for(dt.datetime(2026, 10, 19, 8, 17, tzinfo=utc), 60)
    assert not clear_for(dt.datetime(2026, 10, 19, 9, 17, tzinfo=utc), 60)
    assert not clear_for(dt.datetime(2026, 10, 19, 4, 17, tzinfo=utc), 60)
    assert clear_for(dt.datetime(2026, 10, 19, 12, 17, tzinfo=utc), 60)
    assert clear_for(dt.datetime(2026, 10, 19, 23, 17, tzinfo=utc), 60)
    assert last_cell('"Hub today (UTC)"\n"10/19/2026 00:00:00 +00:00"\n') == "10/19/2026 00:00:00 +00:00"
    assert parse_date("10/19/2026 00:00:00 +00:00") == dt.date(2026, 10, 19)
    assert parse_date("2026-10-19") == dt.date(2026, 10, 19)
    t = parse_trigger("T1|20745|0|20745|8|0")
    assert t["hub"] == dt.date(2026, 10, 19) and t["slot"] == dt.datetime(2026, 10, 19, 8, tzinfo=utc)
    assert not t["paused"] and not t["tried"]
    t = parse_trigger("T1|20745|1||8|1")
    assert t["paused"] and t["tried"] and t["slot"] is None
    for bad in ("", "T0|1|0|1|8|0", "T1|x|0|1|8|0", "T1|1|0|1|24|0"):
        try:
            parse_trigger(bad)
            raise AssertionError(bad)
        except Stop:
            pass
    assert state_word('NO ROW - no row for this week yet') == "NO ROW"
    assert state_word(None) == "BLANK"
    assert csv_line("2026-10-19", "a, b") == '2026-10-19,"a, b"\n'
    assert clean("a | b\nc") == "a / b c"
    assert last_cell("only a header\n") is None
    selftest_ticks()
    print("selftest ok")
    return 0


def selftest_ticks():
    """The hourly decision against a fake Pigment: what fires, what waits, what never fires twice."""
    global utcnow, Pigment
    real = (utcnow, Pigment, time.sleep)
    utc, d = dt.timezone.utc, 20745  # 2026-10-19, a Monday

    def run(now, trig):
        calls, tr, states = [], {"v": trig}, ["NO ROW", "OPEN", "OPEN", "PUBLISHED"]

        class Fake:
            def __init__(self, env):
                pass

            def export(self, view_id, label):
                calls.append(("export", label))
                if label == "trigger":
                    return "h\n" + tr["v"] + "\n"
                if label == "run state":
                    return "h\n" + (states.pop(0) if len(states) > 1 else states[0]) + " - x\n"
                return "h\n" + {"live boundary": "Sep 26", "hub today": now.date().isoformat()}.get(label, "0") + "\n"

            def trigger(self, config_id, label):
                calls.append(("trigger", label))
                return 1, 0

            def push(self, config_id, csv_text, label):
                calls.append(("push", label, csv_text.splitlines()[1].split(",")[4] if label == "run log" else ""))
                if label == "run log" and ",STARTED," in csv_text:
                    tr["v"] = tr["v"][:-1] + "1"
                return 1, 0

        globals().update(utcnow=lambda: now, Pigment=Fake)
        time.sleep = lambda s: None
        cfg = {"views": {"trigger": 1, "state": 1, "live_lam": 1, "hub_today": 1,
                         "recon": [{"label": x, "view": 1} for x in ("01", "02", "03")]},
               "configs": {"hub_today_roll": 1, "row_create": 1, "row_publish": 1, "run_log": 1,
                           "snap": [{"label": "01-a", "id": 1}], "month_grain": [1]}}
        env = {"PIGMENT_IMPORT_KEY": "", "PIGMENT_EXPORT_KEY": "", "PIGMENT_RUN_CONFIG": json.dumps(cfg),
               "PUBLISH_ENABLED": "true"}
        return Run(env, "auto").go(), calls

    def at(h):
        return dt.datetime(2026, 10, 19, h, 17, tzinfo=utc)

    try:
        for name, now, trig, fires in [
                ("not due", at(7), f"T1|{d}|0|{d}|8|0", False), ("due", at(8), f"T1|{d}|0|{d}|8|0", True),
                ("tried", at(13), f"T1|{d}|0|{d}|8|1", False), ("paused", at(8), f"T1|{d}|1|{d}|8|0", False),
                ("no slot", at(8), f"T1|{d}|0||8|0", False), ("window", at(9), f"T1|{d}|0|{d}|9|0", False)]:
            rc, calls = run(now, trig)
            fired = ("trigger", "01-a") in calls
            assert rc == 0 and fired == fires, name
            if fires:
                logged = [c[2] for c in calls if c[:2] == ("push", "run log")]
                assert logged == ["STARTED", "PUBLISHED"], logged
                assert calls.index(("push", "run log", "STARTED")) < calls.index(("trigger", "01-a"))
        rc, calls = run(at(8), f"T1|{d - 1}|1|{d}|8|0")
        assert ("trigger", "roll 1/1") in calls and ("trigger", "01-a") not in calls  # paused: clock only
        rc, calls = run(at(3), f"T1|{d - 1}|0|{d + 7}|8|0")
        assert not any(c[0] in ("trigger", "push") for c in calls)  # no roll before ROLL_HOUR_UTC
    finally:
        utcnow, Pigment, time.sleep = real


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in ("auto", "roll", "capture", "publish", "check", "logtest"):
        print("mode must be auto, roll, capture, publish, check, logtest or selftest", file=sys.stderr)
        return 64
    if os.environ.get("RUN_ENABLED", "").lower() != "true" and mode != "check":
        log("run", result="disabled (repository variable RUN_ENABLED is not true)")
        return 0
    run = None
    try:
        run = Run(os.environ, mode)
        return run.go()
    except Stop as e:
        if run:
            run.ping(str(e))
        else:
            log("run", result=str(e))
        return e.code
    except Exception as e:  # noqa: BLE001 - the class name only: a message could carry data
        msg = f"unexpected error ({type(e).__name__})"
        if run:
            run.ping(msg)
        else:
            log("run", result=msg)
        return 1


if __name__ == "__main__":
    sys.exit(main())
