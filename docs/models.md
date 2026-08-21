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

| Model | Real | Prompt | Output |
|---|---|---|---|
| local-fast | 65,536 | 49,152 | 16,384 |
| local-wide | 98,304 | 73,728 | 24,576 |
| local-* | unmeasured | 24,576 | 6,144 |

The default split reserves `HEADROOM = 8192` for output. That assumes the
*prompt* runs out first, which is wrong for a reasoning model — both models
above have spent an entire iteration deliberating and hit the 8192 cap
mid-sentence without emitting a tool call. `OUTPUT_OVERRIDE` in the extension
rebalances per model; it replaces HEADROOM on both sides, so the sum still lands
on the real window.

An unmeasured model falls back to 24,576, which is small enough that no
llama.cpp deployment rejects it.

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
