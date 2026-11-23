{
  description = "DuckDB development";

  inputs = {
    nixpkgs-pin.url = "git+https://codeberg.org/dysbits/nixpkgs-pin";
    nixpkgs.follows = "nixpkgs-pin/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      nixpkgs-pin,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
        };
      in
      {
        devShells.default = pkgs.mkShell {
          packages =
            (import ./tools.nix { inherit pkgs; })
            ++ (with pkgs; [
              git
              jq
              curl
              openspec
              openssh
            ]);

          shellHook = ''
            export CCACHE_DIR="$HOME/.cache/ccache"
            export CC=gcc
            export CXX=g++
            export GEN=ninja
            export DUCKDB_LINKER=mold

            if [ ! -e /usr/bin/env ]; then
              echo "note: /usr/bin/env is missing; run build/*/test/run via python3"
            fi
          '';
        };
      }
    );
}
