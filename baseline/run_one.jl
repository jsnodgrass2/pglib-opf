# run_one.jl -- one benchmark job (one case x one model x one solver) in a fresh Julia process.
#
#   julia --project=. run_one.jl <case.m> <model> <solver> <out.json> [time_limit_s] [threads]
#
# model : ACP ACR IVR DCP LPAC SOC QC PF_NR PF_IPOPT
# solver: ipopt ipopt-ma27 ipopt-ma57 gurobi cplex highs native
#
# The experiment design follows the PGLib-OPF baseline (pglib-opf BASELINE.md and
# PowerModels docs/src/experiment-results.md): Ipopt with tol = 1e-6, the solve time
# excludes Julia's JIT (a warm-up solve of pglib_opf_case5_pjm runs first), and a run
# counts as solved when the termination status is LOCALLY_SOLVED (or OPTIMAL).
# The JSON sidecar records everything needed to rebuild a table row.

import Pkg, SHA, Dates
using JSON
using JuMP
using PowerModels
const MOI = JuMP.MOI

PowerModels.silence()

const MODELS = Dict(
    "ACP"      => (ACPPowerModel,   build_opf),
    "ACR"      => (ACRPowerModel,   build_opf),
    "IVR"      => (IVRPowerModel,   build_opf_iv),
    "DCP"      => (DCPPowerModel,   build_opf),
    "LPAC"     => (LPACCPowerModel, build_opf),
    "SOC"      => (SOCWRPowerModel, build_opf),
    "QC"       => (QCRMPowerModel,  build_opf),
    "PF_IPOPT" => (ACPPowerModel,   build_pf),
)
const NONCONVEX = Set(["ACP", "ACR", "IVR", "PF_IPOPT"])
const WARMUP_CASE = joinpath(@__DIR__, "..", "pglib_opf_case5_pjm.m")

case_path, model, solver, out_path = ARGS[1], ARGS[2], ARGS[3], ARGS[4]
time_limit = length(ARGS) >= 5 ? parse(Float64, ARGS[5]) : 7200.0
threads    = length(ARGS) >= 6 ? parse(Int, ARGS[6]) : 1

solver in ("gurobi",)          && @eval using Gurobi
solver in ("cplex",)           && @eval using CPLEX
solver in ("highs",)           && @eval using HiGHS
startswith(solver, "ipopt")    && @eval using Ipopt
solver in ("ipopt-ma27", "ipopt-ma57") && @eval using HSL_jll

function make_optimizer(solver, model, time_limit, threads)
    if startswith(solver, "ipopt")
        attrs = Any["tol" => 1e-6, "max_wall_time" => time_limit, "print_level" => 5]
        if solver != "ipopt"
            push!(attrs, "hsllib" => HSL_jll.libhsl_path, "linear_solver" => split(solver, "-")[2])
        end
        return optimizer_with_attributes(Ipopt.Optimizer, attrs...)
    elseif solver == "gurobi"
        env = Gurobi.Env()
        attrs = Any["Threads" => threads, "TimeLimit" => time_limit]
        # Gurobi 13 NL barrier: local optimum of a nonconvex continuous model (preview feature).
        # Its iteration limit (default 1000) is set to Ipopt's default max_iter, 3000.
        model in NONCONVEX && push!(attrs, "OptimalityTarget" => 1, "NLBarIterLimit" => 3000)
        return optimizer_with_attributes(() -> Gurobi.Optimizer(env), attrs...)
    elseif solver == "cplex"
        return optimizer_with_attributes(CPLEX.Optimizer, "CPXPARAM_Threads" => threads,
                                         "CPXPARAM_TimeLimit" => time_limit)
    elseif solver == "highs"
        return optimizer_with_attributes(HiGHS.Optimizer, "threads" => threads,
                                         "time_limit" => time_limit)
    end
    error("unknown solver $solver")
end

trysafe(f) = try f() catch; nothing end
# JSON has no Inf or NaN (Ipopt reports objective_lb = -Inf): store those as strings.
jsonsafe(x::AbstractFloat) = isfinite(x) ? x : string(x)
jsonsafe(x::AbstractDict) = Dict(k => jsonsafe(v) for (k, v) in x)
jsonsafe(x) = x

