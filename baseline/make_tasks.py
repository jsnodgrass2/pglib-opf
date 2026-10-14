r"""make_tasks.py -- write a task list for run_queue.py.

    python make_tasks.py <plan> <cases_dir> <results_dir> <tasks.json>

plan: "sample" (timed sample) or "epigrids" (the EPIGRIDS cases benchmark matrix).
cases_dir holds release\, release\new_england\, pglib_v23.07\typ\ and sensitivity\ copies of
the .m files; results_dir receives one JSON sidecar and one log per job.
"""
import json, sys
from pathlib import Path

# name -> (path relative to cases_dir, buses)
RELEASE = {
    "eastern":   ("release/Base_Eastern_Interconnect_515GW.m", 78484),
    "western":   ("release/Base_West_Interconnect_121GW.m", 20758),
    "miohin":    ("release/Base_MIOHIN_76GW.m", 10192),
    "texas":     ("release/Base_Texas_66GW.m", 7336),
    "florida":   ("release/Base_Florida_42GW.m", 5658),
    "wisconsin": ("release/Wisconsin_1664.m", 1664),
    "ne_base":   ("release/new_england/BaseCase.m", 250),
}
NE_POINTS = ["Easy/Fall116Generous", "Easy/Winter12Generous", "Easy/Winter68Generous",
             "Medium/Fall106Tight", "Medium/Fall116Tight", "Medium/Spring4Generous",
             "Medium/Summer69Tight", "Medium/Summer90Tight", "Medium/Winter68Tight",
             "Hard/Spring13Tight", "Hard/Summer90Generous", "Hard/Summer93Generous"]
for p in NE_POINTS:
    RELEASE["ne_" + p.split("/")[1].lower()] = (f"release/new_england/{p}.m", 250)
PGLIB = {f"pglib{n}": (f"pglib_v23.07/typ/pglib_opf_case{n}_epigrids.m", n)
         for n in (78484, 20758, 10192, 7336, 5658)}

IPOPT_MODELS = ["ACP", "ACR", "IVR", "DCP", "LPAC", "SOC", "QC"]
GUROBI_MODELS = ["DCP", "LPAC", "SOC", "QC", "ACR", "IVR", "ACP"]
CPLEX_MODELS = ["DCP", "LPAC", "SOC", "QC"]
AC_FAMILY = {"ACP", "ACR", "IVR", "QC", "PF_IPOPT"}


def est_gb(model, buses):
    """Rough commit estimate per job (GB); replaced by measured peaks after the sample."""
    per_bus = 2.5e-4 if model in AC_FAMILY else 1.0e-4
    return round(1.0 + per_bus * buses, 1)


def task(cases_dir, results_dir, setname, name, rel, buses, model, solver):
    big = buses >= 70000
    out = Path(results_dir) / setname / name / f"{model}__{solver}.json"
    return {
        "id": f"{setname}/{name}/{model}/{solver}",
        "case": str(Path(cases_dir) / rel), "model": model, "solver": solver,
        "out": str(out), "log": str(out.with_suffix(".log")),
        "time_limit": 28800 if big else 7200, "threads": 1,
        "heavy": big and model in AC_FAMILY, "est_gb": est_gb(model, buses),
    }


def combos(full):
    c = [(m, "ipopt") for m in IPOPT_MODELS]
    if full:
        c += [(m, "gurobi") for m in GUROBI_MODELS] + [(m, "cplex") for m in CPLEX_MODELS]
        c += [("DCP", "highs"), ("PF_NR", "native"), ("PF_IPOPT", "ipopt")]
    return c


def main():
    plan, cases_dir, results_dir, out = sys.argv[1:5]
    T = []
    if plan == "sample":
        w = RELEASE["wisconsin"]
        T += [task(cases_dir, results_dir, "sample", "wisconsin", *w, m, s) for m, s in combos(True)]
        tx = RELEASE["texas"]
        T += [task(cases_dir, results_dir, "sample", "texas", *tx, m, s) for m, s in combos(False)]
        T += [task(cases_dir, results_dir, "sample", "texas", *tx, m, "gurobi") for m in ("DCP", "SOC", "QC", "ACR")]
        e = RELEASE["eastern"]
        T += [task(cases_dir, results_dir, "sample", "eastern", *e, m, s)
              for m, s in (("ACP", "ipopt"), ("DCP", "ipopt"), ("DCP", "gurobi"))]
    elif plan == "epigrids":
        for name, (rel, buses) in RELEASE.items():
            T += [task(cases_dir, results_dir, "release", name, rel, buses, m, s) for m, s in combos(True)]
        for name, (rel, buses) in PGLIB.items():
            T += [task(cases_dir, results_dir, "pglib", name, rel, buses, m, "ipopt") for m in IPOPT_MODELS]
        sens = Path(cases_dir) / "sensitivity"
        # Florida, Wisconsin and New England have no unit whose limits differ from the .pwb.
        for name in ("eastern", "western", "miohin", "texas"):
            rel, buses = RELEASE[name]
            srel = "sensitivity/" + Path(rel).name
            if (Path(cases_dir) / srel).exists():
                T += [task(cases_dir, results_dir, "sensitivity", name, srel, buses, m, "ipopt")
                      for m in IPOPT_MODELS + ["PF_IPOPT"]]
    else:
        sys.exit(f"unknown plan {plan}")
    Path(out).write_text(json.dumps(T, indent=1))
    print(f"{len(T)} tasks -> {out}")


if __name__ == "__main__":
    main()
