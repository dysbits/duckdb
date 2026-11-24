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
        tools = import ./tools.nix { inherit pkgs; };
        devTools = with pkgs; [
          git
          jq
          curl
          openspec
          openssh
        ];

        # Drop gcc so the clang shell's stdenv compiler is unambiguous.
        clangTools = builtins.filter (package: package != pkgs.gcc) tools;

        commonShellHook = ''
          export CCACHE_DIR="$HOME/.cache/ccache"

          if [ ! -e /usr/bin/env ]; then
            echo "note: /usr/bin/env is missing; run build/*/test/run via python3"
          fi
        '';

        gccShell = pkgs.mkShell {
          packages = tools ++ devTools;
          shellHook = commonShellHook + ''
            export CC=gcc
            export CXX=g++
          '';
        };

        clangShell = (pkgs.mkShell.override { stdenv = pkgs.llvmPackages_21.stdenv; }) {
          packages = clangTools ++ devTools;
          shellHook = commonShellHook + ''
            export CC=clang
            export CXX=clang++
          '';
        };
      in
      {
        devShells = {
          default = gccShell;
          gcc = gccShell;
          clang = clangShell;
        };
      }
    );
}
