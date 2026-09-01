"""What lmloop needs from an agent, and how each one provides it.

The loop is not really about pi.  Everything that makes it worth having -- the
plan, the handoff, git as the only witness, the structural checks, never
discarding work -- is independent of which agent does the typing.  What is
pi-specific is narrow: an argv, and the shape of the JSON events it streams.

So that is what an adapter is.  Each one answers three questions:

    what command runs an iteration
    which lines are worth parsing at all
    what does this event mean

Everything downstream speaks the vocabulary below, and knows nothing about the
agent that produced it.

Every bundled adapter was written against captured output, not documentation.

One naming trap is worth stating once, because two different projects answer to
`oh-my-pi`.  The npm package of that name is a pi *extension* -- pi discovers it
and it arrives through `PiHarness` needing no adapter at all.  `OmpHarness` is
the other one: `github.com/can1357/oh-my-pi`, whose binary is `omp`, a
fork of pi rather than a layer on it.  It has its own binary, its own `~/.omp`,
and enough divergence in its argv and its stream to need an adapter of its own.
"""

from harness_base import COMPACTION, MESSAGE_END, TOOL, TOOL_END, Harness, _tail
from harness_omp import OMP_DEFAULT_TOOLS, OMP_TOOLS, OMP_UI_TOOLS, OmpHarness
from harness_opencode import OpencodeHarness
from harness_pi import PiHarness

__all__ = [
    "COMPACTION", "MESSAGE_END", "TOOL", "TOOL_END", "Harness",
    "PiHarness", "OmpHarness", "OpencodeHarness",
    "OMP_TOOLS", "OMP_DEFAULT_TOOLS", "OMP_UI_TOOLS", "get", "_tail",
]

_HARNESSES = {h.name: h for h in (PiHarness(), OmpHarness(), OpencodeHarness())}


def get(name: str) -> Harness:
    try:
        return _HARNESSES[(name or "pi").strip().lower()]
    except KeyError:
        raise SystemExit(
            f"lmloop: unknown harness {name!r}; known: {', '.join(sorted(_HARNESSES))}"
        ) from None
