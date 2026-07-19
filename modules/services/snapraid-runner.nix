{
  config,
  lib,
  pkgs,
  ...
}:
with lib; let
  cfg = config.services.snapraid-runner;
  loggingOption =
    if cfg.logging.file == null
    then overrideExisting cfg.logging {file = "";}
    else cfg.logging;

  # Shared hardening for both the runner and the weekly-report units. ExecStart
  # differs, so it is merged in per-service.
  sharedServiceConfig = {
    Type = "oneshot";
    Nice = 19;
    IOSchedulingPriority = 7;
    CPUSchedulingPolicy = "batch";

    LockPersonality = true;
    MemoryDenyWriteExecute = true;
    NoNewPrivileges = true;
    PrivateTmp = true;
    ProtectClock = true;
    ProtectControlGroups = true;
    ProtectHostname = true;
    ProtectKernelLogs = true;
    ProtectKernelModules = true;
    ProtectKernelTunables = true;
    RestrictNamespaces = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    SystemCallArchitectures = "native";
    SystemCallFilter = "@system-service";
    SystemCallErrorNumber = "EPERM";
    CapabilityBoundingSet = "CAP_DAC_OVERRIDE CAP_FOWNER";

    ProtectSystem = "strict";
    ProtectHome = "read-only";
    # Creates and keeps /var/lib/snapraid-runner writable (default history dir)
    # despite ProtectSystem=strict.
    StateDirectory = "snapraid-runner";
    ReadWritePaths =
      # sync, diff, scrub requires access to directories containing content files
      # to remove them if they are stale
      let
        contentDirs = map dirOf config.services.snapraid.contentFiles;
      in
        unique (
          attrValues config.services.snapraid.dataDisks
          ++ contentDirs
          ++ config.services.snapraid.parityFiles
          ++ optional (cfg.logging.file != null) (dirOf cfg.logging.file)
          ++ optional (cfg.history.file != null) (dirOf cfg.history.file)
        );
  };
