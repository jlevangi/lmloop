"""Shipped configuration values. Parsing, validation, and overrides live in config.py."""

import models

DEFAULTS: dict = {
    "agent": {
        # Which agent does the typing.  See harness.py -- the loop needs an
        # argv and a JSON event stream, and nothing else, so swapping one is a
        # config line rather than a rewrite.
        #
        # "pi" also covers anything layered on pi, including the npm package
        # called oh-my-pi, which is an extension pi auto-discovers.  "omp" is a
        # different project of almost the same name -- github.com/can1357/oh-my-pi,
        # a fork with its own binary and its own browser, task and LSP tools --
        # and it is never selected implicitly.
        "harness": "pi",
        # No default, on purpose.  This shipped as `llama-swap/local-fast`, one
        # person's model name on one person's server -- it does not exist even
        # on the machine it was written for any more, so the default could only
        # fail, and it failed *late*: after a worktree, a branch, a prompt and a
        # preflight.  Empty fails immediately, saying what to set.
        "model": "",
        # Every extension in ~/.pi/agent/settings.json adds tool definitions,
        # and those escape whatever budget a harness compacts to.  On a 57344
        # declared window this allowlist is the cheapest lever that works.
        #
        # `replace` is pi-hashline-edit-pro's editor.  It is listed because
        # leaving it out does not save an agent from editing -- it just pushes
        # it into `bash` heredocs, which is worse in every way: no diff, no
        # partial-failure reporting, and nothing the event stream can count.
        #
        # This list is pi's.  omp rejects `replace` and `ls` outright, so a
        # project that selects omp and leaves this untouched gets omp's own
        # default instead -- see `config.resolve_tools`, and docs/operations.md
        # for the allowlist to use when the work has a user interface in it.
        "tools": "read,write,edit,replace,bash,grep,find,ls",
        # Passed to `pi --thinking` when set; empty means pi's default.
        #
        # A reasoning model can deliberate its entire output budget away
        # before it emits a single tool call.  local-fast produced 45k
        # characters weighing up test cases -- "Actually, let me
        # reconsider", twice -- hit the 8192-token cap mid-sentence, and
        # ended the message with the write it was building never sent.
        # local-wide did the same thing at the same cap.  On a local
        # model the deliberation is not free thinking, it is the budget
        # the work needed.
        "thinking": "",
        # Planning and editing are different jobs, and on local hardware they
        # want different models.
        #
        # Deciding what the steps are is a whole-repository question: it wants
        # the widest context available and can afford to be slow, because it
        # happens once per run.  Carrying out a step is a two-file question that
        # happens every iteration, where throughput is what matters and a large
        # window is wasted.  local-wide has a 90112-token prompt budget against
        # local-fast's 49152; local-fast produces real edits at several times the rate.
        #
        # Empty means "use the model above for both", which is the behaviour
        # this had before and remains a perfectly reasonable setting.
        "planner_model": "",
        "planner_thinking": "",
        # Where omp's browser tool should attach, when the allowlist includes
        # it.  Only omp has a browser; every other harness ignores this.
        #
        # It must be an HTTP CDP *discovery* endpoint -- omp rejects `ws://`
        # and `wss://` by name -- and it must not need a credential in its
        # query string, because omp's attach drops one.  See browser.py, which
        # says which of those you have before a run starts rather than after.
        #
        # Left empty, the browser tool falls back to omp's own configuration:
        # its `browser.cdpUrl` setting, its relay, or a headless Chromium it
        # launches itself.  Setting it here only adds the preflight.
        #
        # A CDP endpoint is credentials -- anything that can reach it can read
        # every page the browser has open -- so, like `[notify] url`, it may
        # point at its value instead of holding it: `env:NAME`, `file:PATH`,
        # `!command`.  See `config.reference`.
        "browser_cdp_url": "",
    },
    "models": {
        # llama-swap directly, not through a router.  A router reports model
        # metadata rather than how the weights were loaded, and declaring its
        # numbers killed runs on HTTP 400 mid-iteration.
        #
        # The address itself comes from ~/.config/lmloop/model-budgets.json,
        # which the pi extension reads too -- one place to edit when the box
        # moves, rather than one here and one in a JavaScript file nobody
        # remembers is there.  A repo's own .lmloop.toml still overrides it.
        #
        # Names a host, so it may point at its value the way `[notify] url`
        # does -- `env:NAME`, `file:PATH`, `!command` -- for the operator whose
        # GPU box is a separate machine and would rather not commit its
        # address.  See `config.reference`.
        "llama_swap_url": models.budgets()["llama_swap_url"],
        # Which model-id prefixes mean "served by that llama-swap".  Seeded from
        # the same shared file, and overridable per repo like everything else
        # here -- a project pointed at a different local server should not have
        # to edit a file the pi extension also reads.  An empty list turns the
        # local path off: no preflight, no measured window, every model's
        # metadata from the agent's own catalogue.
        "local_providers": models.budgets()["local_providers"],
        # Whether to pause when the model server is simply NOT THERE, as opposed
        # to there and failing.  On a workstation whose GPU is also the machine
        # its owner plays games on, llama-swap being stopped for an hour or two
        # is routine, not an incident -- so the loop waits it out and picks the
        # iteration back up rather than burning the run.  Observed before this
        # existed: the server was stopped by hand, the loop retried 1m/2m/4m and
        # then ended the run with "model server unreachable".
        #
        # Deliberately NOT the same policy as a server that answers and
        # misbehaves: that still gets the short 1m/2m/4m backoff, because a
        # server which is up and broken does not fix itself by being waited on.
        # The two are told apart by whether `GET /running` answers at all.
        #
        # A positive value enables the hold; 0 restores the old bounded backoff.
        # The historical name remains config-compatible, but an enabled hold is
        # now an explicit PAUSE and has no timer: restart the provider, then
        # resume through the ordinary dashboard/keyboard control.
        "server_wait_seconds": 21600,
    },
    "worktree": {
        "root": "{repo}/.worktrees/{run_id}",
        "branch": "lmloop/{run_id}",
        # Untracked paths to link from the repo into the worktree.
        #
        # `git worktree add` materialises tracked files and nothing else, so a
        # fresh worktree has the source but not the environment that runs it.
        # Watched live on one project: the agent spent an hour and 24 tool calls
        # hunting for a python3 that could import Flask, because flask lives in
        # `~/git/some-project/.venv`, `.venv` is untracked, and the worktree
        # therefore had no virtualenv at all.  It never wrote a line -- it was
        # stuck trying to verify work it could not run.  The same iteration's
        # gate had already failed `rc=127` for the same reason.
        #
        # Symlinked rather than copied: a virtualenv bakes absolute paths into
        # its shebangs and pyvenv.cfg, so a copy either points back at the
        # original anyway or breaks, and node_modules is too big to duplicate
        # per run.  The trade is that a run shares one environment with the repo
        # and with other runs -- an agent that installs a package changes it for
        # everyone.  That is the right default for a loop whose whole job is to
        # run the project's own code, but it is why this is a list you can empty.
        #
        # Paths that do not exist are skipped, and every name here is added to
        # the git exclude list so `git add -A` cannot sweep the link into a
        # commit.
        "link": [".venv", "venv", "node_modules"],
    },
    "iteration": {
        # local-fast's best measured iteration was 87 minutes; local-wide did
        # not finish one in 100.  Any timeout here is a backstop, not a budget.
        #
        # Treat that local-wide figure as unproven rather than settled.  It rests on
        # "9-10K output tokens, did not finish", and that project has since shown
        # local-fast producing 10184 output tokens in 69 minutes while thrashing on
        # context overflow -- the same signature.  Nobody was counting
        # compactions when local-wide was measured, so a slow model and a model out of
        # room look identical in that number.  ``max_compactions`` below is what
        # tells them apart.
        "timeout_seconds": 14400,
        "stall_seconds": 1200,
        # Give up on an iteration that has overflowed its context this many times
        # without writing anything.  Observed on one project: six overflows in 69
        # minutes, 81 tool calls, all reads.  Each overflow discards everything
        # the agent had read, so the third one is not a slow start, it is a loop.
        # 0 disables the check.
        "max_compactions": 3,
        # Cut an iteration short when the agent calls the same tool on the same
        # target this many times running with nothing between that could have
        # changed the answer.  Different failure from `max_compactions`: that one
        # is the window losing to the codebase, this one is a model that fits
        # fine and is not reading its own tool results.  Observed: 222 tool
        # calls in 1h45m cycling the same reads, while every clock the loop had
        # watched for silence and the agent was never silent.  2 would fire on
        # an honest retry; 0 disables the check.
        "max_repeats": 3,
        # Cut an iteration short when a single tool call has been running this
        # long without returning.  A different question from `stall_seconds`,
        # which asks how long the *agent* may be silent and is routinely raised
        # into the hours for a slow model -- so it is the wrong clock for a
        # subprocess that will never return.
        #
        # Observed: an agent started a headless Chrome inside its bash tool for
        # a frontend objective and the browser never exited.  That blocked the
        # tool call, which blocked the agent, which went silent; the repository
        # had `stall_seconds = 3600`, so the run sat idle for 38 minutes.
        #
        # Generous by default, because a real build or test suite is a tool
        # call too and killing one of those is worse than waiting.  0 disables
        # the check and leaves `stall_seconds` as the only clock.
        "tool_seconds": 1800,   # 30m
    },
    "planning": {
        "pre_write_file_limit": 3,
        "steps_per_iteration": 1,
    },
    "notify": {
        # A run is unattended for hours by design, so the moment it ends is the
        # moment nobody is watching.  One push when it stops, never per
        # iteration: a notification every twenty minutes for ten hours is a
        # channel you learn to ignore, which costs more than it gives.
        "url": "",            # e.g. "https://ntfy.example.com"
        "topic": "lmloop",
        "token": "",          # bearer, if the server requires one
        # Makes the notification tap through to the run in the dashboard.
        "dashboard_url": "",  # e.g. "https://lmloop.example.com"
    },
    "prune": {
        # Sweep when a run ends, rather than on a timer.  A run is exactly when
        # the disk usage happens and exactly when someone is around to see the
        # result, and a cron job that quietly rewrites run directories at 3am is
        # harder to trust than one line at the end of a run that says what it
        # did.  Nothing is deleted but regenerable bytecode; see prune.py.
        "after_run": True,
        # 0 sweeps every finished run in the repository, including the one that
        # has just ended -- which is the one holding the space.
        "older_than_days": 0.0,
    },
    "gate": {
        "command": "",
        "blocks_commit": False,
    },
    "env": {
        # What the agent and the gate see of the host environment.  The default
        # is an allowlist, because the alternative -- and what this was until
        # lm-ka5.9 -- hands every credential in the operator's shell to a
        # process that is about to run arbitrary commands and commit files.
        # See env.py for what the base list covers and why it is broad.
        #
        # "all" restores the old behaviour for anyone who has looked at this and
        # wants their whole environment anyway.
        "inherit": "allowlist",
        # Extra names to pass, exact or with a trailing `*`.  This is also the
        # opt-in for credentials the harness genuinely needs in the environment
        # rather than in its own config file, e.g. "ANTHROPIC_API_KEY": naming
        # one here is an explicit decision and exempts it from the
        # credential-name filter.  That exemption takes the exact name -- a
        # `*` entry still adds variables but never opts a credential in.
        "pass": [],
        # Names to withhold whatever else allowed them.  Wins over everything.
        "block": [],
    },
    "stop": {
        # The point of the project is a big objective worked down over many
        # short iterations, so the iteration cap is not the safety rail -- it
        # was 3, which cannot decompose anything.  `no_diff_iterations` and
        # `max_wall_hours` are the guards that actually stop a run going
        # nowhere, and both watch evidence rather than counting.
        "max_iterations": 20,  # legacy alias; new configs use the two keys below
        "initial_turns": 20,
        "hard_turn_ceiling": 20,
        "max_wall_hours": 10,
        "no_diff_iterations": 3,
        # A fixed iteration count is the wrong shape for a plan whose length is
        # not known when the run starts.  With this on, the budget is recomputed
        # from the plan every iteration -- one per step, plus `retry_allowance`
        # spare -- so a step that needs two attempts does not cost the run its
        # last step, and a plan the agent grows mid-run grows the budget with
        # it.  `max_iterations` stops being the target and becomes the ceiling.
        "budget_follows_plan": True,
        "retry_allowance": 5,
    },
}
