#!/usr/bin/env python3
import argparse
import configparser
import logging
import logging.handlers
import os.path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter, defaultdict
from io import StringIO

from . import report as report_mod

# Global variables
config = None
notification_log = None
# Always-full-detail capture of the run log, used only as the source for the
# failure-attachment file (see setup_logger / send_notification).
full_capture = None
# Structured record of the current run (see init_report for its shape).
run_report = None


def init_report():
    global run_report
    run_report = {
        "start": time.time(),
        "diff": {"add": 0, "remove": 0, "move": 0, "update": 0},
        # Ordered {name: {"duration": float, "success": bool}} for phases
        # that actually ran (touch, diff, sync, scrub).
        "phases": {},
        "scrub_plan": "",
        "success": None,
        "failed_phase": None,
        "error": "",  # multi-line excerpt for the notification body
        "error_short": "",  # one-line summary for the history record
    }


def tee_log(infile, out_lines, log_level):
    """
    Create a thread that saves all the output on infile to out_lines and
    logs every line with log_level
    """

    def tee_thread():
        for line in iter(infile.readline, ""):
            logging.log(log_level, line.rstrip())
            out_lines.append(line)
        infile.close()

    t = threading.Thread(target=tee_thread)
    t.daemon = True
    t.start()
    return t


def snapraid_command(command, args=None, *, allow_statuscodes=None):
    """
    Run snapraid command
    Raises subprocess.CalledProcessError if errorlevel != 0
    """
    if args is None:
        args = {}
    if allow_statuscodes is None:
        allow_statuscodes = []
    arguments = ["--conf", config["snapraid"]["config"], "--quiet"]
    for k, v in args.items():
        arguments.extend(["--" + k, str(v)])
    p = subprocess.Popen(
        [config["snapraid"]["executable"], command] + arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Snapraid always outputs utf-8 on windows. On linux, utf-8
        # also seems a sensible assumption.
        encoding="utf-8",
        errors="replace",
    )
    out = []
    err = []
    threads = [
        tee_log(p.stdout, out, logging.OUTPUT),
        tee_log(p.stderr, err, logging.OUTERR),
    ]
    for t in threads:
        t.join()
    ret = p.wait()
    # sleep for a while to prevent output mixup
    time.sleep(0.3)
    if ret == 0 or ret in allow_statuscodes:
        return out
    else:
        e = subprocess.CalledProcessError(ret, f"snapraid {command}")
        # Attach the tail so a failure notification can lead with the actual
        # error (stderr preferred, falling back to stdout) instead of it
        # being buried in the full log.
        tail = err or out
        e.output_tail = [line.rstrip() for line in tail[-15:]]
        raise e


def notifications_configured():
    """True when apprise notifications are enabled and have a config file."""
    return bool(config["notification"]["enabled"] and config["notification"]["config"])


def apprise_send(title, body, attach=None):
    """Send a single notification through the configured apprise services.

    ``attach`` is an optional file path passed straight to apprise; targets
    that don't support attachments simply ignore it.
    """
    import apprise

    logging.info("sending msg")
    # Create an Apprise instance
    apobj = apprise.Apprise()

    # Create an Config instance
    apprise_config = apprise.AppriseConfig()

    apprise_config_file = config["notification"]["config"]
    # Add a configuration source:
    apprise_config.add(apprise_config_file)
    # Make sure to add our config into our apprise object
    apobj.add(apprise_config)

    # Then notify these services any time you desire. The below would
    # notify all of the services that have not been bound to any specific
    # tag.
    apobj.notify(
        body=body,
        title=title,
        attach=attach,
    )


