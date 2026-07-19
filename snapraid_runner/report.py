"""Structured run reports: rendering notifications and appending history.

Everything here is a pure function over a plain report dict (see
``snapraid_runner.init_report`` for its shape) so it can be tested without
ever invoking snapraid.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

# History record schema version. Bump when the JSONL layout changes so a
# future --report reader can tell old records apart.
SCHEMA = 1

RETENTION_DAYS = 90

# Fixed timestamp format so parsing stays trivial across Python 3.9+
# (fromisoformat only learned to accept a trailing "Z" in 3.11).
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_DIFF_KEYS = ("add", "remove", "move", "update")


def format_duration(seconds):
    """Human, phone-readable durations: 8s, 4m 12s, 1h 2m."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def diff_total(diff):
    """Total number of changes across all diff categories."""
    return sum(diff.get(k, 0) for k in _DIFF_KEYS)


def _diff_summary(diff):
    """One-line, symbol-decorated summary of the diff counts."""
    parts = []
    if diff.get("add"):
        parts.append(f"+{diff['add']} added")
    if diff.get("remove"):
        parts.append(f"−{diff['remove']} removed")
    if diff.get("update"):
        parts.append(f"~{diff['update']} updated")
    if diff.get("move"):
        parts.append(f"→{diff['move']} moved")
    return " · ".join(parts) if parts else "no changes"


def build_title(report):
    """Title carries the whole story; emoji status first."""
    if not report.get("success"):
        phase = report.get("failed_phase") or "run"
        return f"❌ SnapRAID: {phase} failed"

    diff = report["diff"]
    if diff_total(diff) == 0:
        return "✅ SnapRAID: no changes"

    words = [
        ("add", "added"),
        ("remove", "removed"),
        ("update", "updated"),
        ("move", "moved"),
    ]
    parts = [f"{diff[k]} {label}" for k, label in words if diff.get(k)]
    title = "✅ SnapRAID: " + " · ".join(parts)
    sync = report["phases"].get("sync")
    if sync:
        title += f" · synced in {format_duration(sync['duration'])}"
    return title


def _phase_line(name, phase, report):
    dur = format_duration(phase["duration"])
    if name == "diff":
        status = _diff_summary(report["diff"])
    elif not phase["success"]:
        status = "❌ failed"
    elif name == "scrub":
        plan = report.get("scrub_plan")
        status = f"✅ plan {plan}" if plan else "✅ completed"
    else:
        status = "✅ completed"
    return f"{name.capitalize():<6} {status}   ({dur})"


def build_body(report, short=True, full_log=None):
    """Body: error excerpt first (on failure), then one line per phase."""
    lines = []
    if not report.get("success") and report.get("error"):
        lines.append(report["error"].rstrip())
        lines.append("")
    for name in ("touch", "diff", "sync", "scrub"):
        phase = report["phases"].get(name)
        if phase:
            lines.append(_phase_line(name, phase, report))
    body = "\n".join(lines)
    if not short and full_log:
        body += "\n\n" + "-" * 40 + "\n" + full_log.rstrip()
    return body


def render(report, short=True, full_log=None):
    """Return the (title, body) pair for an apprise notification."""
    return build_title(report), build_body(report, short, full_log)


def should_suppress(report, quiet):
    """True when a quiet no-op success should not be notified."""
    return bool(quiet and report.get("success") and diff_total(report["diff"]) == 0)


def to_history_record(report, now=None):
    """Flatten a report into the JSONL history schema."""
    now = now or datetime.now(timezone.utc)
    phases = report.get("phases", {})
    return {
        "schema": SCHEMA,
        "timestamp": now.strftime(_TS_FORMAT),
        "success": bool(report.get("success")),
        "diff": {k: report["diff"].get(k, 0) for k in _DIFF_KEYS},
        "sync_ran": "sync" in phases,
        "scrub_ran": "scrub" in phases,
        "durations": {k: round(v["duration"], 3) for k, v in phases.items()},
        "failed_phase": report.get("failed_phase"),
        "error": (report.get("error_short") or "").strip(),
    }


def append_history(path, record, now=None):
    """Append one record and prune entries older than RETENTION_DAYS.

    The file is tiny, so we rewrite it wholesale via a temp file + atomic
    os.replace. Malformed existing lines are skipped rather than fatal.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)

    kept = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = datetime.strptime(entry["timestamp"], _TS_FORMAT).replace(
                        tzinfo=timezone.utc
                    )
                except (ValueError, KeyError, TypeError):
                    continue  # skip malformed, don't crash the run
                if ts >= cutoff:
                    kept.append(line)

    kept.append(json.dumps(record))

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------------
# Weekly report
#
# Everything below is for the scheduled `--report weekly` mode. It has two
# inputs, both parsed fail-soft: the JSONL run history and the text of
# `snapraid status`. Nothing here ever raises on malformed input -- a missing
# or garbled piece just drops out of the report.
# --------------------------------------------------------------------------

BAR_WIDTH = 10
_BAR_FULL = "▇"  # ▇
_BAR_EMPTY = "▁"  # ▁

# Only mention fragmentation when it's actually worth a look. Excess fragments
# below this are normal churn, not a maintenance signal. Deliberate constant:
# expose as a config option if anyone ever wants to tune it.
FRAGMENT_WARN = 100


def read_history(path, now=None, days=7):
    """Return history records from the last ``days`` days (newest last).

    Fail-soft: a missing/empty file or malformed lines yield an empty list.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    records = []
    if not path or not os.path.exists(path):
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.strptime(entry["timestamp"], _TS_FORMAT).replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, KeyError, TypeError):
                continue
            if ts >= cutoff:
                records.append(entry)
    return records


