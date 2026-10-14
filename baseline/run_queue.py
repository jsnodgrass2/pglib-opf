"""run_queue.py -- run a list of benchmark jobs (run_one.jl) from one flat process pool.

    python run_queue.py <tasks.json> --workers N [--status STATUS.md] [--commit-cap-gb 70]

tasks.json is a list of {"id", "case", "model", "solver", "out", "log", "time_limit",
"threads", "heavy", "est_gb"}. A job whose "out" JSON already exists is skipped, so a
re-run resumes. Heavy jobs (the largest AC-family models) run one at a time. No job starts
while the Windows commit charge plus the job's estimate would pass the cap. A job still
running at 1.1 x its time limit + 5 minutes is killed and recorded as KILLED.
Each job's peak private bytes (its commit charge) are sampled and written to <out>.mem.json.
"""
import argparse, ctypes, datetime as dt, json, os, subprocess, sys, time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong if n != "dwLength" and n != "dwMemoryLoad" else ctypes.c_ulong)
                for n in ("dwLength", "dwMemoryLoad", "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                          "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


def commit_gb():
    """(committed, limit) in GB, from GlobalMemoryStatusEx (page-file fields = commit)."""
    s = MEMORYSTATUSEX()
    s.dwLength = ctypes.sizeof(s)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return (s.ullTotalPageFile - s.ullAvailPageFile) / 2**30, s.ullTotalPageFile / 2**30


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def tree_private_bytes(p):
    total = 0
    try:
        for q in [p] + p.children(recursive=True):
            try:
                total += q.memory_info().private
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    return total


def kill_tree(p):
    try:
        for q in p.children(recursive=True):
            q.kill()
        p.kill()
    except psutil.Error:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--commit-cap-gb", type=float, default=70.0)
    ap.add_argument("--status", default=None, help="append a STATUS line here every --status-every minutes")
    ap.add_argument("--status-every", type=float, default=30.0)
    ap.add_argument("--julia", default="julia")
    ap.add_argument("--cpus", default=None,
                    help="comma list of logical CPUs; each job is pinned to one free CPU and --workers "
                         "becomes the list length (use the performance cores on a hybrid CPU)")
    ap.add_argument("--share-dir", default=None,
                    help="folder where queues running at the same time publish their memory reservations, "
                         "so each counts the others' jobs that are still growing (default: next to the task list)")
    a = ap.parse_args()
    share = Path(a.share_dir or Path(a.tasks).parent / "queue_share")
    share.mkdir(parents=True, exist_ok=True)
    mine = share / (Path(a.tasks).stem + ".json")
    cpus = [int(c) for c in a.cpus.split(",")] if a.cpus else None
    if cpus:
        a.workers = len(cpus)

    tasks = json.loads(Path(a.tasks).read_text())

    def finished(t):  # a sidecar from a crash is a placeholder, so that job runs again
        p = Path(t["out"])
        return p.exists() and json.loads(p.read_text()).get("termination_status") != "CRASHED"

    todo = [t for t in tasks if not finished(t)]
    # Priority 0 before 1. Within it: the heavy chain first (one at a time), then the smaller cases
    # largest first, then the other jobs on the largest case. Those run beside the long heavy chain
    # instead of filling the memory budget at the start and leaving cores idle.
    todo.sort(key=lambda t: (t.get("priority", 0), not t.get("heavy", False), t.get("big", False),
                             -t.get("est_gb", 0.0)))
    total, skipped = len(tasks), len(tasks) - len(todo)
    print(f"{now()} {total} tasks, {skipped} already done, {len(todo)} to run, workers={a.workers}", flush=True)

    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", GUROBI_JL_USE_GUROBI_JLL="false")
    env.setdefault("GUROBI_HOME", r"C:\gurobi1302\win64")
    running = {}  # id -> dict(proc, ps, task, start, peak)
    done = skipped
    last_status = 0.0
    t0 = time.time()

    def status_line(tag=""):
        used, limit = commit_gb()
        line = (f"{now()} | queue {Path(a.tasks).stem} | done {done}/{total}, running {len(running)} "
                f"({', '.join(sorted(running))[:300]}) | commit {used:.1f}/{limit:.1f} GB | "
                f"CPU {psutil.cpu_percent(interval=1):.0f}% | elapsed {(time.time()-t0)/3600:.2f} h {tag}")
        print(line, flush=True)
        if a.status:
            with open(a.status, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    while todo or running:
        # Reap finished or overdue jobs and sample memory.
        for jid in list(running):
            r = running[jid]
            r["peak"] = max(r["peak"], tree_private_bytes(r["ps"]))
            rc = r["proc"].poll()
            overdue = time.time() - r["start"] > 1.1 * r["task"]["time_limit"] + 300
            if rc is None and overdue:
                kill_tree(r["ps"])
                r["proc"].wait()
                rc = "KILLED"
            if rc is not None:
                t = r["task"]
                mem = {"id": jid, "peak_private_gb": r["peak"] / 2**30, "wall_s": time.time() - r["start"],
                       "returncode": rc, "cpu": r["cpu"]}
                Path(t["out"]).parent.mkdir(parents=True, exist_ok=True)
                Path(t["out"] + ".mem.json").write_text(json.dumps(mem, indent=1))
                if not Path(t["out"]).exists():  # killed or crashed before writing its sidecar
                    Path(t["out"]).write_text(json.dumps({**t, "termination_status": str(rc) if rc == "KILLED" else "CRASHED",
                                                          "returncode": rc, "wall_s": mem["wall_s"]}, indent=1))
                done += 1
                print(f"{now()} done {jid} rc={rc} peak={mem['peak_private_gb']:.1f} GB wall={mem['wall_s']:.0f}s", flush=True)
                del running[jid]

        # Start jobs while there are free workers and commit headroom.
        heavy_running = any(r["task"].get("heavy") for r in running.values())
        used, _ = commit_gb()
        # The cap can be changed while queues run: write a number (GB) to <share>/cap_gb.txt.
        cap = a.commit_cap_gb
        try:
            cap = float((share / "cap_gb.txt").read_text())
        except (OSError, ValueError):
            pass
        # Running jobs still growing toward their estimated peak keep that headroom reserved,
        # this queue's and (through the share folder) every other queue's.
        reserved = sum(max(0.0, r["task"].get("est_gb", 1.0) - tree_private_bytes(r["ps"]) / 2**30)
                       for r in running.values())
        mine.write_text(json.dumps({"reserved_gb": reserved, "time": time.time()}))
        for f in share.glob("*.json"):
            if f != mine:
                try:
                    o = json.loads(f.read_text())
                    if time.time() - o["time"] < 120:
                        used += o["reserved_gb"]
                except (OSError, ValueError, KeyError):
                    pass
        used += reserved
        for t in list(todo):
            if len(running) >= a.workers:
                break
            if t.get("heavy") and heavy_running:
                continue
            if used + t.get("est_gb", 1.0) > cap:
                continue
            Path(t["log"]).parent.mkdir(parents=True, exist_ok=True)
            logf = open(t["log"], "w", encoding="utf-8")
            cmd = [a.julia, f"--project={HERE}", str(HERE / "run_one.jl"), t["case"], t["model"], t["solver"],
                   t["out"], str(t["time_limit"]), str(t.get("threads", 1))]
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env, cwd=str(HERE))
            ps = psutil.Process(proc.pid)
            cpu = None
            if cpus:
                cpu = next(c for c in cpus if c not in {r["cpu"] for r in running.values()})
                ps.cpu_affinity([cpu])  # children (none expected) inherit it
            running[t["id"]] = {"proc": proc, "ps": ps, "task": t, "start": time.time(), "peak": 0, "cpu": cpu}
            todo.remove(t)
            used += t.get("est_gb", 1.0)
            reserved += t.get("est_gb", 1.0)
            heavy_running = heavy_running or t.get("heavy", False)
            print(f"{now()} start {t['id']}", flush=True)
        mine.write_text(json.dumps({"reserved_gb": reserved, "time": time.time()}))

        if a.status and time.time() - last_status > a.status_every * 60:
            status_line()
            last_status = time.time()
        time.sleep(5)

    mine.write_text(json.dumps({"reserved_gb": 0.0, "time": time.time()}))
    status_line("| queue finished")


if __name__ == "__main__":
    sys.exit(main())