def send_notification(success):
    short = config["notification"]["short"]
    full_log = notification_log.getvalue() if notification_log else None
    message_title, message_body = report_mod.render(
        run_report, short=short, full_log=full_log
    )

    if success:
        apprise_send(message_title, message_body)
        return

    # On failure, attach the complete run log as a file so the full detail is
    # available even when `short` trimmed it from the body. Writing the
    # attachment must never cost us the error notification itself: if anything
    # goes wrong we log a warning and send without it.
    tmpdir = None
    attach = None
    try:
        try:
            tmpdir = tempfile.mkdtemp()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            attach = os.path.join(tmpdir, f"snapraid-runner-{stamp}.log")
            with open(attach, "w", encoding="utf-8") as f:
                f.write(full_capture.getvalue() if full_capture else "")
        except Exception:
            logging.warning(
                "Failed to write run log attachment; sending without it", exc_info=True
            )
            attach = None
        apprise_send(message_title, message_body, attach=attach)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _should_notify(is_success):
    if not config["notification"]["enabled"]:
        return False
    if ("error", "success")[is_success] not in config["notification"]["sendon"]:
        return False
    # quiet: never suppress failures, only no-op successes.
    if report_mod.should_suppress(run_report, config["notification"]["quiet"]):
        return False
    return True


def write_history():
    path = config["history"]["file"]
    if not path:
        return
    # A history write must never fail the run.
    try:
        record = report_mod.to_history_record(run_report)
        report_mod.append_history(path, record)
    except Exception:
        logging.warning("Failed to write run history", exc_info=True)


def finish(is_success):
    run_report["success"] = is_success
    write_history()
    if _should_notify(is_success):
        try:
            send_notification(is_success)
        except Exception:
            logging.exception("Failed to send notification")
    if is_success:
        logging.info("Run finished successfully")
    else:
        logging.error("Run failed")
    sys.exit(0 if is_success else 1)


def fail(phase, message, tail=None):
    """Record a phase failure into the run_report and finish the run."""
    run_report["failed_phase"] = phase
    lines = list(tail or [])
    if message:
        lines.append(message)
    run_report["error"] = "\n".join(line for line in lines if line.strip())
    run_report["error_short"] = (
        message.strip().splitlines()[0]
        if message and message.strip()
        else f"{phase} failed"
    )
    finish(False)


def load_config(args):
    global config
    parser = configparser.RawConfigParser()
    parser.read(args.conf)
    sections = ["snapraid", "logging", "scrub", "notification", "history", "report"]
    config = dict((x, defaultdict(lambda: "")) for x in sections)
    for section in parser.sections():
        for k, v in parser.items(section):
            config[section][k] = v.strip()

    int_options = [
        ("snapraid", "deletethreshold"),
        ("logging", "maxsize"),
        ("scrub", "older-than"),
    ]
    for section, option in int_options:
        try:
            config[section][option] = int(config[section][option])
        except ValueError:
            config[section][option] = 0

    # scrub-age-warning: unset means the default (30 days), not disabled.
    # An explicit 0 disables the warning; a bad value also falls back to 30.
    raw_warn = config["report"]["scrub-age-warning"]
    if raw_warn == "":
        config["report"]["scrub-age-warning"] = 30
    else:
        try:
            config["report"]["scrub-age-warning"] = int(raw_warn)
        except ValueError:
            config["report"]["scrub-age-warning"] = 30

    config["scrub"]["enabled"] = config["scrub"]["enabled"].lower() == "true"
    config["snapraid"]["touch"] = config["snapraid"]["touch"].lower() == "true"
    config["notification"]["short"] = config["notification"]["short"].lower() == "true"
    config["notification"]["quiet"] = config["notification"]["quiet"].lower() == "true"

    if config["notification"]["enabled"] == "":
        # Backward-compat: older configs have no `enabled` key. Treat a
        # configured `sendon` as opting in, rather than silently disabling
        # notifications for everyone who never set the key.
        config["notification"]["enabled"] = bool(config["notification"]["sendon"])
    else:
        config["notification"]["enabled"] = (
            config["notification"]["enabled"].lower() == "true"
        )

    # Migration
    if config["scrub"]["percentage"]:
        config["scrub"]["plan"] = config["scrub"]["percentage"]

    if args.scrub is not None:
        config["scrub"]["enabled"] = args.scrub

    if args.ignore_deletethreshold:
        config["snapraid"]["deletethreshold"] = -1


