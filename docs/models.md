# Local models, measured

Numbers here were measured on this hardware, not read off a model card. Model
cards are actively misleading for self-hosted serving: a router advertised
1,000,000 context for a model running with `--ctx-size 65536`, and declaring
that killed three runs on HTTP 400 mid-iteration.

## Where the catalogue comes from

`~/.pi/agent/extensions/model-catalog.js` registers **two** providers despite
its name:

| Provider | Endpoint | For |
|---|---|---|
| `9router` | `https://router.example.com/v1` | cloud models |
| `llama-swap` | `http://127.0.0.1:8080/v1` | self-hosted, **no router in the path** |

Never pass `--no-extensions` to pi. That extension is the entire model
catalogue; disabling extension discovery takes both providers with it.

## Declared windows

llama-swap models get their real `n_ctx` from
`~/.config/lmloop/model-context.json`, written by `lmloop models --detect`. The
extension then splits it into a prompt budget and an output budget that sum to
the real window exactly, so overshooting is impossible.

The model names below are placeholders for one deployment's lineup — a fast
model with a narrower window and a wider, slower one. Substitute your own; the
numbers come from `models --detect` against whatever you actually run, and the
point of the table is the *shape*, not the rows.

| Model | Real | Prompt | Output |
|---|---|---|---|
| local-fast | 65,536 | 49,152 | 16,384 |
| local-wide | 196,608 | 172,032 | 24,576 |
| local-wide-agent | 32,768 | 24,576 | 6,144 |
| local-* | unmeasured | 24,576 | 6,144 |

One row here changed from 98,304 the day its `--ctx-size` was raised on the
box. The figures are whatever `models --detect` last wrote, so re-read them
after changing llama-swap's config rather than trusting this table.

The default split reserves `HEADROOM = 8192` for output. That assumes the
*prompt* runs out first, which is wrong for a reasoning model — both models
above have spent an entire iteration deliberating and hit the 8192 cap
mid-sentence without emitting a tool call. `OUTPUT_OVERRIDE` rebalances per
model; it replaces HEADROOM on both sides, so the sum still lands on the real
window.

### One place to change it

`~/.config/lmloop/model-budgets.json` holds the split policy and the llama-swap
address. Both sides read it: `models.py` here, and the pi extension that
actually configures the agent.

```json
{
  "llama_swap_url": "http://127.0.0.1:8080",
  "headroom": 8192,
  "output_override": { "local-wide": 24576, "local-fast": 16384 },
  "unmeasured_context": 24576,
  "local_provider": "llama-swap"
}
```

`local_provider` is which provider prefix means "a local server this machine can
measure for itself". Everything above -- the `/running` preflight, the
`--ctx-size` measurement, the context cache -- applies to that provider and no
other, and nothing in `models.py` compares a provider to a literal.

**If you have no local server, set it to `""`.** The local path then turns off
completely: no preflight, no cache lookup, and every model's window comes from
the agent's own config the way a router-backed model's already does. llama-swap
is one deployment, not a requirement.

This exists because those numbers used to be written down three times -- in
`models.py`, in `lmloop models --detect`, and in the extension -- and had
already drifted. `models.py` reserved the default 8192 for local-wide where the
extension reserves 24,576, so lmloop spent a run believing in 16K of prompt room
pi had never been given. That is not only a readout: `Run.window` is that value,
and the thrash escalation in `loop.py` picks a rescue model by comparing it.

Both sides keep their own defaults behind `??` and `_FALLBACK`, so deleting the
file changes nothing. It is a place to edit, not a dependency.

**One caveat that is easy to trip over.** In the extension the constants are
`const`, so declaration order matters: anything reading `BUDGETS` has to come
after it or the module dies on a temporal-dead-zone `ReferenceError` at load,
and a dead extension takes the whole model catalogue with it. `node --check`
does not catch this -- it is a runtime error, not a syntax one.

An unmeasured model falls back to 24,576, which is small enough that no
llama.cpp deployment rejects it.

## Where omp reads its models

omp does not share pi's catalogue. Providers live in `~/.omp/agent/models.yml`;
`--config` overlays carry settings and not providers, and `PI_CODING_AGENT_DIR`
relocates the whole directory. `docs/operations.md` has the llama-swap block to
append, and the one thing in it that must match exactly: the provider has to be
named `llama-swap`, because everything below keys off that prefix.

