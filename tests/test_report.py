"""Unit tests for snapraid_runner.report (stdlib unittest, no pytest)."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO

import snapraid_runner
from snapraid_runner import report


def make_report(**overrides):
    """A default success-with-changes report, overridable per test."""
    base = {
        "start": 0.0,
        "diff": {"add": 12, "remove": 3, "move": 2, "update": 5},
        "phases": {
            "diff": {"duration": 8.0, "success": True},
            "sync": {"duration": 252.0, "success": True},
        },
        "scrub_plan": "",
        "success": True,
        "failed_phase": None,
        "error": "",
        "error_short": "",
    }
    base.update(overrides)
    return base


class FormatDurationTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(report.format_duration(8), "8s")
        self.assertEqual(report.format_duration(0), "0s")
        self.assertEqual(report.format_duration(59.4), "59s")

    def test_minutes(self):
        self.assertEqual(report.format_duration(252), "4m 12s")
        self.assertEqual(report.format_duration(120), "2m")
        self.assertEqual(report.format_duration(483), "8m 3s")

    def test_hours(self):
        self.assertEqual(report.format_duration(3720), "1h 2m")
        self.assertEqual(report.format_duration(3600), "1h")


class TitleTest(unittest.TestCase):
    def test_success_with_changes(self):
        title = report.build_title(make_report())
        self.assertEqual(
            title,
            "✅ SnapRAID: 12 added · 3 removed · 5 updated · 2 moved "
            "· synced in 4m 12s")

    def test_success_with_changes_partial(self):
        r = make_report(diff={"add": 12, "remove": 3, "move": 0, "update": 0})
        title = report.build_title(r)
        self.assertEqual(
            title, "✅ SnapRAID: 12 added · 3 removed · synced in 4m 12s")

    def test_no_changes(self):
        r = make_report(
            diff={"add": 0, "remove": 0, "move": 0, "update": 0},
            phases={"diff": {"duration": 8.0, "success": True}})
        self.assertEqual(report.build_title(r), "✅ SnapRAID: no changes")

    def test_failure_names_phase(self):
        r = make_report(success=False, failed_phase="sync")
        self.assertEqual(report.build_title(r), "❌ SnapRAID: sync failed")

    def test_failure_without_phase(self):
        r = make_report(success=False, failed_phase=None)
        self.assertEqual(report.build_title(r), "❌ SnapRAID: run failed")


class BodyTest(unittest.TestCase):
    def test_body_with_changes(self):
        body = report.build_body(make_report())
        self.assertEqual(
            body,
            "Diff   +12 added · −3 removed · ~5 updated · →2 moved   (8s)\n"
            "Sync   ✅ completed   (4m 12s)")

    def test_body_scrub_plan(self):
        r = make_report(
            scrub_plan="12%",
            phases={
                "diff": {"duration": 8.0, "success": True},
                "sync": {"duration": 252.0, "success": True},
                "scrub": {"duration": 483.0, "success": True},
            })
        body = report.build_body(r)
        self.assertIn("Scrub  ✅ plan 12%   (8m 3s)", body)

    def test_body_no_changes(self):
        r = make_report(
            diff={"add": 0, "remove": 0, "move": 0, "update": 0},
            phases={"diff": {"duration": 8.0, "success": True}})
        self.assertEqual(report.build_body(r), "Diff   no changes   (8s)")

    def test_failure_leads_with_excerpt(self):
        r = make_report(
            success=False, failed_phase="sync",
            error="snapraid: fatal disk error\nCommand returned status 1",
            phases={
                "diff": {"duration": 8.0, "success": True},
                "sync": {"duration": 3.0, "success": False},
            })
        body = report.build_body(r)
        # Error excerpt must lead the body, before the phase summary.
        self.assertTrue(body.startswith("snapraid: fatal disk error"))
        self.assertIn("Sync   ❌ failed   (3s)", body)
        self.assertLess(body.index("fatal disk error"), body.index("Sync"))

    def test_short_false_appends_full_log(self):
        body = report.build_body(
            make_report(), short=False, full_log="line1\nline2\n")
        self.assertIn("Sync   ✅ completed", body)
        self.assertIn("----", body)
        self.assertTrue(body.rstrip().endswith("line2"))

    def test_short_true_omits_full_log(self):
        body = report.build_body(
            make_report(), short=True, full_log="secret log")
        self.assertNotIn("secret log", body)


class SuppressTest(unittest.TestCase):
    def test_suppress_quiet_noop_success(self):
        r = make_report(
            diff={"add": 0, "remove": 0, "move": 0, "update": 0})
        self.assertTrue(report.should_suppress(r, quiet=True))

    def test_no_suppress_when_changes(self):
        self.assertFalse(report.should_suppress(make_report(), quiet=True))

    def test_no_suppress_when_not_quiet(self):
        r = make_report(
            diff={"add": 0, "remove": 0, "move": 0, "update": 0})
        self.assertFalse(report.should_suppress(r, quiet=False))

    def test_never_suppress_failures(self):
        r = make_report(
            success=False,
            diff={"add": 0, "remove": 0, "move": 0, "update": 0})
        self.assertFalse(report.should_suppress(r, quiet=True))


class HistoryTest(unittest.TestCase):
    def test_record_schema(self):
        r = make_report(scrub_plan="12%")
        r["phases"]["scrub"] = {"duration": 483.0, "success": True}
        now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
        rec = report.to_history_record(r, now=now)
        self.assertEqual(rec["schema"], 1)
        self.assertEqual(rec["timestamp"], "2026-07-19T12:00:00Z")
        self.assertTrue(rec["success"])
        self.assertEqual(rec["diff"], {"add": 12, "remove": 3,
                                       "move": 2, "update": 5})
        self.assertTrue(rec["sync_ran"])
        self.assertTrue(rec["scrub_ran"])
        self.assertEqual(rec["durations"]["sync"], 252.0)
        self.assertIsNone(rec["failed_phase"])

    def test_failure_record(self):
        r = make_report(
            success=False, failed_phase="sync",
            error_short="disk error",
            phases={"diff": {"duration": 8.0, "success": True}})
        rec = report.to_history_record(r)
        self.assertFalse(rec["success"])
        self.assertEqual(rec["failed_phase"], "sync")
        self.assertEqual(rec["error"], "disk error")
        self.assertFalse(rec["sync_ran"])

    def test_append_creates_and_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.jsonl")
            ts = self._now().strftime(report._TS_FORMAT)
            report.append_history(path, {"timestamp": ts, "n": 1},
                                  now=self._now())
            report.append_history(path, {"timestamp": ts, "n": 2},
                                  now=self._now())
            with open(path) as f:
                lines = [json.loads(x) for x in f if x.strip()]
            self.assertEqual([e["n"] for e in lines], [1, 2])

    def test_append_prunes_old_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.jsonl")
            now = datetime(2026, 7, 19, tzinfo=timezone.utc)
            old = (now - timedelta(days=200)).strftime(report._TS_FORMAT)
            recent = (now - timedelta(days=10)).strftime(report._TS_FORMAT)
            with open(path, "w") as f:
                f.write(json.dumps({"timestamp": old, "n": "old"}) + "\n")
                f.write(json.dumps({"timestamp": recent, "n": "recent"}) + "\n")
            report.append_history(
                path, {"timestamp": now.strftime(report._TS_FORMAT), "n": "new"},
                now=now)
            with open(path) as f:
                kept = [json.loads(x)["n"] for x in f if x.strip()]
            self.assertEqual(kept, ["recent", "new"])

    def test_append_skips_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.jsonl")
            now = datetime(2026, 7, 19, tzinfo=timezone.utc)
            good = json.dumps(
                {"timestamp": now.strftime(report._TS_FORMAT), "n": "good"})
            with open(path, "w") as f:
                f.write("not json at all\n")
                f.write(json.dumps({"no": "timestamp"}) + "\n")
                f.write(good + "\n")
            report.append_history(
                path, {"timestamp": now.strftime(report._TS_FORMAT), "n": "new"},
                now=now)
            with open(path) as f:
                kept = [json.loads(x)["n"] for x in f if x.strip()]
            self.assertEqual(kept, ["good", "new"])

    def _now(self):
        return datetime(2026, 7, 19, tzinfo=timezone.utc)


# A realistic v14 `snapraid status` fixture (real column shape, bigger
# numbers). Note: Wasted can be "-" (n/a) or negative; the histogram plot in
# the middle must be ignored.
STATUS_FIXTURE = """\
Self-test...
Loading state from /mnt/d1/.content...
Using 421 MiB of memory for the file-system.
SnapRAID status report:

   Files Fragmented Excess  Wasted  Used    Free  Use Name
            Files  Fragments  GB      GB      GB
  102842      523     1289    12.4    6821    3204  68% d1
   88121       12       34     0.0    4210    5812  42% d2
   45012        0        0       -    1988    1620  55% d3
 --------------------------------------------------------------------------
  235975      535     1323    12.4   13019   10636  55%


