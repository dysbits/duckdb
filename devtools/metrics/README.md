# DuckDB metrics toolkit

`duckdb_metrics.py` runs a command, samples its process tree from Linux
`/proc`, and writes a JSON report plus an optional CSV time series. Optional
backends add `perf` counters, flamegraphs, and eBPF tracing.

The tool is dependency-free by default (Python standard library only). All
external tools are detected at runtime and degrade to a warning plus the
portable `/proc` path when unavailable.

## Quick start

```bash
# One command, JSON report in /tmp/report.json
python3 devtools/metrics/duckdb_metrics.py \
  --out /tmp/report.json -- sleep 1

# Benchmark runner with per-benchmark timings
python3 devtools/metrics/duckdb_metrics.py \
  --out /tmp/micro.json --csv /tmp/micro.csv --per-benchmark \
  -- build/reldebug/benchmark/benchmark_runner \
     benchmark/micro/nulls/no_nulls_addition.benchmark

# File opens and per-file bytes (eBPF, privileged host only)
python3 devtools/metrics/duckdb_metrics.py \
  --ebpf auto --ebpf-script fileio.bt --out /tmp/files.json -- \
  build/perf/duckdb -c "SELECT * FROM 'data.parquet' LIMIT 1"

# Portable fallback: syscall counts only, higher overhead
python3 devtools/metrics/duckdb_metrics.py \
  --files strace --out /tmp/files.json -- <command>

# perf counters and a flamegraph
python3 devtools/metrics/duckdb_metrics.py \
  --perf stat --out /tmp/perf.json -- <command>
python3 devtools/metrics/duckdb_metrics.py \
  --flamegraph /tmp/flame.svg --out /tmp/flame.json -- <command>
```

The wrapper exits with the wrapped command's exit code. Command output is not
captured or modified.

## Report layout

| Section | Contents |
|---|---|
| `summary` | wall time, CPU, peak RSS/PSS, logical and block IO, fd/thread peaks, faults, context switches, `wait4` rusage |
| `series` | one row per sample (also written to CSV when `--csv` is used) |
| `files` | syscall counts from `strace`; eBPF output is recorded under `ebpf` |
| `benchmarks` | parsed `benchmark_runner` timings and per-benchmark statistics |
| `perf` | supported events, hardware-counter availability, data/flamegraph paths |
| `ebpf` | detection result and raw trace path when available |
| `environment` | kernel, CPU count, memory, git revision, tool availability |
| `warnings` | every fallback or missing-tool warning |

`--smaps` adds PSS sampling from `smaps_rollup`. It is more expensive than the
default `status` read and is off by default.

## Metric semantics

- **CPU** comes from `wait4` rusage for the launched process (all threads) and
  from cumulative `/proc/<pid>/stat` deltas for the process tree. `cpu_pct` can
  exceed 100% when threads run in parallel; `cpu_pct_of_nproc` divides by the
  available CPU count.
- **RSS** is the sum of resident memory across the tree, so shared pages can be
  counted more than once. `peak_rss_max_kb` is the largest single-process RSS.
  `peak_pss_kb` is only meaningful with `--smaps`.
- **Logical IO** (`rchar`/`wchar`) counts bytes that passed through read/write
  syscalls, including page-cache hits. **Block IO** (`read_bytes`/`write_bytes`)
  counts bytes that reached the block layer. Block IO can be zero on a warm page
  cache even when a query reads a large file.
- **File descriptors** and **threads** are peak sampled counts. The sampler uses
  a 50 ms default interval and a 5 ms burst for the first 100 ms, so short
  commands still record IO and CPU. Very short work can still be missed; lower
  `--interval` for targeted runs.
- **Final counters** are read after the child exits but before it is reaped.
  Some kernels deny `/proc/<pid>/io` for a zombie; the wrapper then keeps the
  last live sample and reports the rusage peak RSS. `read_bytes` can therefore
  be zero for sub-millisecond commands.
- **Context switches and faults** in `summary` come from `wait4` rusage for the
  root process; the series row sums the live process tree.

## File tracing

eBPF is the primary per-path backend. Where `bpftrace`, the required
capabilities, and kernel BTF are available:

