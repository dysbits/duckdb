# Tools required to build and test DuckDB in the AI workspace.
# The workspace image build service expects a list of packages.
{ pkgs, ... }:
let
  # Python with pip and pytest; provides `python3`, `pip`, and `pytest`.
  python3 = pkgs.python3.withPackages (ps: [
    ps.pip
    ps.pytest
  ]);

  # Convert Ninja .ninja_log files (v5-v7) to Chrome/Perfetto trace JSON.
  ninjatracing = pkgs.writeShellScriptBin "ninjatracing" ''
    exec ${pkgs.python3}/bin/python3 ${pkgs.fetchurl {
      url = "https://raw.githubusercontent.com/nico/ninjatracing/fc2924576c9338ff0721f539e6dfa2f3983ae2db/ninjatracing";
      hash = "sha256-dECLW9gBYNf3giu5VZAMcc06pkBh+5OEqcLsOrbQCk8=";
    }} "$@"
  '';

  # The clang wrapper also ships cc/c++ and binutils, which collide with gcc
  # when the image is assembled as a single profile. Expose only the compiler.
  clangOnly = pkgs.linkFarm "clang-only" [
    {
      name = "bin/clang";
      path = "${pkgs.clang}/bin/clang";
    }
    {
      name = "bin/clang++";
      path = "${pkgs.clang}/bin/clang++";
    }
  ];
in
with pkgs;
[
  # Compiler and build system
  gcc
  gnumake
  cmake
  ninja
  pkg-config
  ccache
  mold

  # Python with pip/pytest for the Makefile venvs and test runner
  python3

  # Shell utilities used by the Makefile and helper scripts
  gnugrep
  gnused
  findutils
  gawk
  diffutils
  patch
  gnutar
  gzip
  xz
  bzip2
  unzip
  which

  # Formatting, linting and editor tooling
  typos
  clang-tools
  clangOnly
  lldb

  # Build profiling and task helpers
  just
  ninjatracing
]
