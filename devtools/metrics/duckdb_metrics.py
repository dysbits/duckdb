#!/usr/bin/env python3
"""Run a command and report process, IO, and profiling metrics.

The tool is intentionally dependency-free: it reads Linux /proc directly and
uses optional external programs (perf, strace, bpftrace) when they are
available. See devtools/metrics/README.md for usage and metric semantics.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import select
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_INTERVAL = 0.05
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
REPO_ROOT = Path(__file__).resolve().parents[2]
EBPF_DIR = Path(__file__).resolve().parent / "ebpf"

SERIES_FIELDS = [
    "t_s",
    "processes",
    "cpu_user_s",
    "cpu_sys_s",
    "rss_kb",
    "rss_max_kb",
    "pss_kb",
    "fds",
    "threads",
    "rchar",
    "wchar",
    "read_bytes",
    "write_bytes",
    "syscr",
    "syscw",
    "minflt",
    "majflt",
    "vol_ctx",
    "nvol_ctx",
    "sched_run_ns",
    "sched_wait_ns",
]

SOFTWARE_PERF_EVENTS = [
    "task-clock",
    "page-faults",
    "context-switches",
    "cpu-migrations",
    "minor-faults",
    "major-faults",
]

HARDWARE_PERF_EVENTS = [
    "cycles",
    "instructions",
    "cache-references",
    "cache-misses",
    "branches",
    "branch-misses",
]


class Warnings:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)
        if not self.quiet:
            print(f"warning: {message}", file=sys.stderr)


def read_text(path: str | Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def read_int(path: str | Path, default: int = 0) -> int:
    try:
        return int(read_text(path).strip())
    except (OSError, ValueError):
        return default


def read_kb(field: str) -> int:
    try:
        value = field.split()[0]
        return int(value)
    except (IndexError, ValueError):
        return 0


def parse_proc_stat(pid: int) -> dict | None:
    try:
        data = read_text(f"/proc/{pid}/stat")
    except OSError:
        return None
    # comm may contain spaces and parentheses; fields resume after the last ')'.
    rparen = data.rfind(")")
    if rparen < 0:
        return None
    fields = data[rparen + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return {
            "pid": pid,
            "state": fields[0],
            "ppid": int(fields[1]),
            "minflt": int(fields[7]),
            "majflt": int(fields[9]),
            "utime": int(fields[11]),
            "stime": int(fields[12]),
            "cutime": int(fields[13]),
            "cstime": int(fields[14]),
            "threads": int(fields[17]),
            "starttime": int(fields[19]),
        }
    except (IndexError, ValueError):
        return None


def parse_proc_status(pid: int) -> dict:
    status: dict[str, int] = {}
    try:
        lines = read_text(f"/proc/{pid}/status").splitlines()
    except OSError:
        return status
    wanted = {
        "VmRSS": "rss_kb",
        "VmHWM": "vmhwm_kb",
        "VmSize": "vsize_kb",
        "Threads": "threads_status",
        "voluntary_ctxt_switches": "vol_ctx",
        "nonvoluntary_ctxt_switches": "nvol_ctx",
    }
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in wanted:
            status[wanted[key]] = read_kb(value.strip())
    return status


def parse_proc_io(pid: int) -> dict:
    result = {
        "rchar": 0,
        "wchar": 0,
        "syscr": 0,
        "syscw": 0,
        "read_bytes": 0,
        "write_bytes": 0,
        "cancelled_write_bytes": 0,
    }
    try:
        lines = read_text(f"/proc/{pid}/io").splitlines()
    except OSError:
        return result
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in result:
            try:
                result[key] = int(value.strip())
            except ValueError:
                pass
    return result


def parse_smaps_rollup(pid: int) -> dict:
    result = {
        "pss_kb": 0,
        "rss_kb": 0,
        "anonymous_kb": 0,
        "shared_clean_kb": 0,
        "shared_dirty_kb": 0,
        "private_clean_kb": 0,
        "private_dirty_kb": 0,
    }
    try:
        lines = read_text(f"/proc/{pid}/smaps_rollup").splitlines()
    except OSError:
        return result
    mapping = {
        "Pss": "pss_kb",
        "Rss": "rss_kb",
        "Anonymous": "anonymous_kb",
        "Shared_Clean": "shared_clean_kb",
        "Shared_Dirty": "shared_dirty_kb",
        "Private_Clean": "private_clean_kb",
        "Private_Dirty": "private_dirty_kb",
    }
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in mapping:
            result[mapping[key]] = read_kb(value.strip())
    return result


def parse_schedstat(pid: int) -> dict:
    try:
        fields = read_text(f"/proc/{pid}/schedstat").split()
        return {
            "sched_run_ns": int(fields[0]),
            "sched_wait_ns": int(fields[1]),
            "sched_slices": int(fields[2]),
        }
    except (OSError, IndexError, ValueError):
        return {"sched_run_ns": 0, "sched_wait_ns": 0, "sched_slices": 0}


def count_fds(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return 0


def children_of(pid: int) -> set[int] | None:
    result: set[int] = set()
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return None
    readable = False
    for tid in tids:
        try:
            data = read_text(task_dir / tid / "children").strip()
            readable = True
        except OSError:
            continue
        if data:
            for part in data.split():
                try:
                    result.add(int(part))
                except ValueError:
                    pass
    return result if readable else None


def parse_ppid(pid: int) -> int | None:
    try:
        data = read_text(f"/proc/{pid}/stat")
    except OSError:
        return None
    rparen = data.rfind(")")
    if rparen < 0:
        return None
    fields = data[rparen + 2 :].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def build_ppid_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        ppid = parse_ppid(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    return children


def read_process(pid: int, want_smaps: bool) -> dict | None:
    stat = parse_proc_stat(pid)
    if stat is None:
        return None
    status = parse_proc_status(pid)
    io = parse_proc_io(pid)
    sched = parse_schedstat(pid)
    smaps = parse_smaps_rollup(pid) if want_smaps else {}
    result = {
        **stat,
        **status,
        **io,
        **sched,
        **smaps,
        "fds": count_fds(pid),
    }
    result.setdefault("rss_kb", 0)
    result.setdefault("vmhwm_kb", 0)
    result.setdefault("threads_status", result.get("threads", 0))
    result.setdefault("vol_ctx", 0)
    result.setdefault("nvol_ctx", 0)
    result.setdefault("pss_kb", 0)
    return result


def collect_tree(root_pid: int, want_smaps: bool) -> dict[int, dict]:
    procs: dict[int, dict] = {}
    stack = [root_pid]
    ppid_map: dict[int, list[int]] | None = None
    while stack:
        pid = stack.pop()
        if pid in procs:
            continue
        proc = read_process(pid, want_smaps)
        if proc is None:
            continue
        procs[pid] = proc
        if ppid_map is None:
            kids = children_of(pid)
            if kids is None:
                ppid_map = build_ppid_map()
                kids = set(ppid_map.get(pid, []))
        else:
            kids = set(ppid_map.get(pid, []))
        stack.extend(kids)
    return procs


def aggregate(procs: dict[int, dict], want_smaps: bool) -> dict:
    row = {field: 0 for field in SERIES_FIELDS if field not in ("t_s", "processes")}
    row["processes"] = len(procs)
    row["rss_max_kb"] = 0
    for proc in procs.values():
        row["cpu_user_s"] += (proc.get("utime", 0) + proc.get("cutime", 0)) / CLK_TCK
        row["cpu_sys_s"] += (proc.get("stime", 0) + proc.get("cstime", 0)) / CLK_TCK
        row["rss_kb"] += proc.get("rss_kb", 0)
        row["rss_max_kb"] = max(row["rss_max_kb"], proc.get("rss_kb", 0))
        if want_smaps:
            row["pss_kb"] += proc.get("pss_kb", 0)
        row["fds"] += proc.get("fds", 0)
        row["threads"] += proc.get("threads_status", proc.get("threads", 0))
        for field in (
            "rchar",
            "wchar",
            "read_bytes",
            "write_bytes",
            "syscr",
            "syscw",
            "minflt",
            "majflt",
            "vol_ctx",
            "nvol_ctx",
            "sched_run_ns",
            "sched_wait_ns",
        ):
            row[field] += proc.get(field, 0)
    return row


class Sampler:
    def __init__(self, root_pid: int, interval: float, want_smaps: bool) -> None:
        self.root_pid = root_pid
        self.interval = interval
        self.want_smaps = want_smaps
        self.start = time.monotonic()
        self.seen: set[int] = set()
        self.peak_rss_kb = 0
        self.peak_rss_max_kb = 0
        self.peak_pss_kb = 0
        self.peak_fds = 0
        self.peak_threads = 0
        self.peak_processes = 0

    def sample(self) -> dict | None:
        procs = collect_tree(self.root_pid, self.want_smaps)
        if not procs:
            return None
        row = aggregate(procs, self.want_smaps)
        row["t_s"] = round(time.monotonic() - self.start, 6)
        self._update_peaks(row, procs)
        return row

    def final(self) -> dict | None:
        procs = collect_tree(self.root_pid, self.want_smaps)
        if not procs:
            return None
        row = aggregate(procs, self.want_smaps)
        row["t_s"] = round(time.monotonic() - self.start, 6)
        self._update_peaks(row, procs)
        return row

    def _update_peaks(self, row: dict, procs: dict[int, dict]) -> None:
        self.seen.update(procs)
        self.peak_rss_kb = max(self.peak_rss_kb, int(row.get("rss_kb", 0)))
        self.peak_rss_max_kb = max(self.peak_rss_max_kb, int(row.get("rss_max_kb", 0)))
        self.peak_pss_kb = max(self.peak_pss_kb, int(row.get("pss_kb", 0)))
        self.peak_fds = max(self.peak_fds, int(row.get("fds", 0)))
        self.peak_threads = max(self.peak_threads, int(row.get("threads", 0)))
        self.peak_processes = max(self.peak_processes, len(procs))


def waitid_nohang(pid: int) -> bool:
    if hasattr(os, "WNOWAIT"):
        try:
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            return True
        return info is not None
    return False


def reap(pid: int) -> tuple[int, object | None]:
    try:
        _, status, rusage = os.wait4(pid, 0)
        return status, rusage
    except ChildProcessError:
        return 0, None


def rusage_dict(rusage: object | None) -> dict:
    if rusage is None:
        return {}
    return {
        "user_s": round(rusage.ru_utime, 6),
        "sys_s": round(rusage.ru_stime, 6),
        "max_rss_kb": int(rusage.ru_maxrss),
        "minor_faults": int(rusage.ru_minflt),
        "major_faults": int(rusage.ru_majflt),
        "voluntary_ctxt_switches": int(rusage.ru_nvcsw),
        "nonvoluntary_ctxt_switches": int(rusage.ru_nivcsw),
    }


def is_benchmark_runner(command: list[str]) -> bool:
    return any(Path(part).name == "benchmark_runner" for part in command[:1])


def benchmark_output_arg(command: list[str]) -> bool:
    return any(arg.startswith("--out=") for arg in command)


def parse_benchmark_csv(path: Path) -> dict:
    rows = []
    if not path.exists():
        return {"source": str(path), "rows": [], "stats": []}
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("name\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, run, timing = parts[0], parts[1], parts[2]
        try:
            run_number = int(run)
        except ValueError:
            continue
        try:
            seconds = float(timing)
        except ValueError:
            seconds = None
        rows.append({"name": name, "run": run_number, "timing_s": seconds})
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["timing_s"] is not None:
            groups.setdefault(row["name"], []).append(row["timing_s"])
    stats = []
    for name, values in groups.items():
        values = sorted(values)
        count = len(values)
        if count == 0:
            continue
        stats.append(
            {
                "name": name,
                "runs": count,
                "min_s": round(values[0], 6),
                "median_s": round(statistics.median(values), 6),
                "mean_s": round(sum(values) / count, 6),
                "max_s": round(values[-1], 6),
            }
        )
    return {"source": str(path), "rows": rows, "stats": stats}


def detect_hardware_pmu() -> bool:
    return os.path.exists("/sys/bus/event_source/devices/cpu")


class PerfCollector:
    def __init__(
        self, pid: int, args: argparse.Namespace, warnings: Warnings, out_dir: Path
    ) -> None:
        self.pid = pid
        self.args = args
        self.warnings = warnings
        self.out_dir = out_dir
        self.process: subprocess.Popen | None = None
        self.output_path: Path | None = None
        self.data_path: Path | None = None
        self.events: dict[str, dict] = {}
        self.available = False
        self.hardware = detect_hardware_pmu()
        self.stderr_path: Path | None = None
        self._started = False

    def start(self) -> None:
        perf = shutil.which("perf")
        if perf is None:
            self.warnings.add("perf not found; perf metrics are unavailable")
            return
        try:
            self.process = None
            if self.args.perf == "stat":
                self.output_path = self.out_dir / "perf-stat.csv"
                self.stderr_path = self.out_dir / "perf-stat.err"
                events = self.args.perf_events
                if not events:
                    events = list(SOFTWARE_PERF_EVENTS)
                    if self.hardware:
                        events += HARDWARE_PERF_EVENTS
                command = [
                    perf,
                    "stat",
                    "-p",
                    str(self.pid),
                    "-x,",
                    "-e",
                    ",".join(events),
                    "-o",
                    str(self.output_path),
                ]
            elif self.args.perf == "record":
                self.data_path = self.out_dir / "perf.data"
                self.stderr_path = self.out_dir / "perf-record.err"
                command = [
                    perf,
                    "record",
                    "-p",
                    str(self.pid),
                    "-F",
                    str(self.args.perf_freq),
                    "-g",
                    "--call-graph",
                    self.args.call_graph,
                    "-o",
                    str(self.data_path),
                ]
            else:
                return
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=open(self.stderr_path, "w", encoding="utf-8"),
            )
            self._started = True
            self.available = True
        except OSError as exc:
            self.warnings.add(f"perf could not start: {exc}")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if (
            self.process.returncode
            not in (0, None, -signal.SIGINT, 128 + signal.SIGINT)
            and self.stderr_path
        ):
            error = read_text(self.stderr_path).strip()
            if error:
                self.warnings.add(
                    f"perf exited with status {self.process.returncode}: {error.splitlines()[0]}"
                )
        if self.args.perf == "stat" and self.output_path and self.output_path.exists():
            self._parse_stat(self.output_path)
        if self.args.flamegraph:
            if self.data_path and self.data_path.exists():
                self._write_flamegraph()
            else:
                self.warnings.add("perf record produced no data; no flamegraph written")

    def _parse_stat(self, path: Path) -> None:
        for line in read_text(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            event = parts[2].split(":")[0]
            raw = parts[0].strip()
            try:
                value = float(raw)
                self.events[event] = {"value": value, "unit": parts[1], "status": "ok"}
            except ValueError:
                self.events[event] = {"value": None, "unit": parts[1], "status": raw}

    def _write_flamegraph(self) -> None:
        perf = shutil.which("perf")
        if perf is None:
            return
        try:
            result = subprocess.run(
                [perf, "script", "-i", str(self.data_path)],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.warnings.add(f"could not read perf samples: {exc}")
            return
        if result.returncode != 0:
            self.warnings.add(
                f"perf script failed: {result.stderr.strip().splitlines()[:1]}"
            )
            return
        folded = fold_perf_script(result.stdout)
        if not folded:
            self.warnings.add("perf collected no samples; no flamegraph written")
            return
        target = Path(self.args.flamegraph)
        folded_path = target.with_suffix(target.suffix + ".folded")
        folded_path.parent.mkdir(parents=True, exist_ok=True)
        folded_text = (
            "\n".join(f"{stack} {count}" for stack, count in folded.items()) + "\n"
        )
        folded_path.write_text(folded_text)
        renderer = shutil.which("inferno-flamegraph") or shutil.which("flamegraph.pl")
        if renderer and target.suffix.lower() == ".svg":
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(target, "w", encoding="utf-8") as handle:
                    render = subprocess.run(
                        [renderer],
                        input=folded_text,
                        stdout=handle,
                        text=True,
                        timeout=120,
                        check=False,
                    )
                if render.returncode == 0:
                    self.warnings.add(f"flamegraph written to {target}")
                else:
                    self.warnings.add(
                        f"flamegraph renderer failed; wrote folded stacks to {folded_path}"
                    )
                    self.args.flamegraph = str(folded_path)
            except OSError as exc:
                self.warnings.add(
                    f"could not render flamegraph: {exc}; wrote {folded_path}"
                )
                self.args.flamegraph = str(folded_path)
        else:
            self.warnings.add(
                f"flamegraph renderer not found; wrote folded stacks to {folded_path}"
            )
            self.args.flamegraph = str(folded_path)

    def report(self) -> dict:
        report = {
            "mode": self.args.perf if self.args.perf != "off" else "off",
            "available": self.available,
            "hardware_counters": self.hardware,
            "events": self.events,
        }
        if self.data_path:
            report["data_file"] = str(self.data_path)
        if self.args.flamegraph:
            report["flamegraph"] = self.args.flamegraph
        return report


def fold_perf_script(output: str) -> Counter:
    folded: Counter = Counter()
    frames: list[str] = []
    stack_re = re.compile(
        r"^\s+[0-9a-fA-F]+\s+(.*?)(?:\+0x[0-9a-fA-F]+)?\s+\(([^)]+)\)\s*$"
    )
    for line in output.splitlines():
        if not line.strip():
            if frames:
                folded[";".join(reversed(frames))] += 1
                frames = []
            continue
        if line.lstrip().startswith("#"):
            continue
        match = stack_re.match(line)
        if match:
            frames.append(match.group(1).strip())
        elif frames:
            # A non-stack line ends the current event block.
            folded[";".join(reversed(frames))] += 1
            frames = []
    if frames:
        folded[";".join(reversed(frames))] += 1
    return folded


def cap_effective() -> int:
    try:
        for line in read_text("/proc/self/status").splitlines():
            if line.startswith("CapEff:"):
                return int(line.split(":", 1)[1].strip(), 16)
    except OSError:
        pass
    return 0


def detect_ebpf() -> dict:
    reasons = []
    bpftrace = shutil.which("bpftrace")
    if bpftrace is None:
        reasons.append("bpftrace not found")
    btf = os.path.exists("/sys/kernel/btf/vmlinux")
    if not btf:
        reasons.append("/sys/kernel/btf/vmlinux is missing")
    caps = cap_effective()
    # CAP_SYS_ADMIN (21), CAP_PERFMON (38), CAP_BPF (39)
    privileged = os.geteuid() == 0 or bool(caps & ((1 << 21) | (1 << 38) | (1 << 39)))
    if not privileged:
        reasons.append("missing CAP_BPF/CAP_PERFMON/CAP_SYS_ADMIN")
    return {
        "available": not reasons,
        "bpftrace": bpftrace,
        "btf": btf,
        "privileged": privileged,
        "reasons": reasons,
    }


def start_ebpf(
    pid: int, script_name: str, out_dir: Path, warnings: Warnings
) -> tuple[subprocess.Popen | None, Path | None]:
    detection = detect_ebpf()
    if not detection["available"]:
        return None, None
    script = EBPF_DIR / script_name
    if not script.exists():
        warnings.add(f"eBPF script not found: {script}")
        return None, None
    out_path = out_dir / f"ebpf-{script.stem}.txt"
    try:
        handle = open(out_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [detection["bpftrace"], str(script), str(pid)],
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        handle.close()
        return process, out_path
    except OSError as exc:
        warnings.add(f"could not start bpftrace: {exc}")
        return None, None


def stop_ebpf(process: subprocess.Popen | None, warnings: Warnings) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.returncode not in (0, None):
        warnings.add(f"bpftrace exited with status {process.returncode}")


def build_strace_command(command: list[str], output: Path) -> list[str]:
    return [
        "strace",
        "-f",
        "-c",
        "-o",
        str(output),
        "-e",
        "trace=openat,open,creat,close,read,pread64,pwrite64,write,readv,writev,preadv,pwritev",
        "--",
        *command,
    ]


def parse_strace_counts(path: Path) -> dict:
    syscalls = []
    if not path.exists():
        return {"mode": "strace", "syscalls": [], "source": str(path)}
    for line in read_text(path).splitlines():
        line = line.strip()
        if (
            not line
            or line.startswith("-")
            or line.startswith("%")
            or line.startswith("total")
        ):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        syscall = parts[-1]
        if syscall == "total":
            continue
        if len(parts) >= 6:
            calls_token = parts[-3]
        elif len(parts) == 5:
            calls_token = parts[-2]
        else:
            continue
        if not calls_token.isdigit():
            continue
        syscalls.append({"syscall": syscall, "calls": int(calls_token)})
    return {"mode": "strace", "syscalls": syscalls, "source": str(path)}


def git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return None


def mem_total_kb() -> int:
    try:
        for line in read_text("/proc/meminfo").splitlines():
            if line.startswith("MemTotal:"):
                return read_kb(line.split(":", 1)[1].strip())
    except OSError:
        pass
    return 0


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SERIES_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a command and report CPU, memory, IO, fd, and profiling metrics.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --out /tmp/report.json -- my_command\n"
            "  %(prog)s --ebpf auto --perf stat --per-benchmark -- "
            "build/reldebug/benchmark/benchmark_runner 'benchmark/micro/.*'\n"
        ),
    )
    parser.add_argument(
        "--out", default="metrics.json", help="JSON report path (default: metrics.json)"
    )
    parser.add_argument("--csv", help="optional CSV time-series path")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="sampling interval in seconds",
    )
    parser.add_argument(
        "--smaps", action="store_true", help="sample PSS from smaps_rollup (slower)"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress warnings and the summary"
    )
    parser.add_argument(
        "--files",
        choices=["off", "strace"],
        default="off",
        help="file tracing backend (strace reports syscall counts)",
    )
    parser.add_argument(
        "--perf", choices=["off", "stat", "record"], default="off", help="perf backend"
    )
    parser.add_argument("--perf-events", help="comma-separated perf events")
    parser.add_argument(
        "--perf-freq", type=int, default=99, help="perf record sampling frequency"
    )
    parser.add_argument(
        "--call-graph",
        choices=["dwarf", "fp"],
        default="dwarf",
        help="perf call-graph mode",
    )
    parser.add_argument(
        "--flamegraph", help="write a flamegraph SVG (implies --perf record)"
    )
    parser.add_argument(
        "--per-benchmark",
        action="store_true",
        help="parse benchmark_runner timing output",
    )
    parser.add_argument(
        "--benchmark-csv",
        help="read benchmark timings from this file instead of injecting --out=",
    )
    parser.add_argument(
        "--ebpf", choices=["auto", "on", "off"], default="auto", help="eBPF backend"
    )
    parser.add_argument(
        "--ebpf-script",
        default="opensnoop.bt",
        help="eBPF script to run (default: opensnoop.bt)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to run (use -- before the command)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if not sys.platform.startswith("linux"):
        print("error: duckdb_metrics.py requires Linux /proc", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.flamegraph:
        args.perf = "record"
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")

    warnings = Warnings(args.quiet)
    if args.files == "strace" and shutil.which("strace") is None:
        parser.error("--files strace requires strace on PATH")

    ebpf = detect_ebpf()
    if args.ebpf == "on" and not ebpf["available"]:
        parser.error(
            "--ebpf on requested but eBPF is unavailable: " + ", ".join(ebpf["reasons"])
        )
    if args.ebpf == "auto" and not ebpf["available"]:
        warnings.add(
            "eBPF unavailable: "
            + ", ".join(ebpf["reasons"])
            + "; falling back to /proc (use --files strace for file syscall counts)"
        )
    if args.ebpf == "off":
        ebpf["available"] = False

    tmpdir = Path(tempfile.mkdtemp(prefix="duckdb-metrics-"))
    strace_out: Path | None = None
    benchmark_csv: Path | None = None

    original_command = list(command)
    if args.per_benchmark and is_benchmark_runner(original_command):
        if args.benchmark_csv:
            benchmark_csv = Path(args.benchmark_csv)
        elif not benchmark_output_arg(original_command):
            benchmark_csv = tmpdir / "benchmark-timings.csv"
            original_command.append(f"--out={benchmark_csv}")

    run_command = list(original_command)
    if args.files == "strace":
        strace_out = tmpdir / "strace.txt"
        run_command = build_strace_command(run_command, strace_out)
        if args.perf != "off":
            warnings.add(
                "perf attaches to strace rather than the traced command when --files strace is used"
            )

    start = time.monotonic()
    try:
        process = subprocess.Popen(run_command)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"error: could not start command: {exc}", file=sys.stderr)
        return 126

    # Attach profilers immediately; an unconditional sleep loses very short commands.
    perf = (
        PerfCollector(process.pid, args, warnings, tmpdir)
        if args.perf != "off"
        else None
    )
    if perf is not None:
        perf.start()
    ebpf_process: subprocess.Popen | None = None
    ebpf_out: Path | None = None
    if ebpf["available"]:
        ebpf_process, ebpf_out = start_ebpf(
            process.pid, args.ebpf_script, tmpdir, warnings
        )

    sampler = Sampler(process.pid, args.interval, args.smaps)
    series: list[dict] = []
    interrupted = False
    fast_start_until = time.monotonic() + 0.1
    pidfd = None
    try:
        pidfd = os.pidfd_open(process.pid)
    except (AttributeError, OSError):
        pidfd = None
    try:
        while True:
            if waitid_nohang(process.pid):
                break
            row = sampler.sample()
            if row is not None:
                series.append(row)
            # Sample rapidly right after launch so short commands still record IO.
            sleep_for = (
                min(args.interval, 0.005)
                if time.monotonic() < fast_start_until
                else args.interval
            )
            if pidfd is not None:
                select.select([pidfd], [], [], sleep_for)
            else:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        interrupted = True
        try:
            process.send_signal(signal.SIGINT)
        except OSError:
            pass
    finally:
        if pidfd is not None:
            os.close(pidfd)

    final_row = None
    if interrupted:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        status = process.returncode if process.returncode is not None else 130
        rusage = None
    else:
        final_row = sampler.final()
        status, rusage = reap(process.pid)
    wall_s = time.monotonic() - start
    if perf is not None:
        perf.stop()
    stop_ebpf(ebpf_process, warnings)

    exit_code = os.waitstatus_to_exitcode(status) if status is not None else 0
    signal_number = None
    if exit_code < 0:
        signal_number = -exit_code
        exit_code = 128 + signal_number
    process.returncode = -signal_number if signal_number is not None else exit_code

    if final_row is not None:
        final_row["t_s"] = round(wall_s, 6)
        # Zombies may report zeroed status/IO on some kernels; keep the final
        # snapshot only when it carries information beyond the last live sample.
        if (
            final_row.get("rss_kb", 0) > 0
            or final_row.get("rchar", 0) > 0
            or final_row.get("wchar", 0) > 0
            or final_row.get("cpu_user_s", 0) > 0
        ):
            series.append(final_row)
        else:
            final_row = None
    last_live = series[-1] if series else None
    last = final_row or last_live
    if last is not None and last_live is not None and last is not last_live:
        for field in (
            "cpu_user_s",
            "cpu_sys_s",
            "rss_kb",
            "pss_kb",
            "fds",
            "threads",
            "rchar",
            "wchar",
            "read_bytes",
            "write_bytes",
            "syscr",
            "syscw",
            "minflt",
            "majflt",
            "vol_ctx",
            "nvol_ctx",
            "sched_run_ns",
            "sched_wait_ns",
        ):
            last[field] = max(last.get(field, 0), last_live.get(field, 0))
        last["processes"] = max(last.get("processes", 0), last_live.get("processes", 0))
    rusage_values = rusage_dict(rusage)

    summary: dict = {
        "wall_s": round(wall_s, 6),
        "samples": len(series),
        "processes_observed": len(sampler.seen),
        "peak_processes": sampler.peak_processes,
        "peak_rss_kb": max(sampler.peak_rss_kb, rusage_values.get("max_rss_kb", 0)),
        "peak_rss_max_kb": max(
            sampler.peak_rss_max_kb, rusage_values.get("max_rss_kb", 0)
        ),
        "peak_pss_kb": sampler.peak_pss_kb,
        "peak_fds": sampler.peak_fds,
        "peak_threads": sampler.peak_threads,
    }
    if last:
        summary["cpu_user_s"] = round(last.get("cpu_user_s", 0), 6)
        summary["cpu_sys_s"] = round(last.get("cpu_sys_s", 0), 6)
        summary["rchar"] = int(last.get("rchar", 0))
        summary["wchar"] = int(last.get("wchar", 0))
        summary["read_bytes"] = int(last.get("read_bytes", 0))
        summary["write_bytes"] = int(last.get("write_bytes", 0))
        summary["syscr"] = int(last.get("syscr", 0))
        summary["syscw"] = int(last.get("syscw", 0))
        summary["vmhwm_kb"] = int(last.get("vmhwm_kb", 0)) or rusage_values.get(
            "max_rss_kb", 0
        )
    else:
        summary.update(
            {
                "cpu_user_s": 0,
                "cpu_sys_s": 0,
                "rchar": 0,
                "wchar": 0,
                "read_bytes": 0,
                "write_bytes": 0,
                "syscr": 0,
                "syscw": 0,
                "vmhwm_kb": 0,
            }
        )
    if "user_s" in rusage_values:
        summary["cpu_user_s"] = max(summary["cpu_user_s"], rusage_values["user_s"])
        summary["cpu_sys_s"] = max(summary["cpu_sys_s"], rusage_values["sys_s"])
    cpu_total = summary["cpu_user_s"] + summary["cpu_sys_s"]
    summary["cpu_pct"] = round(100.0 * cpu_total / wall_s, 3) if wall_s > 0 else 0.0
    summary["cpu_pct_of_nproc"] = round(
        summary["cpu_pct"] / max(os.cpu_count() or 1, 1), 3
    )
    summary["rusage"] = rusage_values
    summary["minor_faults"] = max(
        rusage_values.get("minor_faults", 0), int(last.get("minflt", 0)) if last else 0
    )
    summary["major_faults"] = max(
        rusage_values.get("major_faults", 0), int(last.get("majflt", 0)) if last else 0
    )
    summary["voluntary_ctxt_switches"] = max(
        rusage_values.get("voluntary_ctxt_switches", 0),
        int(last.get("vol_ctx", 0)) if last else 0,
    )
    summary["nonvoluntary_ctxt_switches"] = max(
        rusage_values.get("nonvoluntary_ctxt_switches", 0),
        int(last.get("nvol_ctx", 0)) if last else 0,
    )

    report: dict = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": original_command,
        "cwd": os.getcwd(),
        "exit_code": exit_code,
        "signal": signal_number,
        "wall_s": round(wall_s, 6),
        "summary": summary,
        "series": series,
        "environment": {
            "kernel": platform.release(),
            "python": platform.python_version(),
            "nproc": os.cpu_count(),
            "mem_total_kb": mem_total_kb(),
            "git_rev": git_revision(),
            "perf": shutil.which("perf"),
            "perf_hardware_counters": detect_hardware_pmu(),
            "strace": shutil.which("strace"),
            "ebpf": ebpf,
            "smaps": args.smaps,
        },
        "warnings": warnings.items,
    }

    if perf is not None:
        report["perf"] = perf.report()
    if ebpf_out is not None:
        report["ebpf"] = {
            "available": True,
            "output": str(ebpf_out),
            "script": args.ebpf_script,
        }
    elif args.ebpf != "off":
        report["ebpf"] = ebpf

    if strace_out is not None:
        report["files"] = parse_strace_counts(strace_out)

    if benchmark_csv is not None:
        report["benchmarks"] = parse_benchmark_csv(benchmark_csv)

    out_path = Path(args.out)
    if str(out_path) == "-":
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.csv:
        write_csv(Path(args.csv), series)

    if not args.quiet:
        print(
            f"wall={summary['wall_s']}s cpu={summary['cpu_user_s'] + summary['cpu_sys_s']:.3f}s "
            f"peak_rss={summary['peak_rss_kb'] / 1024:.1f}MiB "
            f"rchar={summary['rchar']} read_bytes={summary['read_bytes']}",
            file=sys.stderr,
        )
        if str(out_path) != "-":
            print(f"wrote {out_path}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