100%|o
 50%|o
  0%|o_____________________________________________________________________
     45                   days ago of the last scrub/sync                 0

The oldest block was scrubbed 45 days ago, the median 8, the newest 0.

No sync is in progress.
12% of the array is not scrubbed.
No file has a zero sub-second timestamp.
No rehash is in progress or needed.
No error detected.
"""


class ParseStatusTest(unittest.TestCase):
    def test_disk_rows(self):
        s = report.parse_status(STATUS_FIXTURE)
        self.assertEqual([d["name"] for d in s["disks"]], ["d1", "d2", "d3"])
        d1 = s["disks"][0]
        self.assertEqual(d1["files"], 102842)
        self.assertEqual(d1["frag_files"], 523)
        self.assertEqual(d1["excess"], 1289)
        self.assertEqual(d1["wasted_gb"], 12.4)
        self.assertEqual(d1["used_gb"], 6821.0)
        self.assertEqual(d1["free_gb"], 3204.0)
        self.assertEqual(d1["use_pct"], 68)

    def test_wasted_dash_is_none(self):
        s = report.parse_status(STATUS_FIXTURE)
        self.assertIsNone(s["disks"][2]["wasted_gb"])  # d3 wasted "-"

    def test_totals_row(self):
        s = report.parse_status(STATUS_FIXTURE)
        self.assertIsNotNone(s["total"])
        self.assertIsNone(s["total"]["name"])
        self.assertEqual(s["total"]["files"], 235975)
        self.assertEqual(s["total"]["excess"], 1323)
        self.assertEqual(s["total"]["use_pct"], 55)

    def test_scrub_line(self):
        s = report.parse_status(STATUS_FIXTURE)
        self.assertEqual(s["scrub"], {"oldest": 45, "median": 8, "newest": 0})

    def test_health_ok(self):
        s = report.parse_status(STATUS_FIXTURE)
        self.assertTrue(s["health"]["ok"])

    def test_health_danger(self):
        danger = STATUS_FIXTURE.replace(
            "No error detected.",
            "DANGER! In the array there are 3 errors!")
        s = report.parse_status(danger)
        self.assertFalse(s["health"]["ok"])
        self.assertIn("DANGER", s["health"]["text"])

    def test_has_content(self):
        self.assertTrue(report.status_has_content(
            report.parse_status(STATUS_FIXTURE)))

    def test_empty_fails_soft(self):
        s = report.parse_status("")
        self.assertEqual(s["disks"], [])
        self.assertIsNone(s["total"])
        self.assertIsNone(s["scrub"])
        self.assertIsNone(s["health"])
        self.assertFalse(report.status_has_content(s))

    def test_garbage_fails_soft(self):
        s = report.parse_status("total nonsense\n???\n-- not a table --\n")
        self.assertFalse(report.status_has_content(s))

    def test_ignores_histogram_plot(self):
        # The plot's "0%|o___" and "50%|o" lines must not become disk rows.
        s = report.parse_status(STATUS_FIXTURE)
        self.assertEqual(len(s["disks"]), 3)


class BarTest(unittest.TestCase):
    def test_widths_and_rounding(self):
        self.assertEqual(report._bar(0), "▁" * 10)
        self.assertEqual(report._bar(100), "▇" * 10)
        self.assertEqual(report._bar(68), "▇" * 7 + "▁" * 3)   # 6.8 -> 7
        self.assertEqual(report._bar(42), "▇" * 4 + "▁" * 6)   # 4.2 -> 4
        self.assertEqual(report._bar(55), "▇" * 6 + "▁" * 4)   # 5.5 -> 6 (half-up)
        self.assertEqual(report._bar(4), "▁" * 10)             # 0.4 -> 0
        self.assertEqual(report._bar(5), "▇" * 1 + "▁" * 9)    # 0.5 -> 1

    def test_clamps_out_of_range(self):
        self.assertEqual(report._bar(None), "▁" * 10)
        self.assertEqual(report._bar(150), "▇" * 10)
        self.assertEqual(report._bar(-5), "▁" * 10)

    def test_format_free(self):
        self.assertEqual(report.format_free(198), "198 GB")
        self.assertEqual(report.format_free(1620), "1.6 TB")
        self.assertEqual(report.format_free(3204), "3.2 TB")
        self.assertEqual(report.format_free(None), "")

    def test_usage_graph_alignment(self):
        s = report.parse_status(STATUS_FIXTURE)
        lines = report.render_usage_graph(s)
        self.assertEqual(lines[0], "Disk usage")
        # Names padded to len("total") == 5; free space shown for disks.
        self.assertEqual(
            lines[1], "d1    " + "▇" * 7 + "▁" * 3 + "  68%  (3.2 TB free)")
        self.assertTrue(lines[-1].startswith("total "))
        self.assertIn("55%", lines[-1])
        self.assertNotIn("free", lines[-1])  # totals row has no free column


def _hist(now, days_ago, success=True, diff=None, sync=None,
          failed_phase=None, error=""):
    ts = (now - timedelta(days=days_ago)).strftime(report._TS_FORMAT)
    rec = {
        "schema": 1, "timestamp": ts, "success": success,
        "diff": diff or {"add": 0, "remove": 0, "move": 0, "update": 0},
        "sync_ran": sync is not None, "scrub_ran": False,
        "durations": {"sync": sync} if sync is not None else {},
        "failed_phase": failed_phase, "error": error,
    }
    return rec


class WeekAggregationTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

    def test_aggregates_counts_and_diff(self):
        recs = [
            _hist(self.now, 1, diff={"add": 5, "remove": 1,
                                     "move": 0, "update": 2}, sync=100),
            _hist(self.now, 2, diff={"add": 3, "remove": 0,
                                     "move": 1, "update": 0}, sync=50),
            _hist(self.now, 3, success=False,
                  failed_phase="sync", error="disk error"),
        ]
        s = report.summarize_history(recs)
        self.assertEqual(s["runs"], 3)
        self.assertEqual(s["succeeded"], 2)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["diff"], {"add": 8, "remove": 1,
                                     "move": 1, "update": 2})
        self.assertEqual(s["sync_seconds"], 150)
        self.assertEqual(s["failures"],
                         [{"phase": "sync", "error": "disk error"}])

    def test_empty_history(self):
        s = report.summarize_history([])
        self.assertEqual(s["runs"], 0)
        self.assertEqual(s["failures"], [])

    def test_read_history_excludes_old(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps(_hist(self.now, 1)) + "\n")     # in window
                f.write(json.dumps(_hist(self.now, 8)) + "\n")     # too old
                f.write("garbage not json\n")                      # malformed
            recs = report.read_history(path, now=self.now, days=7)
            self.assertEqual(len(recs), 1)

    def test_read_history_missing_file(self):
        self.assertEqual(report.read_history("", now=self.now), [])
        self.assertEqual(
            report.read_history("/no/such/file", now=self.now), [])


class WeeklyTitleTest(unittest.TestCase):
    def test_all_ok(self):
        summary = report.summarize_history(
            [_hist(datetime(2026, 7, 19, tzinfo=timezone.utc), i)
             for i in range(7)])
        self.assertEqual(
            report.build_weekly_title(summary, warned=False),
            "📊 SnapRAID weekly: 7 runs, all OK")

    def test_some_failed(self):
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        recs = [_hist(now, i) for i in range(6)]
        recs.append(_hist(now, 6, success=False, failed_phase="sync"))
        summary = report.summarize_history(recs)
        self.assertEqual(
            report.build_weekly_title(summary, warned=False),
            "⚠️ SnapRAID weekly: 6/7 runs OK")

    def test_warning_flips_emoji(self):
        summary = report.summarize_history(
            [_hist(datetime(2026, 7, 19, tzinfo=timezone.utc), 0)])
        # All runs OK but a scrub-age warning was raised -> ⚠️ prefix.
        self.assertTrue(
            report.build_weekly_title(summary, warned=True).startswith("⚠️"))

    def test_no_history(self):
        summary = report.summarize_history([])
        self.assertEqual(
            report.build_weekly_title(summary, warned=False),
            "📊 SnapRAID weekly: no run history")


class WeeklyBodyTest(unittest.TestCase):
    def test_scrub_age_warning_prefix(self):
        s = report.parse_status(STATUS_FIXTURE)  # oldest 45d
        body = report.build_weekly_body(
            report.summarize_history([]), s, scrub_age_warning=30)
        self.assertIn("⚠️ Scrub age: oldest 45d · median 8d · newest 0d", body)

    def test_scrub_age_no_warning_when_under_threshold(self):
        s = report.parse_status(STATUS_FIXTURE)
        body = report.build_weekly_body(
            report.summarize_history([]), s, scrub_age_warning=90)
        self.assertIn("Scrub age: oldest 45d", body)
        self.assertNotIn("⚠️ Scrub age", body)

    def test_scrub_age_disabled(self):
        s = report.parse_status(STATUS_FIXTURE)
        body = report.build_weekly_body(
            report.summarize_history([]), s, scrub_age_warning=0)
        self.assertNotIn("⚠️", body)

    def test_health_and_fragmentation(self):
        s = report.parse_status(STATUS_FIXTURE)
        body = report.build_weekly_body(
            report.summarize_history([]), s, scrub_age_warning=30)
        self.assertIn("✅ No errors detected", body)
        self.assertIn("Fragmentation: 1323 excess fragments", body)

    def test_no_history_line(self):
        body = report.build_weekly_body(
            report.summarize_history([]), report.parse_status(""),
            scrub_age_warning=30)
        self.assertIn("No run history available.", body)

    def test_raw_status_fallback_only_when_empty_parse(self):
        empty = report.parse_status("weird unparseable output here")
        body = report.build_weekly_body(
            report.summarize_history([]), empty,
            raw_status="weird unparseable output here")
        self.assertIn("weird unparseable output here", body)


def _fake_apprise(calls):
    """A stand-in `apprise` module recording every notify() call.

    Because the import in apprise_send is lazy, injecting this into
    sys.modules is enough. notify() reads any attachment while it still
    exists (send cleans it up afterwards) so tests can assert on its content.
    """
    mod = types.ModuleType("apprise")

    class Apprise:
        def add(self, *a, **k):
            pass

        def notify(self, **kwargs):
            rec = dict(kwargs)
            attach = kwargs.get("attach")
            if attach and os.path.exists(attach):
                rec["_basename"] = os.path.basename(attach)
                with open(attach, encoding="utf-8") as f:
                    rec["_content"] = f.read()
            calls.append(rec)
            return True

    class AppriseConfig:
        def add(self, *a, **k):
            pass

    mod.Apprise = Apprise
    mod.AppriseConfig = AppriseConfig
    return mod


class NotificationAttachmentTest(unittest.TestCase):
    """send_notification: failures attach the full run log, success doesn't."""

    def setUp(self):
        self.sr = snapraid_runner
        self._saved = {k: getattr(self.sr, k) for k in
                       ("config", "run_report", "notification_log",
                        "full_capture")}
        self._saved_apprise = sys.modules.get("apprise")

        self.calls = []
        sys.modules["apprise"] = _fake_apprise(self.calls)

        self.sr.config = {
            "notification": {"short": True, "enabled": True,
                             "config": "/dev/null"},
        }
        self.sr.notification_log = StringIO("high level log\n")
        self.sr.full_capture = StringIO(
            "2026-07-19 [OUTPUT] scanning disk d1: 102842 files\n"
            "2026-07-19 [OUTERR] snapraid: fatal: unable to read parity\n")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.sr, k, v)
        if self._saved_apprise is None:
            sys.modules.pop("apprise", None)
        else:
            sys.modules["apprise"] = self._saved_apprise

    def test_failure_attaches_full_log(self):
        self.sr.run_report = make_report(
            success=False, failed_phase="sync",
            error="snapraid: fatal: unable to read parity")
        self.sr.send_notification(False)

        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertIsNotNone(call["attach"])
        self.assertRegex(
            call["_basename"], r"^snapraid-runner-\d{8}-\d{6}\.log$")
        # The full OUTPUT/OUTERR detail lives in the attachment, not the body.
        self.assertIn("[OUTPUT] scanning disk d1", call["_content"])
        self.assertIn("[OUTERR] snapraid: fatal", call["_content"])
        # The temp dir is cleaned up once the notification has been sent.
        self.assertFalse(os.path.exists(call["attach"]))
        self.assertFalse(os.path.exists(os.path.dirname(call["attach"])))

    def test_success_has_no_attachment(self):
        self.sr.run_report = make_report(success=True)
        self.sr.send_notification(True)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0].get("attach"))

    def test_attachment_write_failure_still_sends(self):
        # If building the attachment fails, the error notification still goes
        # out (without the attachment) rather than being lost.
        self.sr.run_report = make_report(
            success=False, failed_phase="sync", error="boom")

        def boom(*a, **k):
            raise OSError("no tmp")

        orig = tempfile.mkdtemp
        tempfile.mkdtemp = boom
        try:
            self.sr.send_notification(False)
        finally:
            tempfile.mkdtemp = orig

        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0].get("attach"))


if __name__ == "__main__":
    unittest.main()
