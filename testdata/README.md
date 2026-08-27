# Captured agent output

One example of each event variant each bundled agent actually emits, taken
from real runs and redacted. Used by `test_harness_contract.py`.

| File | Captured from |
|---|---|
| `pi-events.jsonl` | an archived pi run |
| `omp-events.jsonl` | a live omp worktree |
| `opencode-events.jsonl` | an lmloop run driving opencode against a local llama-swap |
| `pi-models.txt` | `pi --list-models` |
| `omp-models.txt` | `omp models` |
| `omp-models.json` | `omp models --json` |
| `pi-settings.json` | `~/.pi/agent/settings.json` |

The three catalogue files are used by `test_harness_contract.py` and are not
redacted: a catalogue is a list of model names, which is the entire thing under
test. `omp-models.txt` is captured for the same reason a failing input is kept
anywhere -- it is the output that the shared column parser turns into a
handful of bogus `provider/(count)` entries, one per provider's table header,
and the test that reads it is what keeps the reason omp has its own parser
checkable rather than asserted.

Unlike the two event-stream files, these three are not captured from a real
operator's agent, on purpose: a real catalogue names which paid routing
services and which locally-served models someone actually uses, which is
exactly the kind of thing this repository's guard for captured fixtures exists
to catch (see the portability sweep that filed lm-8ws). What the tests need is
the *shape* -- two-plus providers, 90-odd models between them, a mix of
reasoning and non-reasoning entries -- not particular names, so they are
captured for real against a throwaway catalogue instead:

```bash
tools/gen-fake-catalog
```

It writes a scratch `models.yml`/`models.json` under a throwaway
`PI_CODING_AGENT_DIR` -- naming nothing but invented providers ("demo-cloud",
"demo-local") and invented models -- then runs the real `pi --list-models`,
`omp models` and `omp models --json` against it and writes the three files
here from what they actually say. Still a real capture, not a hand-edited one;
just against a config with nothing in it that identifies anyone. Needs `pi`
and `omp` on `PATH`; touches no network and no llama-swap.

`pi-settings.json` is captured for the same reason, and is the one file here
that is trimmed: it is a copy of a real `~/.pi/agent/settings.json`, and the
rest of an operator's installed package list is their business rather than this
repository's. What is left is the shape and the entry that matters — the one
`lmloop doctor` could not see, and that a hand-written fixture would never have
thought to include, since two investigations had already blamed something else.

opencode shares none of the other two's vocabulary — no `message_end`, tool
calls arrive as one `tool_use` with the result already attached, and usage rides
on `step_finish` rather than a message — which is exactly why it is captured
rather than assumed.

## Redaction

Default-deny, the same shape as `env.py`. Every string value is replaced with
a `<key>` placeholder or a neutral stand-in (`src/example.py`, `make test`)
unless its key is structural — `type`, `role`, `stopReason`, `toolName`,
`state`, `reason`. Nothing from the source repository can arrive through a
field the redactor did not anticipate, because the redactor does not need to
anticipate it.

Numbers survive: token counts, timestamps and durations are the part an
adapter has to keep agreeing with, and none of them carries content.

## Regenerating

Re-capture rather than edit. Hand-editing a fixture turns a captured contract
back into an invented one, which is the thing these exist not to be — and the
two bugs they guard against (`auto_compaction_*` vs `compaction_*`, and omp
rejecting `--list-models`) were both cases where what the code believed and
what the agent did had quietly diverged.
