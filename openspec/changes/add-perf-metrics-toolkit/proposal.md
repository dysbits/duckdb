# Proposal

## Why

DuckDB benchmarks currently report wall-clock query timings only. Diagnosing why a query got slower — extra file opens, block-IO changes, memory growth, CPU imbalance, or a hot function — requires assembling ad-hoc commands each time. The development workspace also lacks profiling utilities, has no hardware PMU, and cannot load eBPF tracing programs, so the tooling must degrade predictably instead of failing outright.

## What Changes

- Add a portable metrics wrapper, `devtools/metrics/duckdb_metrics.py`, that runs a command and samples its process tree from `/proc`, reporting CPU, RSS/PSS, logical and block IO, file-descriptor and thread counts, page faults, and context switches as JSON plus a CSV time series. When wrapping `benchmark_runner`, it merges the runner's timing CSV into the report.
- Add `devtools/metrics/ebpf/*.bt` scripts for per-path open/close and file IO, block-IO latency, and off-CPU time, used only when `bpftrace`, capabilities, and kernel BTF are present. Keep `strace -c` as the explicit unprivileged fallback for file syscall counts; do not ship an `LD_PRELOAD` shim.
- Add opt-in `perf` integration: `perf stat` for supported events, `perf record` sampling, and flamegraph generation. Hardware counters are reported as unavailable when the PMU is absent.
- Add a `just build-perf` recipe that configures a `RelWithDebInfo` build with `-fno-omit-frame-pointer` and benchmarks enabled, so `perf record --call-graph fp` works.
- Add the profiling packages (`perf`, `strace`, `sysstat`, `procps`, `lsof`, `hyperfine`, `iotop`, `heaptrack`, `py-spy`, flamegraph tooling, `bpftrace`) to `tools.nix`, plus documentation of the workspace's PMU and eBPF limits.

No breaking changes: existing benchmark output, query behavior, and default builds are untouched. No existing source files under `benchmark/` are modified; per-run metrics inside `benchmark_runner` are deferred to a separate change.

## Capabilities

### New Capabilities

- `performance-metrics`: collecting and reporting process-level CPU, memory, IO, file-descriptor, thread, fault, and scheduling metrics around DuckDB commands and benchmarks; per-path file open/close and byte accounting through eBPF when available; an unprivileged `strace` count fallback; optional perf integration with documented fallbacks; and a frame-pointer build recipe for profiling.

### Modified Capabilities

<!-- None. -->

## Impact

- New `devtools/` tree: metrics wrapper, eBPF scripts, and documentation.
- `tools.nix`: additional profiling packages; requires an image rebuild to take effect.
- `justfile`: new `build-perf` recipe.
- Linux-only collection paths; other platforms degrade to no metrics rather than failing. No changes to DuckDB query semantics, storage, benchmark sources, or benchmark datasets.
