"""summarize.py -- collect run_one.jl sidecars under a results folder into one CSV.

    python summarize.py <results_dir> <out.csv>
"""
import json, sys
from pathlib import Path

import pandas as pd

rows = []
for p in sorted(Path(sys.argv[1]).rglob("*.json")):
    if p.name.endswith(".mem.json"):
        continue
    d = json.loads(p.read_text())
    mem = p.with_name(p.name + ".mem.json")
    m = json.loads(mem.read_text()) if mem.exists() else {}
    net = d.get("network") or {}
    rows.append({
        "set": p.parts[-3], "case": p.parts[-2], "model": d.get("model"), "solver": d.get("solver"),
        "status": d.get("termination_status"), "objective": d.get("objective"),
        "solve_time": d.get("solve_time"), "build_time": d.get("build_time"), "wall_solve": d.get("wall_solve"),
        "iters": d.get("barrier_iterations") or d.get("simplex_iterations") or d.get("iterations"),
        "vars": d.get("variables"), "cons": d.get("constraints"), "buses": net.get("buses"),
        "peak_gb": m.get("peak_private_gb"), "maxrss_gb": (d.get("max_rss_bytes") or 0) / 2**30,
        "job_wall": m.get("wall_s"), "error": (d.get("error") or "")[:200],
    })
df = pd.DataFrame(rows)
df.to_csv(sys.argv[2], index=False)
with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 500):
    print(df.drop(columns=["error"]).to_string(index=False, float_format=lambda x: f"{x:.6g}"))
    errs = df[df.error != ""]
    if len(errs):
        print("\nerrors:")
        print(errs[["case", "model", "solver", "error"]].to_string(index=False))
