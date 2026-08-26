#!/usr/bin/env python3
"""lmloop -- a local-model iteration loop.

Give it an objective; it works in a git worktree for hours and commits what it
did.  It drives `pi` and nothing else: tools, skills, models, and prompts belong
to the agent, and the loop's only jobs are isolation, iteration, and never
throwing work away.

    lmloop run "refactor the dashboard stat grid"
    lmloop run "..." --model llama-swap/local-wide --max-iterations 5
    lmloop models
    lmloop init
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
import display
import eta
import gitops
import harness
import models as models_module
import runrecord
from loop import Run
from rundir import RunDir, make_run_id


STATE_DIR = Path.home() / ".local" / "state" / "lmloop"

# See `runrecord.STALE_AFTER_SECONDS`; kept as an attribute here too since it
# is part of this module's own public surface.
STALE_AFTER_SECONDS = runrecord.STALE_AFTER_SECONDS


def _status_age(state: dict) -> float | None:
    """Seconds since the run last wrote its status, or None if unreadable."""
    return runrecord.age_seconds(state.get("updated_at"))


def _relative(path: Path, root: Path) -> str:
    """A path as short as it can be without becoming ambiguous."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _detach(objective: str, args: argparse.Namespace) -> int:
    """Start the run in its own session and return immediately.

    This exists so something else can kick off a run and get its identity back
    at once -- a pi slash command, a Paseo script, an ssh one-liner from a
    phone.  So the parent has to name the run, not guess at it: the id is no
    longer a pure function of the objective and the date, because a second
    attempt at the same objective today takes the next free lane.  Guessing
    printed an id that belonged to the *previous* run -- so `lmloop resume` on
    it would have resumed the wrong one -- and named the log after it too.

    The parent therefore resolves the id the same way the child would, and
    passes it down, so both agree.

    The log lands outside the worktree on purpose: it has to survive whatever
    happens to the run, including the run never getting far enough to make a
    run directory.
    """
    repo = gitops.repo_root(Path.cwd())
    run_id = Run(repo, config_module.load(repo), objective).run_id
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / f"{run_id}.log"

    argv = [sys.executable, str(Path(__file__).resolve()), "run", objective,
            "--run-id", run_id]
    for flag, value in (("--agent", args.agent), ("--model", args.model),
                        ("--tools", args.tools), ("--gate", args.gate),
                        ("--thinking", args.thinking)):
        if value is not None:
            argv += [flag, value]
    if args.max_iterations:
        argv += ["--max-iterations", str(args.max_iterations)]

    with log_path.open("wb") as log:
        subprocess.Popen(
            argv,
            cwd=str(Path.cwd()),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"started {run_id}")
    print(f"  log:      tail -f {log_path}")
    print(f"  progress: lmloop list")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    repo = gitops.repo_root(cwd)
    config = config_module.load(repo)
    if args.model:
        config["agent"]["model"] = args.model
    if args.thinking:
        config["agent"]["thinking"] = args.thinking
    if args.gate is not None:
        config["gate"]["command"] = args.gate
    # Agent and allowlist together, and after everything else: which tool names
    # are valid is a question only the selected agent can answer.
    config_module.override_agent(config, args.agent or "", args.tools or "")

    objective = args.objective
    if objective == "-":
        objective = sys.stdin.read()
    objective = objective.strip()
    if not objective:
        raise SystemExit("lmloop: empty objective")

    if args.detach:
        return _detach(objective, args)

    run = Run(repo, config, objective, max_iterations=args.max_iterations,
              run_id=getattr(args, "run_id", None))
    # Labels are short and paths are repo-relative because this header is read on
    # a phone as often as on a desktop, and the worktree path alone ran to 96
    # characters -- three wrapped lines before the run has even started.
    display.out(f"lmloop {run.run_id}")
    if run.collided_with:
        # Said before the run starts, not after it fails: this objective already
        # has a run from today, and an operator who meant to carry that one on
        # has one line in which to notice and one command to do it with.
        display.out(f"  note    {run.collided_with} exists; this is a separate attempt")
        display.out(f"          lmloop resume {run.collided_with}  to continue that one instead")
    display.out(f"  model   {run.model}")
    # The repo's basename, not its path: this is the longest and least useful
    # item on a phone, and the absolute path is already in the run:start event
    # and in the summary printed when the run ends.
    display.out(f"  repo    {repo.name}")
    display.out(f"  tree    {_relative(run.worktree, repo)}")
    display.out(f"  branch  {run.branch}")
    display.out(f"  stop    {run.max_iterations} iterations,"
                f" {config['stop']['max_wall_hours']}h,"
                f" {config['stop']['no_diff_iterations']} no-diff")
    if config["gate"]["command"]:
        blocking = "blocks commits" if config["gate"]["blocks_commit"] else "recorded"
        display.out(f"  gate    {config['gate']['command']} ({blocking})")
    display.out()

    if args.dry_run:
        display.out("  --dry-run: nothing created")
        return 0

    run.prepare()
    if run.linked:
        display.out(f"  linked  {', '.join(run.linked)}")
        display.out()
    return run.start()


