# PGLib-OPF baseline runner

Scripts that compute PGLib-OPF baseline results with [PowerModels.jl](https://github.com/lanl-ansi/PowerModels.jl).
The experiment design follows the published baseline (`../BASELINE.md`, and PowerModels
`docs/src/experiment-results.md`):

- each model is built with PowerModels and solved with Ipopt at `tol = 1e-6`;
- a run is counted as solved when the termination status is `LOCALLY_SOLVED` (or `OPTIMAL`);
- the reported solve time excludes Julia's JIT time (a warm-up solve of `pglib_opf_case5_pjm` runs first in the same process);
- relaxation gaps are `100 * (AC - relaxation) / AC`.

Beyond the published columns (AC = `ACPPowerModel`, DC = `DCPPowerModel`, SOC = `SOCWRPowerModel`,
QC = `QCRMPowerModel`) the runner also builds `ACRPowerModel`, `IVRPowerModel`, `LPACCPowerModel` and the AC power flow, and
can solve the same models with Gurobi, CPLEX or HiGHS.

## Files

| File | What it does |
|---|---|
| `run_one.jl` | One job (case x model x solver) in a fresh Julia process; writes a JSON sidecar with status, objective, times, model size, peak memory and package versions |
| `run_queue.py` | Runs a task list from one flat process pool: resumes from finished sidecars, pins each job to one CPU (`--cpus`), runs the largest AC models one at a time, holds new jobs while the Windows commit charge is near a cap, kills jobs past their time limit |
| `make_tasks.py` | Writes task lists (`sample`, `epigrids`) |
| `summarize.py` | Collects sidecars into one CSV |
| `Project.toml`, `Manifest.toml` | The pinned Julia environment |

## Reproduce

```
julia --project=. -e "import Pkg; Pkg.instantiate()"
julia --project=. run_one.jl ../pglib_opf_case14_ieee.m ACP ipopt out/case14_ACP_ipopt.json 7200 1
```

Gurobi and CPLEX need their own licences (`GUROBI_HOME`, `CPLEX_STUDIO_BINARIES` at build time).
`ipopt-ma27` and `ipopt-ma57` need the HSL_jll package from the HSL licence portal.
