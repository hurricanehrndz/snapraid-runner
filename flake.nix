{
  description = "Snapraid runner script to run sync and scrub";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";
    flake-parts.url = "github:hercules-ci/flake-parts";
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
    git-hooks.url = "github:cachix/git-hooks.nix";
    git-hooks.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    let
      mkSnapraidRunner =
        python3Packages:
        with python3Packages;
        buildPythonApplication {
          pname = "snapraid-runner";
          version = "1.3";

          pyproject = true;
          build-system = [ setuptools ];

          propagatedBuildInputs = [ apprise ];

          src = ./.;

          # Tests are stdlib unittest; run them during the build so
          # `nix build` / `nix flake check` gate on them.
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            python -m unittest discover -s tests
            runHook postCheck
          '';
        };

      # Build from the consumer's package set so the overlay does not drag in a
      # second nixpkgs instantiation or a mismatched python.
      snapraidRunnerOverlay = final: prev: {
        snapraid-runner = mkSnapraidRunner final.python3Packages;
      };
    in
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [
        inputs.treefmt-nix.flakeModule
        inputs.git-hooks.flakeModule
      ];

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      flake = {
        overlays.snapraid-runner = snapraidRunnerOverlay;
        overlays.default = snapraidRunnerOverlay;

        nixosModules.snapraid-runner = import ./modules/services/snapraid-runner.nix;
        nixosModules.default = inputs.self.nixosModules.snapraid-runner;
      };

      perSystem =
        { config, pkgs, ... }:
        {
          packages.snapraid-runner = mkSnapraidRunner pkgs.python3Packages;
          packages.default = config.packages.snapraid-runner;

          devShells.default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [ ps.apprise ]))
              pkgs.snapraid
            ];
            shellHook = config.pre-commit.installationScript;
          };

          # `nix fmt`
          formatter = config.treefmt.build.wrapper;

          treefmt = {
            projectRootFile = "flake.nix";
            programs.nixfmt.enable = true;
            programs.ruff-format.enable = true;
            programs.taplo.enable = true;
            programs.yamlfmt.enable = true;
          };

          pre-commit.settings.hooks.treefmt.enable = true;
        };
    };
}
