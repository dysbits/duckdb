# Design

## Context

See `proposal.md` for motivation and `specs/performance-metrics/spec.md` for the behavior contract.

The workspace constrains the design in ways that differ from a normal Linux host:

- The development VM has 2 vCPUs and 6 GB RAM, so benchmark parallelism and sampling overhead matter.
- `/sys/bus/event_source/devices/` exposes only `software`, `msr`, and `breakpoint`; there is no hardware PMU.
- The process runs as uid 1000 with `CapEff=0`, and `/sys/kernel/btf/vmlinux` is absent, so eBPF tracing programs cannot load.
- `/proc/<pid>/{io,stat,status,smaps_rollup,schedstat,task/*}` is readable for the wrapper's own process tree, and `clear_refs=5` can reset the peak-RSS high-water mark.
- `perf`, `strace`, `sysstat`, and `bpftrace` are not installed; `tools.nix` is tracked and drives the workspace image.
- `CMakeLists.txt` replaces `CMAKE_CXX_FLAGS_RELWITHDEBINFO` with the release flags plus `-g`, so a frame-pointer flag must go through `CMAKE_CXX_FLAGS` / `CMAKE_C_FLAGS`.
- `benchmark/benchmark_runner.cpp` runs one warmup plus N timed runs per benchmark (`RunBenchmark`) and emits `name<TAB>run<TAB>timing`, optionally mirrored to `--out=<file>`.

## Goals / Non-Goals

**Goals:**

- One entry point that produces a consistent JSON/CSV report for any command, including `benchmark_runner`.
- Capture CPU, memory, logical and block IO, file-descriptor, thread, fault, and scheduling metrics without external Python packages.
- Attribute file opens, closes, and bytes to paths with low enough overhead to keep timings meaningful.
- Use perf when it is available and degrade to `/proc` only when it is not.
- Correlate existing benchmark timings with whole-process metrics without changing benchmark sources or output.
- Keep all new behavior opt-in; default builds and timing output remain unchanged.

**Non-Goals:**

- System-wide monitoring, daemons, or time-series databases.
- Prometheus/Grafana integration or a web dashboard.
- Automatic regression detection or CI gating.
- Cache dropping or guaranteed cold-cache IO measurement.
- Windows or macOS metric collection; those platforms skip collection gracefully.
- Shipping a general-purpose eBPF agent; the scripts are host-side diagnostics.

## Decisions

### External wrapper only

The wrapper owns process launch, sampling, reporting, file tracing, and perf orchestration. It does not modify `benchmark_runner` or any other existing source; it reads the runner's existing timing CSV.

- Why: the wrapper works for the shell, unit tests, and arbitrary commands, and changes no existing interfaces. Keeping `benchmark/` untouched also makes the toolkit easy to review and back out.
- Alternative: instrument `benchmark_runner` for per-run attribution. Deferred to a separate change because it changes an existing output contract and is not needed for external profiling.
- Consequence: `/proc` metrics are whole-process. Per-benchmark CPU or IO attribution requires running one benchmark per process; the report labels this limitation.

### `/proc` polling with final counters

The sampler reads `/proc` directly on a configurable interval (default 50 ms) and reads final `/proc/<pid>/io` and `/proc/<pid>/status` values after `waitid(..., WNOWAIT)` but before reaping.

