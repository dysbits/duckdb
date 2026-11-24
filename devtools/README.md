# Development tools

Fork-local development tooling that is not part of the DuckDB build or test
suite.

| Path | Purpose |
|---|---|
| [`metrics/`](metrics/) | Process CPU/memory/IO metrics, per-path file tracing, benchmark timing correlation, optional perf/eBPF backends |
| [`perf/`](perf/) | Frame-pointer build and `perf stat`/`record`/flamegraph recipes |

## Quick start

```bash
# Build the perf-friendly binary once
just build-perf

# Profile a benchmark with process metrics and perf counters
python3 devtools/metrics/duckdb_metrics.py \
  --out /tmp/report.json --csv /tmp/report.csv \
  --perf stat --per-benchmark \
  -- build/perf/benchmark/benchmark_runner \
     benchmark/micro/nulls/no_nulls_addition.benchmark

# Per-path file tracing on a privileged host
python3 devtools/metrics/duckdb_metrics.py \
  --ebpf auto --ebpf-script fileio.bt --out /tmp/files.json -- <command>
```

See [`metrics/README.md`](metrics/README.md) for metric definitions, fallbacks,
and limitations.

## Workspace constraints

The development workspace has 2 vCPUs and 6 GB RAM, exposes no hardware PMU, and
cannot load eBPF tracing programs (no `CAP_BPF`, no kernel BTF). All tools detect
these conditions and fall back:

- `perf` collects software events and supports sampling/flamegraphs; hardware
  counters are reported as unavailable.
- eBPF is skipped with a warning; `/proc` sampling still works, and
  `--files strace` provides syscall counts where needed.
- Block-IO counters can be zero on a warm page cache; logical byte counters
  (`rchar`/`wchar`) are always reported.

The packages used by these tools are listed in the repository `tools.nix`. They
become available in a new workspace image after `ai-workspace start`; for the
current session use `nix shell` or `nix profile add`.