def setup_logger():
    log_format = logging.Formatter("%(asctime)s [%(levelname)-6.6s] %(message)s")
    root_logger = logging.getLogger()
    logging.OUTPUT = 15
    logging.addLevelName(logging.OUTPUT, "OUTPUT")
    logging.OUTERR = 25
    logging.addLevelName(logging.OUTERR, "OUTERR")
    root_logger.setLevel(logging.OUTPUT)
    console_logger = logging.StreamHandler(sys.stdout)
    console_logger.setFormatter(log_format)
    root_logger.addHandler(console_logger)

    if config["logging"]["file"]:
        max_log_size = max(config["logging"]["maxsize"], 0) * 1024
        file_logger = logging.handlers.RotatingFileHandler(
            config["logging"]["file"], maxBytes=max_log_size, backupCount=9
        )
        file_logger.setFormatter(log_format)
        root_logger.addHandler(file_logger)

    if config["notification"]["enabled"] and config["notification"]["sendon"]:
        global notification_log, full_capture
        notification_log = StringIO()
        notification_logger = logging.StreamHandler(notification_log)
        notification_logger.setFormatter(log_format)
        if config["notification"]["short"]:
            # Don't send program stdout in the notification
            notification_logger.setLevel(logging.INFO)
        root_logger.addHandler(notification_logger)

        # Second, always-full-detail capture: unlike notification_log this one
        # is never level-limited, so it keeps the complete run log (including
        # OUTPUT/OUTERR program output) at the root logger's level. It is the
        # source for the file attached to failure notifications.
        full_capture = StringIO()
        full_logger = logging.StreamHandler(full_capture)
        full_logger.setFormatter(log_format)
        root_logger.addHandler(full_logger)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--conf",
        default="snapraid-runner.conf",
        metavar="CONFIG",
        help="Configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--no-scrub",
        action="store_false",
        dest="scrub",
        default=None,
        help="Do not scrub (overrides config)",
    )
    parser.add_argument(
        "--ignore-deletethreshold",
        action="store_true",
        help="Sync even if configured delete threshold is exceeded",
    )
    parser.add_argument(
        "--report",
        choices=["weekly"],
        default=None,
        help="Build and send a scheduled report instead of running the sync cycle",
    )
    args = parser.parse_args()

    if not os.path.exists(args.conf):
        print("snapraid-runner configuration file not found")
        parser.print_help()
        sys.exit(2)

    try:
        load_config(args)
    except Exception:
        print("unexpected exception while loading config")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        setup_logger()
    except Exception:
        print("unexpected exception while setting up logging")
        print(traceback.format_exc())
        sys.exit(2)

    if args.report == "weekly":
        sys.exit(run_weekly())

    init_report()

    try:
        run()
    except Exception:
        logging.exception("Run failed due to unexpected exception:")
        if run_report["failed_phase"] is None:
            run_report["failed_phase"] = "run"
            run_report["error"] = traceback.format_exc()
            run_report["error_short"] = "unexpected exception"
        finish(False)


