# Tasks

## 1. Workspace tooling and feasibility

- [x] 1.1 Add `linuxPackages.perf`, `strace`, `time`, `sysstat`, `procps`, `lsof`, `hyperfine`, `iotop`, `heaptrack`, `py-spy`, flamegraph tooling, and `bpftrace` to `tools.nix`, then verify with `nix-instantiate --parse tools.nix` and `nix shell nixpkgs#... --command perf --version`
- [x] 1.2 Probe perf support in the workspace with `perf stat -e task-clock,page-faults -- true` and `perf record -e cpu-clock -g -- sleep 0.1`; record which events and call-graph modes work in `devtools/metrics/README.md` and confirm each probe either succeeds or has a documented fallback
- [x] 1.3 Confirm the `just build-perf` recipe (already added) configures `build/perf` with `-DCMAKE_CXX_FLAGS=-fno-omit-frame-pointer` by running `just --dry-run build-perf` or inspecting `build/perf/CMakeCache.txt` after configure
- [ ] 1.4 Build the benchmark runner with `BUILD_BENCHMARK=1 make reldebug` and verify `build/reldebug/benchmark/benchmark_runner --list` prints benchmarks

## 2. Portable sampler and report

- [x] 2.1 Create `devtools/metrics/duckdb_metrics.py` with command passthrough plus `--out`, `--csv`, `--interval`, `--smaps`, and `--quiet` options; verify `--help` works and a `true` command produces a JSON report
- [x] 2.2 Implement process-tree discovery and sampling of CPU, RSS, fd count, threads, IO, faults, and context switches from `/proc`; verify a command that spawns children (`sh -c 'sleep 0.2 & sleep 0.2'`) aggregates them and reports the observed process count
- [x] 2.3 Implement final counter collection using `waitid(..., WNOWAIT)` and `wait4` rusage before reaping; verify a command that exits immediately still reports non-zero CPU and IO values
- [x] 2.4 Implement the JSON schema and optional CSV writer, including exit-code propagation and non-negative values; verify a failing command returns the same non-zero exit code and records it in the report
- [x] 2.5 Document sampler usage and metric semantics in `devtools/metrics/README.md` (logical vs block IO, RSS vs PSS, warm page cache, 2-vCPU caveat) and verify every documented command runs as written

## 3. File tracing

- [x] 3.1 Remove the `LD_PRELOAD` shim and `--files shim` path from the wrapper, delete `devtools/metrics/io_trace/`, and verify `--help` lists only `off` and `strace`
- [x] 3.2 Keep `--ebpf auto|on|off` detection and script execution; verify the eBPF-unavailable warning and the clear `--ebpf on` failure in this workspace
- [x] 3.3 Verify `--files strace` reports syscall counts for a small DuckDB read/write query and compare them with direct `strace -c` output
- [x] 3.4 Document eBPF-first file tracing, the strace fallback, and the sandbox limitation in `devtools/README.md`, `devtools/metrics/README.md`, and `devtools/metrics/ebpf/README.md`; verify the documented commands

## 4. Benchmark timing correlation

- [ ] 4.1 Auto-detect `benchmark_runner`, inject a temporary `--out=` file when needed, and parse `name<TAB>run<TAB>timing` rows; verify a run of `benchmark/micro/nulls/no_nulls_addition.benchmark` populates the per-benchmark section
- [ ] 4.2 Compute per-benchmark run count, minimum, median, and mean timings while labeling the `/proc` metrics as whole-process; verify the JSON fields and that a non-benchmark command omits the section
- [ ] 4.3 Document benchmark wrapping and the whole-process caveat for per-benchmark attribution in `devtools/metrics/README.md`; verify the documented invocation

## 5. perf integration

- [x] 5.1 Implement event probing and `--perf stat` attach mode; verify in this workspace that supported software events appear and hardware counters are marked unavailable rather than causing an error
- [ ] 5.2 Implement `--perf record` plus `--flamegraph`, including a folded-stack fallback when no renderer is installed; verify an SVG or folded file is produced from `build/perf` binaries
- [x] 5.3 Handle a missing or un-attachable `perf` by warning once and preserving the command's exit status; verify with a `PATH` that excludes perf
- [ ] 5.4 Document perf recipes, DWARF versus frame-pointer call graphs, and the `just build-perf` workflow in `devtools/perf/README.md`; verify the documented commands run as written

## 6. Integration validation

- [x] 6.1 Compare wrapper CPU and peak RSS against `/usr/bin/time -v`, recording the results in `devtools/metrics/README.md`; verify the numbers agree within the documented tolerance
- [x] 6.2 Measure wrapper overhead on a roughly one-second micro benchmark with sampling on and off; verify overhead is documented and below 2% for the default interval
- [ ] 6.3 Run an end-to-end report with `--perf stat --files strace --per-benchmark` against a micro benchmark and a TPC-H SF1 query; verify the report contains all expected sections or clearly documented unavailable markers
- [x] 6.4 Run `openspec validate add-perf-metrics-toolkit --strict` and the repository formatting checks on new files; verify both pass

## Deferred

- Native per-run metrics inside `benchmark_runner` are deferred to a separate change; this change must not modify files under `benchmark/`.

## Workflow follow-up

- Archive the change after the implementation and validation tasks are complete.
