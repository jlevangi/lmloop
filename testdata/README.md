# Captured agent output

One example of each event variant each bundled agent actually emits, taken
from real runs and redacted. Used by `test_harness_contract.py`.

| File | Captured from |
|---|---|
| `pi-events.jsonl` | an archived pi run |
| `omp-events.jsonl` | a live omp worktree |
| `opencode-events.jsonl` | an lmloop run driving opencode against a local llama-swap |

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
