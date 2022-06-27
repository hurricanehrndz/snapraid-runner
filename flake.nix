{
  description = "Snapraid runner script to run sync and scrub";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-22.05";
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
          version = "1.0";

          propagatedBuildInputs = [ apprise ];

          src = ./.;
        };
      default = snapraid-runner;
    } );

    overlays.snapraid-runner = final: prev: {
      inherit (self.packages.${final.system}) snapraid-runner;
    };
    overlays.default = self.overlays.snapraid-runner;

    nixosModules.snapraid-runner = import ./modules/snapraid-runner.nix;
    nixosModules.default = self.nixosModules.snapraid-runner;
  };
}
