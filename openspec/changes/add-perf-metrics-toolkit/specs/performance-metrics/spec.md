# Spec Delta

## Purpose

Provide repeatable, low-overhead performance metrics for DuckDB development so regressions in CPU, memory, IO, and file behavior can be attributed without ad-hoc tooling.

## ADDED Requirements

### Requirement: Process metrics report

The metrics wrapper SHALL run a command and produce a JSON report plus an optional CSV time series containing wall time, user and system CPU time, CPU utilization, RSS and PSS, logical and block IO byte counts, syscall counts, open file-descriptor count, thread count, page faults, and context switches.

#### Scenario: Successful command

- **WHEN** the wrapper runs a command that exits successfully
- **THEN** the JSON report contains the command, an exit code of zero, and every supported metric family with non-negative values

#### Scenario: Command failure

- **WHEN** the wrapped command exits non-zero
- **THEN** the report records the same non-zero exit code and the wrapper exits with that code

#### Scenario: CSV series requested

- **WHEN** the wrapper is invoked with a CSV output path
- **THEN** the CSV contains one row per sample with a monotonic timestamp and the sampled metric columns

### Requirement: Process tree and final counters

The wrapper SHALL monitor descendant processes and SHALL read final counters before the root process is reaped so that short-lived work is not omitted.

#### Scenario: Child processes

- **WHEN** the wrapped command spawns child processes
- **THEN** CPU and IO totals include the descendants and the report identifies how many processes were observed

#### Scenario: Fast exit

- **WHEN** the wrapped command exits before the next sampling tick
- **THEN** the final CPU and IO counters are read from the process before it is reaped and appear in the report

### Requirement: File IO tracing

When eBPF is available and file tracing is requested, the toolkit SHALL run the selected eBPF script for the target process, save its raw output alongside the report, and record the output path. When eBPF is unavailable, file tracing SHALL be limited to an explicit `strace` fallback that reports syscall counts, or disabled.

#### Scenario: eBPF available

- **WHEN** eBPF prerequisites are present and file tracing is requested
- **THEN** the selected eBPF script captures per-path opens, closes, or bytes and the report records the raw output path

#### Scenario: eBPF unavailable

- **WHEN** any eBPF prerequisite is missing
- **THEN** the wrapper warns, does not fail the run, and file tracing is limited to `--files strace` counts or disabled

#### Scenario: strace fallback

- **WHEN** the wrapper is invoked with `--files strace`
- **THEN** the report contains syscall call counts for the traced command and its children

#### Scenario: Memory-mapped access

- **WHEN** a process reads a file through memory mapping instead of read-family syscalls
- **THEN** those bytes are not attributed to the path and the documentation states this limitation

### Requirement: Benchmark timing correlation

The wrapper SHALL detect `benchmark_runner` invocations and merge the runner's timing CSV into the report without changing the runner's own output.

#### Scenario: Benchmark runner wrapped

- **WHEN** the wrapped command is `benchmark_runner` and per-benchmark reporting is enabled
- **THEN** the report includes per-benchmark run timings and timing statistics alongside clearly labeled whole-process metrics

#### Scenario: Other commands

- **WHEN** the wrapped command is not `benchmark_runner`
- **THEN** the report omits benchmark timing sections and the command's output is unchanged

### Requirement: perf integration

The toolkit SHALL offer optional `perf stat` and sampled `perf record` modes, SHALL probe available events before collection, and SHALL report unsupported counters as unavailable without failing the run.

#### Scenario: No hardware PMU

- **WHEN** the host exposes no hardware PMU
- **THEN** the report contains supported software events and marks hardware counters as unavailable

#### Scenario: perf missing

- **WHEN** `perf` is not installed or cannot attach to the target process
- **THEN** the wrapper continues without perf data, emits a warning, and preserves the command's exit status

#### Scenario: Flamegraph requested

- **WHEN** flamegraph output is requested and sampling succeeds
- **THEN** the toolkit writes an SVG or folded-stack file at the requested path

### Requirement: eBPF detection and fallback

The toolkit SHALL provide eBPF scripts for host use and SHALL enable them only when `bpftrace`, the required capabilities, and kernel BTF are available; otherwise it SHALL warn and continue with `/proc` metrics and the optional `strace` count fallback.

#### Scenario: eBPF available

- **WHEN** `bpftrace`, the required capabilities, and kernel BTF are available and eBPF mode is enabled
- **THEN** the requested traces are collected and saved alongside the report

#### Scenario: eBPF unavailable

- **WHEN** any eBPF prerequisite is missing
- **THEN** the wrapper warns once, falls back to `/proc` metrics, and does not fail the run

### Requirement: Frame-pointer profiling build

The repository SHALL provide a build recipe that configures a `RelWithDebInfo` build with `-fno-omit-frame-pointer` and benchmark binaries enabled.

#### Scenario: Build recipe invoked

- **WHEN** the profiling build recipe runs in a clean checkout
- **THEN** it produces `duckdb` and `benchmark_runner` with frame pointers and debug information

#### Scenario: Default build unaffected

- **WHEN** the profiling build recipe runs
- **THEN** it writes to its own build directory and does not modify the release or reldebug build trees

### Requirement: Metric semantics documentation

Documentation SHALL explain the difference between logical and block IO, peak versus current RSS, the workspace's missing PMU and eBPF privileges, and how to obtain cold-cache IO measurements.

#### Scenario: Interpreting IO

- **WHEN** a reader consults the metrics documentation
- **THEN** it explains why block read counters can be zero on a warm page cache and points to logical byte counters

#### Scenario: Environment limits

- **WHEN** a reader runs the toolkit in the development workspace
- **THEN** the documentation states that hardware performance counters and eBPF tracing are unavailable and which fallbacks apply
