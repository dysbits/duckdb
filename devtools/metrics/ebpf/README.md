# eBPF tracing scripts

These scripts are the lowest-overhead backend for file-open, file-IO, block-IO,
and off-CPU tracing. They are **host-only**: the development workspace cannot
load tracing programs because it runs unprivileged without kernel BTF.

## Prerequisites

- `bpftrace` on `PATH`
- root, `CAP_BPF`, `CAP_PERFMON`, or `CAP_SYS_ADMIN`
- kernel BTF at `/sys/kernel/btf/vmlinux`
- tracefs mounted at `/sys/kernel/tracing` or `/sys/kernel/debug/tracing`

The wrapper checks all of these before enabling eBPF. `--ebpf auto` warns and
falls back to `/proc` plus the `--files strace` count backend; `--ebpf on` fails
with the missing prerequisites.

## Scripts

| Script | Scope | Usage |
|---|---|---|
| `opensnoop.bt` | opens per path for one process | `sudo bpftrace opensnoop.bt <pid>` |
| `fileio.bt` | read/write bytes per path for one process | `sudo bpftrace fileio.bt <pid>` |
| `blockio.bt` | block requests and bytes per process | `sudo bpftrace blockio.bt` |
| `offcpu.bt` | off-CPU time histogram for one process | `sudo bpftrace offcpu.bt <pid>` |

`fileio.bt` covers `read`/`write`-family syscalls. Memory-mapped IO and syscalls
that bypass the traced tracepoints are not counted.

## Wrapper integration

`duckdb_metrics.py --ebpf auto|on|off` detects the prerequisites and, when
available, runs the selected script with the target PID:

```bash
python3 devtools/metrics/duckdb_metrics.py \
  --ebpf auto --ebpf-script opensnoop.bt \
  --out /tmp/report.json -- <command>
```

Raw bpftrace output is saved next to the report as `ebpf-<script>.txt` and the
path is recorded under `ebpf.output`. The wrapper does not parse bpftrace output
into the JSON schema because the format varies between bpftrace releases.

## Workspace limitation

Probing the workspace gives:

- `bpftrace` can be installed through `tools.nix`, but
- `/sys/kernel/btf/vmlinux` is absent, and
- the process has `CapEff=0`.

`--ebpf on` therefore fails there by design. Use `--ebpf auto` (the default) or
`--ebpf off` in the sandbox, and run the scripts on a privileged host for eBPF
measurements.