def summarize_history(records):
    """Aggregate a week's run records into a flat summary dict."""
    summary = {
        "runs": len(records),
        "succeeded": 0,
        "failed": 0,
        "diff": {k: 0 for k in _DIFF_KEYS},
        "sync_seconds": 0.0,
        "failures": [],  # [{"phase": str, "error": str}]
    }
    for rec in records:
        if rec.get("success"):
            summary["succeeded"] += 1
        else:
            summary["failed"] += 1
            summary["failures"].append(
                {
                    "phase": rec.get("failed_phase") or "run",
                    "error": (rec.get("error") or "").strip(),
                }
            )
        for k in _DIFF_KEYS:
            summary["diff"][k] += (rec.get("diff") or {}).get(k, 0)
        summary["sync_seconds"] += (rec.get("durations") or {}).get("sync", 0)
    return summary


def _num(token):
    """Parse a snapraid numeric cell; '-' (n/a) becomes None."""
    if token == "-":
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _pct(token):
    """Parse a snapraid 'NN%' cell; '-' becomes None."""
    if token.endswith("%"):
        token = token[:-1]
    if token == "-" or token == "":
        return None
    try:
        return int(token)
    except ValueError:
        return None


def _parse_disk_row(line):
    """Parse a per-disk status row into a dict, or None if it isn't one.

    Row shape (8 whitespace-separated columns):
      Files Fragmented Excess Wasted Used Free Use% Name
    """
    tok = line.split()
    if len(tok) != 8:
        return None
    try:
        files = int(tok[0])
        frag = int(tok[1])
        excess = int(tok[2])
    except ValueError:
        return None  # header row ("Files ... Name") lands here
    use = _pct(tok[6])
    if use is None and tok[6] != "-":
        return None
    return {
        "name": tok[7],
        "files": files,
        "frag_files": frag,
        "excess": excess,
        "wasted_gb": _num(tok[3]),
        "used_gb": _num(tok[4]),
        "free_gb": _num(tok[5]),
        "use_pct": use,
    }


def _parse_total_row(line):
    """Parse the totals row (7 columns, no disk name), or None."""
    tok = line.split()
    if len(tok) != 7:
        return None
    try:
        files = int(tok[0])
        frag = int(tok[1])
        excess = int(tok[2])
    except ValueError:
        return None
    return {
        "name": None,
        "files": files,
        "frag_files": frag,
        "excess": excess,
        "wasted_gb": _num(tok[3]),
        "used_gb": _num(tok[4]),
        "free_gb": _num(tok[5]),
        "use_pct": _pct(tok[6]),
    }


def parse_status(text):
    """Parse `snapraid status` output, fail-soft.

    ``text`` is the raw status output (str or list of lines). Every field is
    optional; anything unparseable is skipped. The ASCII histogram plot is
    ignored. Returns a dict; ``status_has_content`` tells the caller whether
    anything at all was extracted (so it can fall back to the raw text).
    """
    if isinstance(text, str):
        lines = text.splitlines()
    else:
        lines = [line.rstrip("\n") for line in text]

    result = {
        "disks": [],
        "total": None,
        "scrub": None,  # {"oldest": int, "median": int, "newest": int}
        "health": None,  # {"ok": bool, "text": str}
    }

    # Locate the dashed separator that ends the per-disk table.
    dash_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if len(s) >= 10 and set(s) == {"-"}:
            dash_idx = i
            break

    if dash_idx is not None:
        for line in lines[:dash_idx]:
            row = _parse_disk_row(line)
            if row:
                result["disks"].append(row)
        for line in lines[dash_idx + 1 :]:
            if line.strip():
                result["total"] = _parse_total_row(line)
                break

    for line in lines:
        m = re.search(
            r"oldest block was scrubbed (\d+) days? ago,"
            r" the median (\d+), the newest (\d+)",
            line,
        )
        if m:
            result["scrub"] = {
                "oldest": int(m.group(1)),
                "median": int(m.group(2)),
                "newest": int(m.group(3)),
            }
            break

    for line in lines:
        s = line.strip()
        if s.startswith("DANGER"):
            result["health"] = {"ok": False, "text": s}
            break
        if "No error detected" in s:
            result["health"] = {"ok": True, "text": s}
            break

    return result


def status_has_content(status):
    """True if parse_status extracted anything usable."""
    return bool(
        status.get("disks")
        or status.get("total")
        or status.get("scrub")
        or status.get("health")
    )


