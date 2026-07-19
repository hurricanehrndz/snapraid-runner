{
  description = "Snapraid runner script to run sync and scrub";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";
    utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, utils }: {
    packages = utils.lib.eachDefaultSystemMap ( system: rec {
      snapraid-runner =
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        with pkgs.python3Packages;
        buildPythonApplication {
          pname = "snapraid-runner";
          version = "1.2";

          pyproject = true;
          build-system = [ setuptools ];

          propagatedBuildInputs = [ apprise ];

          src = ./.;
        };
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

    overlays.snapraid-runner = final: prev: {
      inherit (self.packages.${final.system}) snapraid-runner;
    };
    overlays.default = self.overlays.snapraid-runner;

    nixosModules.snapraid-runner = import ./modules/services/snapraid-runner.nix;
    nixosModules.default = self.nixosModules.snapraid-runner;
  };
}
