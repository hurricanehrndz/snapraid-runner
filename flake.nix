{
  description = "Snapraid runner script to run sync and scrub";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        customOverrides = self: super: {
          # Overrides go here
        };

        app = with pkgs.python3Packages; buildPythonApplication {
          pname = "snapraid-runenr";
          version = "1.0";

          propagatedBuildInputs = [ apprise ];

          src = ./.;
        }

        packageName = "snapraid-runner";
      in {
        packages.${packageName} = app;

        defaultPackage = self.packages.${system}.${packageName};
      });
}

