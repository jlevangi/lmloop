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
anywhere -- it is the output that the shared column parser turned into
`9router/(97)` and `llama-swap/(7)`, and the test that reads it is what keeps
the reason omp has its own parser checkable rather than asserted.

`pi-settings.json` is captured for the same reason. It is the file `lmloop
doctor` could not see, and the package in it that blocks `git` outright is
the one a hand-written fixture would never have thought to include —
two investigations had already blamed something else.

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
