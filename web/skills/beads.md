# Issue tracking: Beads (`bd`)

This repository tracks work with `bd`. Its data lives outside the git tree, so
using it never dirties your diff.

- `bd show <id>` — full description and acceptance criteria of an issue.
- `bd ready` — open work with nothing blocking it.
- `bd create "title" --description "..." --deps discovered-from:<id>` — record
  work you found but are not doing now. Do this instead of writing TODO lists
  or TODO comments.
- `bd remember "fact"` — a lesson the next iteration or run should know.
- `bd memories <keyword>` — look up what earlier sessions recorded.

Rules:
- Never close an issue you were not assigned. The harness closes the run's
  issue when the plan is complete.
- Do not run `bd dolt push`, `bd sync`, or anything that talks to a remote.
- Keep `bd` calls few: one `bd show` at the start is usually all you need.
