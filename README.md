# snapraid-runner

A small tool that runs a SnapRAID maintenance cycle and reports the result.
On each run it:

1. Runs `snapraid diff` and counts how many files were added, removed, moved or
   modified.
2. Aborts before touching parity if the number of deleted files exceeds a
   configurable **delete threshold** (a guard against accidental mass deletion).
3. Runs `snapraid sync` when there are changes.
4. Optionally runs `snapraid scrub` afterwards.
5. Optionally runs `snapraid touch` first.

All output goes to the console and, optionally, to a size-limited rotating log
file. When enabled, a notification is sent through
[Apprise](https://github.com/caronc/apprise) after each run (or only on
failures). Notifications are structured for a phone screen: an emoji status
title that carries the whole story, one line per phase with its duration, and —
on failure — an excerpt of the actual error leading the body. A **quiet** mode
suppresses the success notification when a run made no changes (failures are
always sent).

Each run can also be appended to a JSONL **run history**, which feeds a
scheduled **weekly report**: a rollup of the week's runs plus a `snapraid status`
snapshot rendered as a per-disk disk-usage graph, with a warning when the oldest
un-scrubbed block gets too old.

It is meant to be run on a schedule — via the bundled NixOS module, a systemd
timer, or cron.

This is a fork of [Chronial/snapraid-runner](https://github.com/Chronial/snapraid-runner).
It has diverged: it is now a proper Python package with a `snapraid-runner`
entry point, uses Apprise for notifications instead of SMTP email, and ships a
Nix flake with a package, overlay, dev shell and a NixOS module. All credit for
the original tool goes to the upstream project.

## Usage on NixOS (flake)

Add this repository as a flake input and pull in the overlay and/or the NixOS
module:

```nix
{
  inputs.snapraid-runner.url = "github:hurricanehrndz/snapraid-runner";

  outputs = { self, nixpkgs, snapraid-runner, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        # makes pkgs.snapraid-runner available
        { nixpkgs.overlays = [ snapraid-runner.overlays.default ]; }
        snapraid-runner.nixosModules.snapraid-runner
        ./configuration.nix
      ];
    };
  };
}
```

Then configure the service. The module builds on the upstream nixpkgs
`services.snapraid` module for the array layout (`dataDisks`, `contentFiles`,
`parityFiles`) — those are read to compute the systemd service's writable
paths, so configure `services.snapraid` as usual and let this module drive the
runner:

```nix
{
  services.snapraid-runner = {
    enable = true;
    interval = "daily";               # systemd OnCalendar; default "01:00"

    snapraid = {
      # executable defaults to ${pkgs.snapraid}/bin/snapraid
      config = "/etc/snapraid.conf";
      deletethreshold = 40;           # -1 to disable
      touch = false;
    };

    logging = {
      file = "/var/log/snapraid-runner.log";  # null (default) disables file logging
      maxsize = 5000;                          # KiB
    };

    scrub = {
      enabled = true;
      plan = "12";                    # percentage, or one of: bad, new, full
      older-than = 10;                # days; only used with percentage plans
    };

    notification = {
      enable = true;
      sendon = "success,error";       # comma-separated: success, error
      short = true;                   # false to include full program output
      quiet = false;                  # true = skip the success notice on a no-op run
      # config = path to the Apprise YAML; defaults to
      #   /etc/snapraid-runner.apprise.yaml
    };

    history = {
      # one JSON line per run; feeds the weekly report. Set null to disable.
      file = "/var/lib/snapraid-runner/history.jsonl";  # this is the default
    };

    report = {
      enable = true;                  # send a scheduled weekly summary
      interval = "Sun 09:00";         # systemd OnCalendar; default "Sun 09:00"
      scrub-age-warning = 30;         # days; warn when oldest un-scrubbed block is older. 0 disables
    };

    # Optional: have the module write the Apprise YAML for you.
    # If you set this, point notification.config at the same path (its default
    # already matches). Otherwise manage the Apprise file yourself.
    apprise-conf = {
      urls = [ "pbul://MY-KEY" ];
    };
  };
}
```

The generated systemd service runs as a hardened `oneshot` unit (strict
`ProtectSystem`, restricted syscalls, minimal capabilities) with write access
limited to the SnapRAID data/content/parity directories, the log directory, and
`/var/lib/snapraid-runner` (the run-history `StateDirectory`).
Enabling the module also disables the stock `snapraid-sync` and `snapraid-scrub`
units so the runner is the single entry point.

Setting `report.enable = true` adds a second, identically hardened unit
(`snapraid-runner-report`) on its own `report.interval` timer that runs
`snapraid-runner --report weekly` to send the weekly summary.

## Usage without Nix

Requires Python >= 3.9 and the `snapraid` binary on `PATH`.

```sh
# install from a checkout of this repo
pip install .

# create your config
cp snapraid-runner.conf.example snapraid-runner.conf
# edit it — at minimum set snapraid.executable and snapraid.config
$EDITOR snapraid-runner.conf

# run
snapraid-runner -c snapraid-runner.conf
```

Schedule it with cron or a systemd timer.

### Command-line flags

| Flag | Effect |
| --- | --- |
| `-c`, `--conf CONFIG` | Path to the config file (default `snapraid-runner.conf`). |
| `--no-scrub` | Skip scrub for this run, overriding `scrub.enabled` in the config. |
| `--ignore-deletethreshold` | Sync even if the delete threshold is exceeded. |
| `--report weekly` | Build and send the weekly report instead of running the sync cycle (see below). |

### Config file

See `snapraid-runner.conf.example` for the full, commented template. Sections:

- `[snapraid]` — `executable`, `config`, `deletethreshold`, `touch`.
- `[logging]` — `file` (leave empty to disable), `maxsize` (KiB).
- `[notification]` — see below.
- `[scrub]` — `enabled`, `plan`, `older-than`.
- `[history]` — `file`: append one JSON line per run (timestamp, diff counts,
  per-phase durations, failures) to this path; leave empty to disable. Entries
  older than 90 days are pruned automatically.
- `[report]` — `scrub-age-warning`: days before the weekly report warns that the
  oldest un-scrubbed block is too old (`0` disables; default `30`).

### Weekly report

`snapraid-runner --report weekly` builds a scheduled summary instead of running
the sync cycle. It rolls up the `[history]` records from the last 7 days and adds
a `snapraid status` snapshot (per-disk usage graph, scrub age, health). Schedule
it with its own cron entry or systemd timer (e.g. weekly).

It is explicitly scheduled, so it ignores `notification.quiet` and
`notification.sendon`. If notifications aren't configured it prints the report to
stdout instead (handy for cron logs).

## Notifications

Notifications are delivered by [Apprise](https://github.com/caronc/apprise),
which supports a large range of services (Pushbullet, Telegram, ntfy, email,
Discord, and many more). The runner reads an Apprise **YAML config file** whose
path is given by `notification.config`; see `apprise.yml.example` and the
[Apprise config_yaml wiki](https://github.com/caronc/apprise/wiki/config_yaml)
for the format.

`[notification]` options:

- `enabled` — set to `false` to disable notifications entirely.
- `sendon` — comma-separated list of events to notify on; any of `success`,
  `error`.
- `short` — `true` (default) sends only the high-level log; set to `false` to
  include the full program output.
- `quiet` — `true` skips the success notification when a run made no changes (a
  no-op). Failures are always sent.
- `config` — path to the Apprise YAML file.

When a run fails, the complete run log is attached to the error notification as
a file (`snapraid-runner-YYYYMMDD-HHMMSS.log`); targets without attachment
support simply ignore it.

Notifications are structured for quick reading. A daily success looks like:

```
✅ SnapRAID: 12 added · 5 updated · 3 moved · synced in 6m 12s

Diff   +12 added · ~5 updated · →3 moved   (4s)
Sync   ✅ completed   (6m 12s)
Scrub  ✅ plan 12%   (2m 8s)
```

and the weekly report like:

```
📊 SnapRAID weekly: 2 runs, all OK

Week in review (last 7 days)
2 runs · 2 OK
Files churned: +14 added · −1 removed · ~5 updated · →3 moved
Total sync time: 7m 47s

Disk usage
d1    ▇▇▇▇▇▇▇▇▇▁  90%  (200 GB free)
d2    ▇▇▇▇▇▁▁▁▁▁  45%  (1.1 TB free)
total ▇▇▇▇▇▇▇▁▁▁  67%

Scrub age: oldest 8d · median 3d · newest 0d

✅ No errors detected
```

(The title is the message subject; the rest is the body.)

## Development

A Nix dev shell provides Python (with Apprise) and the `snapraid` binary:

```sh
nix develop
```

Or, with [direnv](https://direnv.net/), just `cd` into the repo — the committed
`.envrc` (`use flake`) loads the same shell automatically after `direnv allow`.
Entering the shell also installs a [git-hooks](https://github.com/cachix/git-hooks.nix)
pre-commit hook that runs the formatter.

Formatting is handled by [treefmt](https://github.com/numtide/treefmt-nix)
(nixfmt, ruff, taplo, yamlfmt):

```sh
nix fmt
```

`nix flake check` runs the whole gate — package build, the unit tests, the
formatting check, and a NixOS module eval — and is what CI runs on every push.
The tests can also be run directly with `python -m unittest discover -s tests`.
