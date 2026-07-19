{
  description = "Snapraid runner script to run sync and scrub";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";
    utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, utils }: let
    mkSnapraidRunner = python3Packages:
      with python3Packages;
      buildPythonApplication {
        pname = "snapraid-runner";
        version = "1.3";

        pyproject = true;
        build-system = [ setuptools ];

        propagatedBuildInputs = [ apprise ];

        src = ./.;
      };
  in {
    packages = utils.lib.eachDefaultSystemMap ( system: rec {
      snapraid-runner =
        mkSnapraidRunner nixpkgs.legacyPackages.${system}.python3Packages;
      default = snapraid-runner;
    } );

    devShells = utils.lib.eachDefaultSystemMap ( system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.apprise ]))
            pkgs.snapraid
          ];
        };
      } );

    # Build from the consumer's package set so the overlay does not drag in a
    # second nixpkgs instantiation or a mismatched python.
    overlays.snapraid-runner = final: prev: {
      snapraid-runner = mkSnapraidRunner final.python3Packages;
    };
    overlays.default = self.overlays.snapraid-runner;

    nixosModules.snapraid-runner = import ./modules/services/snapraid-runner.nix;
    nixosModules.default = self.nixosModules.snapraid-runner;
  };
}