- Why: no third-party dependency, works unprivileged, and avoids cgroup v2 ambiguity (this VM's cgroup is not delegated to the user).
- Alternatives: `psutil` (extra dependency), cgroup accounting (root cgroup reflects the whole VM), `getrusage` alone (no IO or fd data).
- `smaps_rollup` sampling is optional because it is more expensive than `status`; PSS is reported when enabled.
- Peak RSS is the maximum sampled tree RSS, with `wait4` `ru_maxrss` as the fallback for commands that exit between samples.

### eBPF for file tracing, strace as fallback

Per-path file tracing is implemented with bpftrace scripts. The wrapper starts the selected script with the target PID, saves its raw output next to the report, and records the output path. `--files strace` is an explicit, count-only fallback.

- Why: eBPF sees all syscalls, including direct `syscall()` calls and statically linked binaries, with far less code than an `LD_PRELOAD` shim. `opensnoop.bt` and `fileio.bt` are 28 and 69 lines respectively.
- Why not `LD_PRELOAD`: the shim needed 885 lines of libc hook boilerplate, missed static binaries and direct syscalls, and introduced its own crash modes. It was removed.
- Why not eBPF-only: the workspace cannot load BPF tracing programs (`CapEff=0`, no kernel BTF), and host-side eBPF does not see Kata guest syscalls. `strace` keeps count-level file diagnostics available there.
- `strace` is high-overhead and reports syscall counts, not per-path bytes; it is never enabled implicitly.
- `mmap` reads are not attributed by either backend; this remains documented.

### perf attach mode with capability probing

The wrapper launches the command itself and attaches `perf stat -p <pid>` or `perf record -p <pid>` to preserve accurate `wait4` rusage. Before collection it probes `/sys/bus/event_source/devices/cpu` and a minimal `perf stat` invocation, then requests only supported events.

- Why: running the command under `perf stat --` would make the wrapper's `wait4` see perf's rusage rather than the target's; attach keeps the primary metrics correct.
- Attach exit semantics will be verified during implementation; if `perf -p` does not terminate with the target, the wrapper will fall back to `perf stat --` and take CPU time from `task-clock`.
- Flamegraphs use `perf record -e cpu-clock -F 99 -g --call-graph dwarf` because release builds omit frame pointers. `--call-graph fp` is supported for the frame-pointer build.
- If perf is absent or attach fails, the run continues with no perf section and a single warning.

### Dedicated frame-pointer build recipe

`just build-perf` configures a separate `build/perf` tree with `-DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_BENCHMARKS=1 -DDISABLE_UNITY=1 -DCMAKE_C_FLAGS=-fno-omit-frame-pointer -DCMAKE_CXX_FLAGS=-fno-omit-frame-pointer`.

- Why: `CMAKE_CXX_FLAGS` is appended by DuckDB's CMake code, while `CMAKE_CXX_FLAGS_RELWITHDEBINFO` is overwritten at `CMakeLists.txt:1084`. A dedicated directory avoids clobbering an existing `build/reldebug`. Unity builds are disabled because the optimizer unity translation unit exceeds the workspace's 6 GB memory limit.
- Alternative: add a new `ENABLE_FRAME_POINTERS` CMake option. Deferred to avoid touching core build logic for what is currently fork-local tooling.

### eBPF as an optional host backend

The `ebpf/` scripts are standalone bpftrace programs. The wrapper checks `bpftrace` availability, capabilities, and `/sys/kernel/btf/vmlinux` before enabling them, and otherwise warns once and falls back.

- Why: the sandbox cannot load tracing programs, so eBPF must never be on the critical path. The scripts still provide the lowest-overhead option on a privileged host.
- Script output is saved raw alongside the report rather than parsed into the JSON schema, because bpftrace output formats vary by version.

### JSON/CSV report, no visualization dependency

The report is self-describing JSON plus an optional CSV series. Flamegraphs are the only generated graphic, and the wrapper falls back to folded stacks if a flamegraph renderer is unavailable.

- Why: keeps the tool dependency-free and composable with `jq`, spreadsheets, or future dashboards.

## Risks / Trade-offs

- **perf may be unusable in Kata despite software events** → Probe in the first implementation step; fall back to `/proc` metrics plus DuckDB's built-in profiler. Never fail the wrapped command.
- **eBPF scripts cannot be exercised here** → Keep them host-only, syntax-check them where possible, document prerequisites, and ensure the wrapper's fallback path is tested.
- **eBPF unavailable in the workspace** → Keep eBPF host-only, document prerequisites, and provide `--files strace` for count-level diagnostics. Never fail the wrapped command.
- **Warm page cache reports zero block IO** → Report logical `rchar`/`wchar` alongside `read_bytes`/`write_bytes`, and document cold-cache techniques such as `posix_fadvise`.
- **Sampling misses short bursts** → Use final counters after exit, keep the interval configurable, and retain the last sample if the kernel clears zombie `/proc` entries.
- **2 vCPUs make parallel benchmarks noisy** → Document the environment, recommend `--threads=2`, and report CPU utilization relative to available CPUs.
- **Per-benchmark metrics are whole-process** → Label them clearly and document running one benchmark per process for per-benchmark attribution.
- **No zombie `/proc/<pid>/io` access** → Keep the last live sample and report rusage peak RSS; document that very short IO may be missed.

## Migration Plan

No migration is required: all new behavior is opt-in, no existing benchmark source is modified, and default build/test output is unchanged. Rolling back means removing `devtools/` and reverting the `tools.nix` and `justfile` additions. The `tools.nix` change takes effect only after the host rebuilds the workspace image and replaces the pod; until then, `nix shell` provides the tools for the current session.
