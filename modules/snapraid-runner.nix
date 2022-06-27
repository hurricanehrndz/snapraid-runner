{ config, options, lib, pkgs, ... }:

with lib;

let
  cfg = config.snapraid-runner;
in
{
  options.snapraid-runner = with types; {
    enable = mkEnableOption "snapraid-runner service";
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
        type = str;
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
        default = "/var/log/snapraid-runner.log";
        example = "/var/log/snapraid-runner.log";
        description = "logfile to write to, leave empty to disable";
        type = str;
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
        default = [
          "success"
          "error"
        ];
        example = [
          "success"
          "error"
        ];
        description = "when to send a notificariton on, comma-separated list of [success, error]";
        type = listOf str;
      };
      short = mkOption {
        default = true;
        example = false;
        description = "set to false to get full programm output";
        type = bool;
      };
      config = mkOption {

      };
    };

    scrub = {
      enable = mkEnableOption "snapraid-runner scrub function";
      plan = mkOption {
        default = "12";
        example = "12";
        description = "scrub plan - either a percentage or one of [bad, new, full]";
        type = string;
      };
      older-than = mkOption {
        default = 10;
        example = 10;
        description = "minimum block age (in days) for scrubbing. Only used with percentage plans";
        type = int;
      };
    };
  };

  config = mkIf cfg.enable {
    environment = {
      systemPackages = with pkgs; [
        snapraid
        snapraid-runner
      ];

      etc."snapraid-runner.conf".text = generators.toINI {} { snapraid = cfg.snapraid; };
    };

  };
}