in {
  options.services.snapraid-runner = with types; {
    enable = mkEnableOption "snapraid-runner service";
    interval = mkOption {
      default = "01:00";
      example = "daily";
      description = "How often to run <command>snapraid-runner</command>.";
      type = str;
    };
    # snapraid options
    snapraid = {
      executable = mkOption {
        default = "${pkgs.snapraid}/bin/snapraid";
        example = "${pkgs.snapraid}/bin/snapraid";
        description = "path to the snapraid executable";
        type = str;
      };
      config = mkOption {
        default = "/etc/snapraid.conf";
        example = "/etc/snapraid.conf";
        description = "path to the snapraid config to be used";
        type = path;
      };
      deletethreshold = mkOption {
        default = 40;
        example = 40;
        description = "abort operation if there are more deletes than this, set to -1 to disable";
        type = int;
      };
      touch = mkOption {
        default = false;
        example = true;
        description = "Sets arbitrarily the sub-second time-stamp of all the files that have it at zero";
        type = bool;
      };
    };

    # logging options
    logging = {
      file = mkOption {
        default = null;
        example = "/var/log/snapraid-runner.log";
        description = "logfile to write to, leave empty to disable";
        type = nullOr path;
      };
      maxsize = mkOption {
        default = 5000;
        example = 5000;
        description = "maximum logfile size in KiB, leave empty for infinite";
        type = int;
      };
    };

    # notifications
    notification = {
      enable = mkEnableOption "snapraid-runner notifications";
      sendon = mkOption {
        default = "success,error";
        example = "success,error";
        description = "when to send a notification on, comma-separated list of [success, error]";
        type = str;
      };
      short = mkOption {
        default = true;
        example = false;
        description = "set to false to get full program output";
        type = bool;
      };
      quiet = mkOption {
        default = false;
        example = true;
        description = "suppress the success notification when a run made no changes (a no-op); failures are always sent";
        type = bool;
      };
      config = mkOption {
        default = "/etc/snapraid-runner.apprise.yaml";
        example = "/run/agenix/snapraid-runner.apprise.yaml";
        description = ''
          Python Apprise config path.
          https://github.com/caronc/apprise/wiki/config_yaml
        '';
        type = path;
      };
    };

    scrub = {
      enabled = mkOption {
        default = false;
        example = false;
        description = "set to true to run scrub after sync";
        type = bool;
      };
      plan = mkOption {
        default = "12";
        example = "12";
        description = "scrub plan - either a percentage or one of [bad, new, full]";
        type = str;
      };
      older-than = mkOption {
        default = 10;
        example = 10;
        description = "minimum block age (in days) for scrubbing. Only used with percentage plans";
        type = int;
      };
    };

    # run history (JSONL, one record per run; consumed by the weekly report)
    history = {
      file = mkOption {
        default = "/var/lib/snapraid-runner/history.jsonl";
        example = "/var/lib/snapraid-runner/history.jsonl";
        description = "append one JSON line per run to this file, leave null to disable. Entries older than 90 days are pruned automatically.";
        type = nullOr path;
      };
    };

    # scheduled weekly report (snapraid-runner --report weekly)
    report = {
      enable = mkEnableOption "weekly report";
      interval = mkOption {
        default = "Sun 09:00";
        example = "Sun 09:00";
        description = "how often to send the weekly report (systemd OnCalendar)";
        type = str;
      };
      scrub-age-warning = mkOption {
        default = 30;
        example = 30;
        description = "warn in the report when the oldest un-scrubbed block is older than this many days. Set to 0 to disable the warning.";
        type = int;
      };
    };

    apprise-conf = mkOption {
      type = nullOr attrs;
      default = null;
      example = literalExpression ''
        {
          "urls" = [
            "json://localhost"
          ];
        }
      '';
      description = ''
        Appprise yaml contents.
        Will automatically get converted to yaml.
        see: https://github.com/caronc/apprise/wiki/config_yaml";
      '';
    };
  };

  config = mkIf cfg.enable {
    environment = {
      systemPackages = with pkgs; [
        snapraid
        snapraid-runner
      ];

      etc =
        {
          "snapraid-runner.conf".text = generators.toINI {} {
            snapraid = cfg.snapraid;
            logging = loggingOption;
            # Python reads `enabled`; the NixOS option is `enable` (convention).
            notification =
              removeAttrs cfg.notification ["enable"]
              // {enabled = cfg.notification.enable;};
            scrub = cfg.scrub;
            # Empty string disables history, matching logging.file null-handling.
            history.file =
              if cfg.history.file == null
              then ""
              else cfg.history.file;
            # Only scrub-age-warning is read from [report]; interval/enable
            # drive the systemd timer, not the Python.
            report."scrub-age-warning" = cfg.report.scrub-age-warning;
          };
        }
        // optionalAttrs (cfg.apprise-conf != null) {
          "snapraid-runner.apprise.yaml".text = generators.toYAML {} cfg.apprise-conf;
        };
    };

    systemd.services =
      {
        # disable snapraid services
        snapraid-scrub.enable = mkForce false;
        snapraid-sync.enable = mkForce false;
        snapraid-runner = {
          description = "Diff, Sync and Scrub the SnapRAID array via snapraid-runner";
          startAt = cfg.interval;
          serviceConfig =
            sharedServiceConfig
            // {
              ExecStart = "${pkgs.snapraid-runner}/bin/snapraid-runner -c /etc/snapraid-runner.conf";
            };
        };
      }
      // optionalAttrs cfg.report.enable {
        snapraid-runner-report = {
          description = "Send the SnapRAID weekly report via snapraid-runner";
          startAt = cfg.report.interval;
          serviceConfig =
            sharedServiceConfig
            // {
              ExecStart = "${pkgs.snapraid-runner}/bin/snapraid-runner -c /etc/snapraid-runner.conf --report weekly";
            };
        };
      };
  };
}