def run_weekly():
    """Build and deliver the scheduled weekly report.

    Ignores `quiet`/`sendon` (it's explicitly scheduled). Returns the process
    exit code: 0 on success, 1 if building or sending the report failed. If
    notifications aren't configured, the report is printed instead (exit 0).
    """
    logging.info("Building weekly report...")
    try:
        records = report_mod.read_history(config["history"]["file"])

        # snapraid status: fail-soft. status normally exits 0; treat any
        # nonzero (or crash) as "parse nothing" rather than failing the report.
        raw_status = ""
        try:
            status_lines = snapraid_command("status")
            raw_status = "".join(status_lines)
        except Exception:
            logging.warning("Could not run 'snapraid status'", exc_info=True)
        status = report_mod.parse_status(raw_status)

        title, body = report_mod.render_weekly(
            records,
            status,
            scrub_age_warning=config["report"]["scrub-age-warning"],
            raw_status=raw_status,
        )
    except Exception:
        logging.exception("Failed to build weekly report")
        return 1

    if notifications_configured():
        try:
            apprise_send(title, body)
        except Exception:
            logging.exception("Failed to send weekly report")
            return 1
    else:
        # No notifications configured: print it (useful for testing/cron logs).
        logging.info("Notifications not configured; printing weekly report")
        print(title)
        print()
        print(body)
    return 0


def run_phase(name, command, args=None, *, allow_statuscodes=None):
    """Run a snapraid command, timing it and recording the phase in run_report.

    On failure the phase is recorded and the run is finished (does not
    return).
    """
    start = time.monotonic()
    try:
        out = snapraid_command(command, args, allow_statuscodes=allow_statuscodes)
    except subprocess.CalledProcessError as e:
        run_report["phases"][name] = {
            "duration": time.monotonic() - start,
            "success": False,
        }
        logging.error(e)
        fail(name, str(e), getattr(e, "output_tail", None))
    run_report["phases"][name] = {"duration": time.monotonic() - start, "success": True}
    return out


def run():
    logging.info("=" * 60)
    logging.info("Run started")
    logging.info("=" * 60)

    if not os.path.isfile(config["snapraid"]["executable"]):
        message = (
            f'The configured snapraid executable "{config["snapraid"]["executable"]}"'
            " does not exist or is not a file"
        )
        logging.error(message)
        fail("run", message)
    if not os.path.isfile(config["snapraid"]["config"]):
        message = f"Snapraid config does not exist at {config['snapraid']['config']}"
        logging.error(message)
        fail("run", message)

    if config["snapraid"]["touch"]:
        logging.info("Running touch...")
        run_phase("touch", "touch")
        logging.info("*" * 60)

    logging.info("Running diff...")
    diff_out = run_phase("diff", "diff", allow_statuscodes=[2])
    logging.info("*" * 60)

    diff_results = Counter(line.split(" ")[0] for line in diff_out)
    diff_results = dict(
        (x, diff_results[x]) for x in ["add", "remove", "move", "update"]
    )
    run_report["diff"] = diff_results
    logging.info(
        "Diff results: {add} added,  {remove} removed,  "
        "{move} moved,  {update} modified".format(**diff_results)
    )

    if (
        config["snapraid"]["deletethreshold"] >= 0
        and diff_results["remove"] > config["snapraid"]["deletethreshold"]
    ):
        message = (
            "Deleted files exceed delete threshold of "
            f"{config['snapraid']['deletethreshold']}, aborting. "
            "Run again with --ignore-deletethreshold to sync anyways"
        )
        logging.error(message)
        fail("sync", message)

    if (
        diff_results["remove"]
        + diff_results["add"]
        + diff_results["move"]
        + diff_results["update"]
        == 0
    ):
        logging.info("No changes detected, no sync required")
    else:
        logging.info("Running sync...")
        run_phase("sync", "sync")
        logging.info("*" * 60)

    if config["scrub"]["enabled"]:
        logging.info("Running scrub...")
        try:
            # Check if a percentage plan was given
            int(config["scrub"]["plan"])
        except ValueError:
            scrub_args = {"plan": config["scrub"]["plan"]}
            run_report["scrub_plan"] = config["scrub"]["plan"]
        else:
            scrub_args = {
                "plan": config["scrub"]["plan"],
                "older-than": config["scrub"]["older-than"],
            }
            run_report["scrub_plan"] = f"{config['scrub']['plan']}%"
        run_phase("scrub", "scrub", scrub_args)
        logging.info("*" * 60)

    logging.info("All done")
    finish(True)