That file is YAML, and lmloop is standard library only, so nothing here reads
it — and nothing needs to. omp is asked instead: `omp models --json` returns the
same catalogue as structured output, which is how a cloud model under omp gets a
declared window without anyone hand-writing a YAML parser.

Its discovery reports ids without windows, though, so omp guesses unless the
provider block declares them. It guessed 262144 for a `Qwen3.8-27B` loaded with
`--ctx-size 131072` — and omp compacts against its own number, not lmloop's, so
the guess is the one that matters. `docs/operations.md` has the block.

## The list the dashboard offers

A window is not the same question as *which models exist*, and the dashboard
asks the second one: `Harness.catalogue` returns every selector the configured
agent will accept, and `/api/models` reports in `model_source` whether the list
really came from asking it. Both live on the adapter because the answer is
agent-shaped. `web/server.py` used to parse every agent's output with pi's
column parser — first token a provider, second a model — and `omp models`
prints a provider header and then a box-drawing table, so the only lines that
survived were the headers: the dashboard offered `9router/(97)` and
`llama-swap/(7)`, two models that do not exist, and reported them as omp's own
catalogue. `omp models --json` is the parseable form, and its `selector` field
is exactly the string `--model` takes.

`list_models_argv` is the other half and stays separate: it is the question
asked *for a person*, and `lmloop models` prints what it returns untouched, so
an operator still sees their agent's own table with its context and thinking
columns.

## Models lmloop does not measure

`declared_window` only measures llama-swap, because only llama-swap will tell it
how the weights were actually loaded. For every other provider it asks *the agent
that will run the request* for its catalogue — `Harness.declared_windows`, cached
once per process. pi parses its own `~/.pi/agent/models.json`; omp is asked via
`omp models --json`; opencode has no catalogue command and reports nothing.
Asking the agent rather than one fixed file is what makes the numbers agree by
construction: before this, omp was answered out of pi's `models.json`, which
listed four models where omp knew ninety-seven, so every other one came back
with no window at all.

Before that, `declared_window` returned `None` for anything non-llama-swap, and
a `None` window is a zero window: `Run.window` fell to 0, the dashboard's gauge
went blank, and the thrash escalation ranked every routed model below everything
and so could never pick one. A 262K cloud model was invisible to the one piece
of code most likely to want it.

**This does not make 9router the sensible default for a local loop, and it is
not meant to.** Those windows are *declared*, not measured, which is the whole
distinction this file opens with -- the router advertised 1,000,000 context for
a model running with `--ctx-size 65536`. A routed `agent-local` will report
whatever `models.json` says regardless of how llama-swap actually loaded the
model. Keep self-hosted work pointed at `llama-swap/<model>` directly; the
9router path is there for when you deliberately want a cloud model.

## Measured behaviour

| Model | Character |
|---|---|
| local-fast | The workhorse. 36–52K output tokens per iteration when working; produced 124 passing tests over 14 iterations. Its failure mode is context, not speed. |
| local-wide | Wider window, much slower to act. Four iterations produced zero writes: one thought its whole budget away, two stalled, one was blocked on a missing virtualenv. Best suited to *planning*. |

That split is why `[agent] planner_model` exists: deciding the steps is a
whole-repository question that happens once and wants the widest window;
carrying one out happens every iteration and wants throughput.

Treat the older claim that "local-wide did not finish an iteration in 100 minutes" as
unproven. It rests on "9–10K output tokens, did not finish", and local-fast has
since produced 10,184 output tokens in 69 minutes *while thrashing on context* —
the same signature. Nobody was counting compactions when local-wide was measured, so a
slow model and a model out of room looked identical.

## Operational facts about llama-swap

* **It holds one model at a time.** Requesting a different one evicts the loaded
  one. A naive health check that requested a model stalled a live run this way.
* **`GET /running` is free.** `GET /upstream/<model>/props` *causes* the swap it
  was meant to observe — only use it via `lmloop models --detect`, deliberately.
* **A cold load takes about four minutes and emits nothing.** The stall clock
  deliberately does not start until the first message or tool event, or every
  cold start would look like a hang.
* **pi sets `process.title = "pi"`,** so it appears in `ps` as a bare `pi` with
  no arguments. `pkill -f 'pi --model …'` never matches; use `pkill -x pi`.

## Thinking levels

`[agent] thinking` maps to `pi --thinking`: `off`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max`. On a local model, deliberation is not free thinking —
it is the output budget the work needed. `low` is a reasonable default; `medium`
and above are worth it only for planning.