```bash
python3 devtools/metrics/duckdb_metrics.py \
  --ebpf auto --ebpf-script fileio.bt --out /tmp/files.json -- <command>
```

- `opensnoop.bt` counts opens per path.
- `fileio.bt` tracks read/write bytes per path.
- `blockio.bt` and `offcpu.bt` cover block IO and off-CPU time.

The wrapper starts bpftrace with the target PID and saves raw output next to the
report as `ebpf-<script>.txt`; the path is recorded under `ebpf.output`. The
output is not parsed into the JSON schema because bpftrace formats vary between
releases.

Where eBPF is unavailable, use the explicit `strace` fallback. It reports
syscall counts only, not per-path bytes, and adds significant overhead:

```bash
python3 devtools/metrics/duckdb_metrics.py --files strace --out /tmp/strace.json -- <command>
```

Limitations common to both backends:

- Memory-mapped reads are not attributed to a path.
- eBPF requires a privileged host; `strace` works unprivileged but is slow.
- The workspace cannot load eBPF programs, so file tracing there is limited to
  `--files strace`.

## Benchmark correlation

`--per-benchmark` detects `benchmark_runner`, injects a temporary `--out=` file
when the command does not already specify one, and merges the
`name<TAB>run<TAB>timing` rows into the report.

The `/proc` metrics are **whole-process**, not per benchmark, because the runner
executes all benchmarks in one process. For per-benchmark memory or CPU
attribution, run that benchmark in its own process (one benchmark pattern per
invocation). Native per-run metrics inside `benchmark_runner` are deferred to a
separate change; this toolkit does not modify benchmark sources.

## perf

Software events work in the workspace:

```bash
perf stat -e task-clock,page-faults,context-switches -- true
perf record -e cpu-clock -g --call-graph dwarf -- sleep 0.1
```

The workspace has no hardware PMU (`/sys/bus/event_source/devices/` exposes only
`software`, `msr`, and `breakpoint`), so `cycles`, `instructions`, cache and
branch counters are unavailable. The wrapper probes this and requests software
events only.

`--perf record` attaches to the running command; `--flamegraph out.svg` folds
`perf script` output and uses `inferno-flamegraph` or `flamegraph.pl` when
available, otherwise writes `out.svg.folded`. See
[`../perf/README.md`](../perf/README.md) for frame-pointer builds and recipes.

## eBPF

`--ebpf auto` detects `bpftrace`, capabilities, and kernel BTF. The workspace has
none of the required privileges or BTF, so it warns once and falls back. On a
privileged host, the scripts in [`ebpf/`](ebpf/) provide open/close, per-file IO,
block IO, and off-CPU tracing. `--ebpf on` fails clearly when prerequisites are
missing. See [`ebpf/README.md`](ebpf/README.md).

## Workspace probe results

- 2 vCPUs, 6 GB RAM, no swap.
- `perf` software events and DWARF call graphs work; hardware PMU counters do
  not.
- eBPF tracing cannot load: `CapEff=0`, no `/sys/kernel/btf/vmlinux`.
- `smaps_rollup`, `schedstat`, and `clear_refs=5` are readable for own
  processes; `/proc/<pid>/io` is denied after the process becomes a zombie.
- Page cache is warm for repository and system files, so most block-IO counters
  are zero unless the workload reads new data.

## Validation

Measured with `duckdb -c "SELECT sum(i) FROM range(200000000) t(i)"` (GNU time
and wrapper run sequentially; 6 GB / 2 vCPU workspace):

| Metric | GNU time | Wrapper | Agreement |
|---|---|---|---|
| Peak RSS | 26,256 KiB | 26,212 KiB | within 1% |
| User CPU | 0.16 s | 0.149 s | within 7% |
| System CPU | 0.00 s | 0.019 s | small absolute difference |

Sampling overhead on a ~1 s query is below the run-to-run noise on this 2-vCPU
VM (median wrapper 1,039 ms vs raw 1,098 ms over 12 alternating runs); there is
no measurable overhead above 2%. File tracing was not validated with eBPF in
this workspace because eBPF cannot load; `--files strace` was validated against
direct `strace -c` output.