def format_free(gb):
    """Human free-space: 620 GB, 1.2 TB."""
    if gb is None:
        return ""
    if gb >= 1000:
        return f"{gb / 1000:.1f} TB"
    return f"{int(round(gb))} GB"


def _bar(use_pct):
    """A BAR_WIDTH-char unicode meter for a usage percentage (half-up)."""
    pct = 0 if use_pct is None else max(0, min(100, use_pct))
    filled = int(pct * BAR_WIDTH / 100 + 0.5)
    return _BAR_FULL * filled + _BAR_EMPTY * (BAR_WIDTH - filled)


def render_usage_graph(status):
    """Render the per-disk usage bar graph as a list of lines (or [])."""
    rows = list(status.get("disks") or [])
    total = status.get("total")
    if not rows and not total:
        return []

    names = [d["name"] for d in rows] + (["total"] if total else [])
    width = max(len(n) for n in names)

    lines = ["Disk usage"]
    for d in rows:
        line = f"{d['name']:<{width}} {_bar(d['use_pct'])}"
        if d["use_pct"] is not None:
            line += f"  {d['use_pct']}%"
        free = format_free(d.get("free_gb"))
        if free:
            line += f"  ({free} free)"
        lines.append(line)
    if total:
        line = f"{'total':<{width}} {_bar(total['use_pct'])}"
        if total["use_pct"] is not None:
            line += f"  {total['use_pct']}%"
        lines.append(line)
    return lines


def _scrub_age_line(scrub, warn_days):
    """'Scrub age: oldest 45d · median 8d · newest 0d', ⚠️ if too old."""
    line = (
        f"Scrub age: oldest {scrub['oldest']}d"
        f" · median {scrub['median']}d"
        f" · newest {scrub['newest']}d"
    )
    if warn_days and scrub["oldest"] > warn_days:
        line = "⚠️ " + line
    return line


def scrub_age_warned(status, warn_days):
    """True if the oldest-scrub age exceeds the (nonzero) warning threshold."""
    scrub = status.get("scrub")
    return bool(scrub and warn_days and scrub["oldest"] > warn_days)


def build_weekly_title(summary, warned):
    """📊 when healthy, ⚠️ when any run failed or a warning fired."""
    runs = summary["runs"]
    failed = summary["failed"]
    emoji = "⚠️" if (failed or warned) else "\U0001f4ca"
    if runs == 0:
        text = "no run history"
    elif failed == 0:
        text = f"{runs} runs, all OK"
    else:
        text = f"{summary['succeeded']}/{runs} runs OK"
    return f"{emoji} SnapRAID weekly: {text}"


def build_weekly_body(summary, status, scrub_age_warning=30, raw_status=None):
    """Assemble the plain-text (unicode) weekly report body."""
    sections = []

    # 1. Week summary
    week = ["Week in review (last 7 days)"]
    if summary["runs"] == 0:
        week.append("No run history available.")
    else:
        parts = [f"{summary['runs']} runs", f"{summary['succeeded']} OK"]
        if summary["failed"]:
            parts.append(f"{summary['failed']} failed")
        week.append(" · ".join(parts))
        week.append("Files churned: " + _diff_summary(summary["diff"]))
        if summary["sync_seconds"]:
            week.append("Total sync time: " + format_duration(summary["sync_seconds"]))
    sections.append("\n".join(week))

    # 2. Failures
    if summary["failures"]:
        fail_lines = ["Failures"]
        for f in summary["failures"]:
            err = f["error"]
            fail_lines.append(
                f"• {f['phase']}: {err}" if err else f"• {f['phase']} failed"
            )
        sections.append("\n".join(fail_lines))

    # 3. Disk usage graph
    graph = render_usage_graph(status)
    if graph:
        sections.append("\n".join(graph))

    # 4. Scrub age (+ optional fragmentation)
    tail = []
    if status.get("scrub"):
        tail.append(_scrub_age_line(status["scrub"], scrub_age_warning))
    total = status.get("total")
    if total and (total.get("excess") or 0) >= FRAGMENT_WARN:
        tail.append(
            f"Fragmentation: {total['excess']} excess fragments"
            f" across {total['frag_files']} files"
        )
    if tail:
        sections.append("\n".join(tail))

    # 5. Health verdict (always, when known)
    health = status.get("health")
    if health:
        if health["ok"]:
            sections.append("✅ No errors detected")
        else:
            sections.append("‼️ " + health["text"])

    # Fall back to raw status only if parsing yielded nothing at all.
    if raw_status and not status_has_content(status):
        sections.append("-" * 40 + "\n" + raw_status.rstrip())

    return "\n\n".join(sections)


def render_weekly(records, status, scrub_age_warning=30, raw_status=None):
    """Return the (title, body) pair for the weekly notification."""
    summary = summarize_history(records)
    warned = scrub_age_warned(status, scrub_age_warning)
    if status.get("health") and not status["health"]["ok"]:
        warned = True
    title = build_weekly_title(summary, warned)
    body = build_weekly_body(summary, status, scrub_age_warning, raw_status)
    return title, body
