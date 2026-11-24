set shell := ["bash", "-euo", "pipefail", "-c"]

# Open the dev zellij session in a new maximized Ghostty window and return.
start-zellij:
    setsid -f ghostty --maximize=true -e zellij --config "{{justfile_directory()}}/dev-config.kdl" --layout "{{justfile_directory()}}/dev.kdl" >/dev/null 2>&1

# Usage: just profile-ninja [build-dir]   (default: build/release)
# Convert the most recent Ninja build log to a Chrome/Perfetto trace.
profile-ninja build_dir="build/release":
    ninjatracing {{build_dir}}/.ninja_log > {{build_dir}}/ninja-trace.json
    @echo "Wrote {{build_dir}}/ninja-trace.json - open it at https://ui.perfetto.dev"

# Usage: just profile-analyze [build-dir]   (default: build/clang-trace)
# Requires a build configured with Clang -ftime-trace.
profile-analyze build_dir="build/clang-trace":
    ClangBuildAnalyzer --all {{build_dir}} {{build_dir}}/clang-build-analyzer.bin
    ClangBuildAnalyzer --analyze {{build_dir}}/clang-build-analyzer.bin

# Usage: just build-perf [build-dir]   (default: build/perf)
# RelWithDebInfo build with frame pointers, for perf record/report.
build-perf build_dir="build/perf":
    cmake -G Ninja -S "{{justfile_directory()}}" -B {{build_dir}} \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DBUILD_BENCHMARKS=1 \
        -DDISABLE_UNITY=1 \
        -DCMAKE_C_FLAGS="-fno-omit-frame-pointer" \
        -DCMAKE_CXX_FLAGS="-fno-omit-frame-pointer"
    cmake --build {{build_dir}} --parallel
    @echo "Built {{build_dir}}/duckdb and {{build_dir}}/benchmark/benchmark_runner"