def _discover_runs(repo: Path, config: dict) -> list[tuple[str, Path]]:
    """Every run directory under this repo's configured worktree root.

    See `runrecord.worktree_root`/`.discover_runs`: the WebUI used to resolve
    `[worktree] root` by reading `.lmloop.toml` on its own, which missed an
    override set only in global config -- something that could never happen
    here, since `config` has already been through `config.load`'s full
    defaults-then-global-then-project layering by the time it arrives.
    """
    root = runrecord.worktree_root(repo, config)
    return [(run_dir.name, run_dir) for run_dir in runrecord.discover_runs(root)]


def _read_run_state(run_dir: Path) -> dict:
    """A run's persisted state, for the resume path that reads it before the
    `RunDir` exists.  A run from before this was written simply says nothing."""
    try:
        value = json.loads((run_dir / "run-state.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        pass
    # A crash in the first iteration predates run-state.json.  run:start is
    # written before the agent starts and is therefore the durable fallback.
    try:
        starts = [json.loads(line) for line in (run_dir / "lmloop.log").read_text().splitlines()]
        start = next(item for item in reversed(starts) if item.get("event") == "run:start")
        return {"harness": start.get("agent", ""), "tools": start.get("tools", "")}
    except (OSError, ValueError, StopIteration):
        return {}


def cmd_list(args: argparse.Namespace) -> int:
    repo = gitops.repo_root(Path.cwd())
    runs = _discover_runs(repo, config_module.load(repo))
    if not runs:
        print("no runs for this repo")
        return 0
    for run_id, run_dir in runs:
        worktree = run_dir.parents[2]
        base = (run_dir / "base-commit").read_text().strip()
        commits = gitops.commit_count(worktree, base) if worktree.is_dir() else 0
        done = len(list(run_dir.glob("iteration-*-prompt.md")))
        stopped = "STOP" if (run_dir / "STOP").exists() else ""
        print(f"{run_id}  {done} iterations, {commits} commits  {stopped}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """What a run is doing right now, from its status.json.

    Deliberately reads the one small file rather than the event log: this has to
    stay cheap enough to poll from a phone over ssh, and useful to anything that
    speaks JSON but not lmloop.
    """
    import json

    repo = gitops.repo_root(Path.cwd())
    runs = _discover_runs(repo, config_module.load(repo))
    if not runs:
        print("no runs for this repo")
        return 1
    run_id = args.run_id or runs[-1][0]
    match = [path for name, path in runs if name == run_id]
    if not match:
        raise SystemExit(f"lmloop: no run {run_id} under this repo")
    run_dir = match[0]

    try:
        state = json.loads((run_dir / "status.json").read_text())
    except (OSError, ValueError):
        print(f"{run_id}: no live status (run has not started, or predates status.json)")
        return 1

    # A process launched by an older lmloop cannot add newly introduced status
    # fields mid-run.  Rebuild ETA from the durable log so status gains the
    # feature immediately without restarting that process.
    live = state.get("phase") in ("loading", "working")
    if live and not state.get("eta_seconds"):
        rd = RunDir(run_dir.parents[2], run_id)
        done, total = rd.plan_progress()
        state.update(eta.estimate(
            rd.read_events(), elapsed_seconds=state.get("elapsed_seconds") or 0,
            iteration=state.get("iteration") or 0,
            max_iterations=state.get("max_iterations") or 0,
            plan_done=done, plan_total=total,
        ))

    if args.json:
        print(json.dumps(state, indent=2))
        return 0

    worktree = run_dir.parents[2]
    commits = gitops.commit_count(worktree, (run_dir / "base-commit").read_text().strip())
    flags = " ".join(f for f, on in (("PAUSED", state.get("paused")), ("STOPPING", state.get("stopping"))) if on)

    # Every line below fits 32 columns unwrapped except the run id, which is one
    # unbreakable token.  This is the view someone polls from a phone, and it is
    # worth more terse than pretty: ASCII only, no em dashes to gamble on the
    # terminal's font, and the alarm on a line of its own so wrapping can never
    # bury it.
    age = _status_age(state)
    stale = age is not None and age > STALE_AFTER_SECONDS

    display.out(f"{run_id}")
    display.out(f"  iter {state.get('iteration')}/{state.get('max_iterations')}  {state.get('phase')}")
    if stale:
        # A dead run and a working run are identical in this file otherwise:
        # status.json is the last thing a crashed run wrote, and it says
        # "working".  Nobody checking from a phone can fall back to `ps`.
        display.out(f"  STALE: no update for {display.elapsed(age)}")
    elif age is None:
        display.out("  STALE?: cannot read the update time")
    display.out(f"  {state.get('last_tool') or 'thinking'}, {state.get('elapsed_seconds', 0) // 60}m in")

    if state.get("plan_total"):
        display.out(f"  plan {state['plan_done']}/{state['plan_total']} steps done")
    if live and state.get("eta_seconds") and not stale:
        display.out(f"  ETA about {display.elapsed(state['eta_seconds'])}")
    counts = f"  {state.get('tool_calls')} tools, {state.get('writes')} writes"
    if state.get("compactions"):
        counts += f", {state['compactions']} overflows"
    display.out(counts)
    display.out(f"  {state.get('output_tokens')} out, {commits} commits  {flags}".rstrip())
    if not stale:
        display.out(f"  updated {display.elapsed(age)} ago" if age is not None
                    else f"  updated {state.get('updated_at')}")
    return 1 if stale else 0


def cmd_resume(args: argparse.Namespace) -> int:
    repo = gitops.repo_root(Path.cwd())
    config = config_module.load(repo)
    if args.model:
        config["agent"]["model"] = args.model
    if args.thinking:
        config["agent"]["thinking"] = args.thinking

    runs = _discover_runs(repo, config)
    if not runs:
        raise SystemExit("lmloop: no runs to resume for this repo")
    run_id = args.run_id or runs[-1][0]
    directories = {name: path for name, path in runs}
    if run_id not in directories:
        raise SystemExit(f"lmloop: no run {run_id} under this repo")

    # Which agent this run has been using, before the config file gets a say.
    # A run started with `--agent omp` against a repo whose .lmloop.toml says
    # pi would otherwise be continued by pi -- same worktree, same session
    # directory, same handoff chain, different agent -- and the run's own log
    # would name both.  `--agent` on the resume still wins, because that is
    # someone deciding rather than a file being stale.
    if not args.agent:
        state = _read_run_state(directories[run_id])
        for key in ("harness", "tools"):
            if isinstance(state.get(key), str) and state[key]:
                config["agent"][key] = state[key]
    config_module.override_agent(config, args.agent or "")

    run = Run(repo, config, objective="", max_iterations=None, run_id=run_id)
    # A leftover STOP -- or the STOP-NOW that accompanies a hard stop -- would
    # stop the resumed run before its first iteration.
    run.rundir.stop_path.unlink(missing_ok=True)
    run.rundir.stop_now_path.unlink(missing_ok=True)
    done = run.attach(args.iterations)
    display.out(f"lmloop {run_id}")
    display.out(f"  resuming after {done} iterations")
    display.out(f"  model   {run.model}")
    display.out(f"  tree    {_relative(run.worktree, repo)}")
    if run.linked:
        display.out(f"  linked  {', '.join(run.linked)}")
    display.out()
    return run.start(from_iteration=done)


def cmd_models(args: argparse.Namespace) -> int:
    url = config_module.DEFAULTS["models"]["llama_swap_url"]
    try:
        repo = gitops.repo_root(Path.cwd())
        url = config_module.load(repo)["models"]["llama_swap_url"]
    except SystemExit:
        pass

    if args.detect:
        measured = models_module.real_context(url)
        if not measured:
            print("no model is loaded; nothing to measure")
            print("load one by using it, then re-run -- probing an unloaded model")
            print("would evict whatever is running.")
            return 1
        models_module.save_cache(measured)
        for name, real in measured.items():
            # Asked for rather than recomputed.  This printed the default split
            # while `declared_window` applied the per-model override, so the one
            # command whose whole job is to report the budget reported a
            # different one from the budget the run would use.
            context, output = models_module.declared_window(
                f"{models_module.local_provider()}/{name}") or (0, 0)
            print(f"{name}: real {real}, declaring {context} + {output} output")
        print(f"\nwritten to {models_module.CONTEXT_CACHE}")
        return 0

    try:
        for entry in models_module.running(url):
            print(f"loaded now: {entry.get('model')} ({entry.get('state')})")
    except Exception as error:  # noqa: BLE001 - a down GPU box is normal
        print(f"llama-swap unreachable at {url}: {error}")

    cache = models_module.load_cache()
    print(f"\nmeasured context ({models_module.CONTEXT_CACHE}):")
    for name, real in sorted(cache.items()) or []:
        print(f"  {name}: {real}")
    if not cache:
        print("  (none -- run `lmloop models --detect` while a model is loaded)")

    # Whichever agent is actually configured, not always pi: an omp or
    # opencode setup was being shown pi's catalogue, or pi's "command not
    # found" on a machine that never had it.
    agent_name = "pi"
    try:
        agent_name = config_module.load(gitops.repo_root(Path.cwd()))["agent"]["harness"]
    except SystemExit:
        pass
    try:
        adapter = harness.get(agent_name)
    except SystemExit as error:
        print(f"\n{error}")
        return 0
    argv = adapter.list_models_argv()
    if not argv:
        print(f"\n{agent_name} cannot list its models")
        return 0
    print(f"\navailable to {agent_name}:")
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
    except OSError as error:
        print(f"  could not run {argv[0]}: {error}")
        return 0
    print(result.stdout.strip() or result.stderr.strip())
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Compress finished runs' event streams.  Deletes nothing."""
    import prune as prune_module

    roots = (
        [Path(item).expanduser() for item in args.roots.split(":") if item.strip()]
        if args.roots
        else [gitops.repo_root(Path.cwd())]
    )
    result = prune_module.prune(roots, args.older_than, args.dry_run)

    mb = lambda n: f"{n / 1e6:.1f} MB"
    if not result["files"]:
        display.out("nothing to compress")
    elif args.dry_run:
        display.out(f"would compress {len(result['files'])} file(s), {mb(result['before'])}")
        for path in result["files"][:10]:
            display.out(f"  {path}")
    else:
        display.out(f"compressed {len(result['files'])} file(s)")
        display.out(f"  {mb(result['before'])} -> {mb(result['after'])}, saved {mb(result['saved'])}")
    if result.get("bytecode"):
        verb = "would free" if args.dry_run else "freed"
        display.out(f"  {verb} {mb(result['bytecode'])} of regenerable bytecode cache")
    for name in result["skipped_live"]:
        display.out(f"  skipped {name}: still running")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the dashboard.

    Imported here rather than at module scope so the loop itself never pays for
    the web package, and so a broken dashboard cannot stop a run from starting.
    """
    from web.server import configure, load_env, serve

    load_env(Path(args.env).expanduser() if args.env else Path.home() / ".config" / "lmloop" / "web.env")
    config = configure()
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.roots:
        config["roots"] = [Path(item).expanduser() for item in args.roots.split(":") if item.strip()]
    if args.read_only:
        config["read_only"] = True
    return serve(config)


def cmd_init(args: argparse.Namespace) -> int:
    target = (
        Path.cwd() / config_module.PROJECT_CONFIG
        if args.project
        else config_module.GLOBAL_CONFIG
    )
    if target.exists():
        print(f"{target} already exists")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(config_module.sample())
    print(f"wrote {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="lmloop", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="work on an objective until a stop condition hits")
    run.add_argument("objective", help="what to work on; '-' reads stdin")
    run.add_argument("--agent", help="which agent does the typing: pi, omp, or opencode")
    run.add_argument("--model", help="override the configured model")
    run.add_argument("--tools", help="override the agent's tool allowlist")
    run.add_argument("--thinking", help="thinking level: off, minimal, low, medium, high, xhigh, max")
    run.add_argument("--gate", help="override the commit gate command")
    run.add_argument("--max-iterations", type=int, help="override the iteration cap")
    run.add_argument("--detach", action="store_true", help="start in the background and print the run id")
    # How --detach tells its child which lane it picked, so parent and child
    # agree on the id even when today already used the derived one.
    run.add_argument("--run-id", help=argparse.SUPPRESS)
    run.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="continue a run that stopped, in its existing worktree")
    resume.add_argument("run_id", nargs="?", help="which run; defaults to the most recent")
    resume.add_argument("--iterations", type=int, default=3, help="how many more iterations to run")
    resume.add_argument("--agent", help="which agent does the typing: pi, omp, or opencode")
    resume.add_argument("--model", help="override the configured model")
    resume.add_argument("--thinking", help="thinking level: off, minimal, low, medium, high, xhigh, max")
    resume.set_defaults(func=cmd_resume)

    runs = sub.add_parser("list", help="runs for this repo, with iteration and commit counts")
    runs.set_defaults(func=cmd_list)

    status = sub.add_parser("status", help="what a run is doing right now")
    status.add_argument("run_id", nargs="?", help="which run; defaults to the most recent")
    status.add_argument("--json", action="store_true", help="emit the raw status document")
    status.set_defaults(func=cmd_status)

    listing = sub.add_parser("models", help="what is loaded, measured, and selectable")
    listing.add_argument("--detect", action="store_true", help="measure the loaded model's real context")
    listing.set_defaults(func=cmd_models)

    prune = sub.add_parser("prune", help="compress finished runs' event streams (deletes nothing)")
    prune.add_argument("--roots", help="colon-separated directories to search; default is this repo")
    prune.add_argument("--older-than", type=float, default=0.0, metavar="DAYS",
                       help="only streams untouched for this many days")
    prune.add_argument("--dry-run", action="store_true", help="report what would be compressed")
    prune.set_defaults(func=cmd_prune)

    web = sub.add_parser("web", help="serve the dashboard: start, watch, pause and stop runs")
    web.add_argument("--host", help="bind address (default 127.0.0.1; anything else needs OIDC)")
    web.add_argument("--port", type=int, help="port (default 8082)")
    web.add_argument("--roots", help="colon-separated directories to discover projects under")
    web.add_argument("--read-only", action="store_true", help="serve the views, refuse every control")
    web.add_argument("--env", help="env file to load (default ~/.config/lmloop/web.env)")
    web.set_defaults(func=cmd_web)

    init = sub.add_parser("init", help="write a starting config")
    init.add_argument("--project", action="store_true", help="write ./.lmloop.toml instead of the global config")
    init.set_defaults(func=cmd_init)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
