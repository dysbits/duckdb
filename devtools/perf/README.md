# perf profiling for DuckDB

DuckDB release builds omit frame pointers, so `perf record --call-graph fp`
produces truncated stacks unless you build with frame pointers. `just build-perf`
does that in a dedicated directory:

```bash
just build-perf                 # build/perf
just build-perf build/my-perf   # custom directory
```

The recipe configures `RelWithDebInfo` with benchmarks enabled and passes
`-fno-omit-frame-pointer` through `CMAKE_C_FLAGS` and `CMAKE_CXX_FLAGS`. DuckDB
replaces `CMAKE_CXX_FLAGS_RELWITHDEBINFO` with the release flags plus `-g`, so
the frame-pointer flag must not be passed through the per-configuration
variable. Unity builds are disabled so the build fits the workspace's 6 GB
memory limit.

## Quick checks

```bash
build/perf/duckdb --version
build/perf/benchmark/benchmark_runner --list
```

## perf stat

Software events work without a hardware PMU:

```bash
perf stat -e task-clock,page-faults,context-switches,cpu-migrations \
  -- build/perf/duckdb -c "SELECT sum(i) FROM range(100000000) t(i)"
```

Add hardware events only on a host that exposes a PMU:

```bash
perf stat -d -- build/perf/duckdb -c "SELECT 42"
```

The development workspace exposes only `software`, `msr`, and `breakpoint` event
sources, so `cycles`, `instructions`, and cache counters are unavailable there.

## perf record and flamegraphs

```bash
perf record -F 999 -g --call-graph dwarf -o /tmp/duckdb.data \
  -- build/perf/duckdb -c "SELECT sum(i) FROM range(100000000) t(i)"
perf report -i /tmp/duckdb.data --stdio

perf script -i /tmp/duckdb.data > /tmp/duckdb.stacks
stackcollapse-perf.pl /tmp/duckdb.stacks > /tmp/duckdb.folded
flamegraph.pl /tmp/duckdb.folded > /tmp/duckdb.svg
```

With the frame-pointer build, `--call-graph fp` is cheaper than DWARF:

```bash
perf record -F 999 -g --call-graph fp -o /tmp/duckdb-fp.data \
  -- build/perf/duckdb -c "SELECT 42"
```

The metrics wrapper can do the recording and folding:

```bash
python3 devtools/metrics/duckdb_metrics.py \
  --flamegraph /tmp/duckdb.svg --out /tmp/duckdb.json \
  -- build/perf/benchmark/benchmark_runner \
     benchmark/micro/nulls/no_nulls_addition.benchmark
```

It attaches to the launched process, keeps `perf.data` in a temporary directory
(its path is recorded in the report), and emits folded stacks when no flamegraph
renderer is installed.

## Call-graph choice

| Build | Recommended `--call-graph` | Notes |
|---|---|---|
| `build/reldebug`, `build/perf` | `dwarf` | Uses debug info; more overhead per sample |
| `build/perf` | `fp` | Cheap unwinding; requires `-fno-omit-frame-pointer` |

## Caveats

- The workspace has 2 vCPUs; parallel queries oversubscribe the machine.
  Compare runs with `--threads=2`.
- `perf` cannot access hardware counters without a PMU. The wrapper marks them
  unavailable instead of failing.
- `perf record -p` attaches to one process. Threads are included; child
  processes are not. Use the process-tree metrics from `duckdb_metrics.py` for
  whole-tree CPU and IO.
- `perf` needs `kernel.perf_event_paranoid <= 2` for user-space per-process
  events; the workspace is at 2.
- Use `--no-inline` on `perf report` if symbols look duplicated, or `perf
  report --hierarchy` for a tree view.