function network_summary(data)
    act(t, k) = count(c -> get(c, k, 1) != 0, values(get(data, t, Dict())))
    Dict(
        "buses"    => count(b -> b["bus_type"] != 4, values(data["bus"])),
        "buses_all"=> length(data["bus"]),
        "gens"     => act("gen", "gen_status"),
        "branches" => act("branch", "br_status"),
        "dclines"  => act("dcline", "br_status"),
        "load_mw"  => sum((l["pd"] for l in values(data["load"]) if l["status"] != 0); init=0.0) * data["baseMVA"],
    )
end

function solve_one(data, model, solver, time_limit, threads)
    rec = Dict{String,Any}()
    if model == "PF_NR"
        t = @elapsed res = compute_ac_pf(data)
        rec["termination_status"] = string(res["termination_status"])
        rec["solve_time"] = res["solve_time"]
        rec["wall_solve"] = t
        rec["iterations"] = get(res, "iterations", nothing)
        return rec
    end
    ptype, build = MODELS[model]
    t_build = @elapsed pm = instantiate_model(data, ptype, build)
    opt = make_optimizer(solver, model, time_limit, threads)
    t_solve = @elapsed res = optimize_model!(pm, optimizer=opt)
    m = pm.model
    rec["build_time"] = t_build
    rec["wall_solve"] = t_solve
    rec["solve_time"] = res["solve_time"]
    rec["termination_status"] = string(res["termination_status"])
    rec["primal_status"] = string(res["primal_status"])
    rec["dual_status"] = string(res["dual_status"])
    rec["objective"] = res["objective"]
    rec["objective_lb"] = get(res, "objective_lb", nothing)
    rec["variables"] = num_variables(m)
    rec["constraints"] = num_constraints(m; count_variable_in_set_constraints=false)
    rec["barrier_iterations"] = trysafe(() -> MOI.get(m, MOI.BarrierIterations()))
    rec["simplex_iterations"] = trysafe(() -> MOI.get(m, MOI.SimplexIterations()))
    rec["solver_name"] = trysafe(() -> solver_name(m))
    rec["solver_version"] = trysafe(() -> string(MOI.get(backend(m), MOI.SolverVersion())))
    rec["raw_status"] = trysafe(() -> MOI.get(m, MOI.RawStatusString()))
    return rec
end

function package_versions()
    want = Set(["PowerModels", "InfrastructureModels", "JuMP", "MathOptInterface", "Ipopt",
                "Ipopt_jll", "HSL_jll", "Gurobi", "CPLEX", "HiGHS", "HiGHS_jll"])
    Dict(p.name => string(p.version) for p in values(Pkg.dependencies()) if p.name in want && p.version !== nothing)
end

out = Dict{String,Any}(
    "case_path" => abspath(case_path),
    "case_sha256" => bytes2hex(open(SHA.sha256, case_path)),
    "model" => model, "solver" => solver,
    "time_limit" => time_limit, "threads" => threads,
    "host" => gethostname(), "cpu" => Sys.cpu_info()[1].model, "julia" => string(VERSION),
    "started" => string(Dates.now()), "versions" => package_versions(),
)
try
    # JIT warm-up on the 5-bus case, so the timed solve below excludes compilation.
    warm = PowerModels.parse_file(WARMUP_CASE)
    out["warmup_seconds"] = @elapsed solve_one(warm, model, solver, 60.0, threads)

    out["parse_time"] = @elapsed data = PowerModels.parse_file(case_path)
    out["network"] = network_summary(data)
    merge!(out, solve_one(data, model, solver, time_limit, threads))
catch err
    out["error"] = sprint(showerror, err)
    out["termination_status"] = "ERROR"
end
out["max_rss_bytes"] = Sys.maxrss()
out["finished"] = string(Dates.now())

mkpath(dirname(abspath(out_path)))
tmp = out_path * ".tmp"
open(io -> JSON.print(io, jsonsafe(out), 2), tmp, "w")
mv(tmp, out_path; force=true)
println("RESULT ", model, " ", solver, " ", out["termination_status"], " ", get(out, "objective", ""), " ", get(out, "solve_time", ""))
