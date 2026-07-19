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
failures).

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
      # config = path to the Apprise YAML; defaults to
      #   /etc/snapraid-runner.apprise.yaml
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
limited to the SnapRAID data/content/parity directories and the log directory.
Enabling the module also disables the stock `snapraid-sync` and `snapraid-scrub`
units so the runner is the single entry point.

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

### Config file

See `snapraid-runner.conf.example` for the full, commented template. Sections:

- `[snapraid]` — `executable`, `config`, `deletethreshold`, `touch`.
- `[logging]` — `file` (leave empty to disable), `maxsize` (KiB).
- `[notification]` — see below.
- `[scrub]` — `enabled`, `plan`, `older-than`.

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
- `config` — path to the Apprise YAML file.

## Development

A Nix dev shell provides Python (with Apprise) and the `snapraid` binary:

```sh
nix develop
```

Or, with [direnv](https://direnv.net/), just `cd` into the repo — the committed
`.envrc` (`use flake`) loads the same shell automatically after `direnv allow`.
