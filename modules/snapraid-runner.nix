{ config, options, lib, pkgs, ... }:

with lib;

let
  cfg = config.snapraid-runner;
  loggingOption = if cfg.logging.file == null then overrideExisting cfg.logging { file = ""; } else cfg.logging;
in
{
  options.snapraid-runner = with types; {
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
      conf = mkOption {
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
        description = "maximum logfile size in KiB, leave empty for infinit";
        type = int;
      };
    };

    # notifications
    notification = {
      enable = mkEnableOption "snapraid-runner notifications";
      sendon = mkOption {
        default = "success,error";
        example = "success,error";
        description = "when to send a notificariton on, comma-separated list of [success, error]";
        type = str;
      };
      short = mkOption {
        default = true;
        example = false;
        description = "set to false to get full programm output";
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
      enable = mkEnableOption "snapraid-runner scrub function";
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

    apprise-conf = mkOption{
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

      etc = {
        "snapraid-runner.conf".text = generators.toINI {} {
          snapraid = cfg.snapraid;
          logging = loggingOption;
          notification = cfg.notification;
          scrub = cfg.scrub;
        };
      } // optionalAttrs (cfg.apprise-conf != null) {
        "snapraid-runner.apprise.yaml".text = generators.toYAML {} cfg.apprise-conf;
      };
    };

    systemd.services = {
      # disable snapraid services
      snapraid-scrub.enable = mkForce false;
      snapraid-sync.enable = mkForce false;
      snapraid-runner = {
        description = "Diff, Sync and Scrub the SnapRAID array via snapraid-runner";
        startAt = cfg.interval;
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${pkgs.snapraid-runner}/bin/snapraid-runner -c /etc/snapraid-runner.conf";
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
          RestrictAddressFamilies = "none";
          RestrictNamespaces = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          SystemCallArchitectures = "native";
          SystemCallFilter = "@system-service";
          SystemCallErrorNumber = "EPERM";
          CapabilityBoundingSet = "CAP_DAC_OVERRIDE" + " CAP_FOWNER";

          ProtectSystem = "strict";
          ProtectHome = "read-only";
          ReadWritePaths =
            # sync, diff, scrub requires access to directories containing content files
            # to remove them if they are stale
            let
              contentDirs = map dirOf config.snapraid.contentFiles;
            in
            unique (
              attrValues config.snapraid.dataDisks ++ contentDirs ++ config.snapraid.parityFiles ++ (
                optional (cfg.logging.file != null) [
                  dirOf cfg.logging.file
                ]
              )
            );
        };
      };
    };
  };
}
