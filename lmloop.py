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
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
import gitops
import models as models_module
from loop import Run


def cmd_run(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    repo = gitops.repo_root(cwd)
    config = config_module.load(repo)
    if args.model:
        config["agent"]["model"] = args.model
    if args.tools:
        config["agent"]["tools"] = args.tools
    if args.gate is not None:
        config["gate"]["command"] = args.gate

    objective = args.objective
    if objective == "-":
        objective = sys.stdin.read()
    objective = objective.strip()
    if not objective:
        raise SystemExit("lmloop: empty objective")

    run = Run(repo, config, objective, max_iterations=args.max_iterations)
    print(f"lmloop {run.run_id}")
    print(f"  repo:     {repo}")
    print(f"  model:    {run.model}")
    print(f"  worktree: {run.worktree}")
    print(f"  branch:   {run.branch}")
    print(f"  stop:     {run.max_iterations} iterations,"
          f" {config['stop']['max_wall_hours']}h wall clock,"
          f" {config['stop']['no_diff_iterations']} no-diff iterations")
    if config["gate"]["command"]:
        blocking = "blocks commits" if config["gate"]["blocks_commit"] else "recorded only"
        print(f"  gate:     {config['gate']['command']} ({blocking})")
    print()

    if args.dry_run:
        print("  --dry-run: nothing created")
        return 0

    run.prepare()
    return run.start()


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
            context = max(real - models_module.HEADROOM, 8192)
            print(f"{name}: real {real}, declaring {context} + {min(models_module.HEADROOM, context // 4)} output")
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

    print("\navailable to pi:")
    result = subprocess.run(["pi", "--list-models"], capture_output=True, text=True)
    print(result.stdout.strip() or result.stderr.strip())
    return 0


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
    run.add_argument("--model", help="override the configured model")
    run.add_argument("--tools", help="override the pi tool allowlist")
    run.add_argument("--gate", help="override the commit gate command")
    run.add_argument("--max-iterations", type=int, help="override the iteration cap")
    run.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    run.set_defaults(func=cmd_run)

    listing = sub.add_parser("models", help="what is loaded, measured, and selectable")
    listing.add_argument("--detect", action="store_true", help="measure the loaded model's real context")
    listing.set_defaults(func=cmd_models)

    init = sub.add_parser("init", help="write a starting config")
    init.add_argument("--project", action="store_true", help="write ./.lmloop.toml instead of the global config")
    init.set_defaults(func=cmd_init)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
