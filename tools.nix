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

  # Aggregate Clang -ftime-trace JSON files into a build-time report.
  clangBuildAnalyzer = pkgs.stdenv.mkDerivation rec {
    pname = "clang-build-analyzer";
    version = "1.6.0";

    src = pkgs.fetchFromGitHub {
      owner = "aras-p";
      repo = "ClangBuildAnalyzer";
      rev = "v${version}";
      hash = "sha256-GIMQZGPFKDrfMqCsA8nR3O8Hzp2jcaZ+yDrPeCxTsIg=";
    };

    nativeBuildInputs = [
      pkgs.cmake
      pkgs.ninja
    ];

    cmakeFlags = [ "-DCMAKE_BUILD_TYPE=Release" ];

    meta = with pkgs.lib; {
      description = "Clang build analysis tool using -ftime-trace";
      homepage = "https://github.com/aras-p/ClangBuildAnalyzer";
      license = with licenses; [
        unlicense
        zlib
        mit
        boost
        bsd3
        asl20
        llvm-exception
        bsd2
      ];
      platforms = platforms.unix;
      mainProgram = "ClangBuildAnalyzer";
    };
  };

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
  clangBuildAnalyzer
  just
  ninjatracing
]
