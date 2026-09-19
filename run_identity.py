"""Readable, collision-resistant run identifiers."""

import time

# Words that start an instruction without describing it.  Only true filler is
# listed: the verb is kept, because "fix the login" and "document the login"
# are different runs and the first word is what says which.
SLUG_FILLER = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "some", "any",
    "i", "we", "you", "id", "ive", "lets", "let", "please",
    "want", "wants", "wanted", "need", "needs", "needed", "like", "would",
    "should", "could", "can", "will", "shall", "to", "for", "of", "and",
    "so", "then", "now", "just", "really", "very", "up",
    "objective", "task", "goal", "prompt", "instructions", "feature", "issue",
})

SLUG_MAX = 36


def make_slug(prompt: str) -> str:
    """The readable middle of a run id.

    Two things were wrong with taking the first 24 characters.  It cut inside a
    word -- `make-the-gamble-king-fro` names a directory and a branch that will
    be read a hundred times and says "fro" in the middle of it -- and it spent a
    third of its budget on the words that begin every objective ever written.
    So: drop leading filler, then stop at a word boundary rather than a column.
    """
    import re

    # If prompt starts with markdown headers or labels, find the real subject line
    clean_prompt = prompt.strip()
    lines = [line.strip() for line in clean_prompt.splitlines() if line.strip()]
    first_meaningful = ""
    for line in lines:
        line_sub = re.sub(r"^[#\s*\->:]+", "", line).strip()
        # Skip generic headings like "# Objective" or "## Summary"
        words_in_line = [w for w in re.split(r"[^a-z0-9]+", line_sub.lower()) if w]
        non_filler = [w for w in words_in_line if w not in SLUG_FILLER]
        if non_filler:
            first_meaningful = line_sub
            break

    target_text = first_meaningful if first_meaningful else clean_prompt
    every = [w for w in re.split(r"[^a-z0-9]+", target_text.lower()) if w]
    words = [w for w in every if w not in SLUG_FILLER]
    if not words:
        words = every

    kept: list[str] = []
    length = 0
    for word in words:
        addition = len(word) + (1 if kept else 0)
        if kept and length + addition > SLUG_MAX:
            break
        # A single word longer than the whole budget still has to be cut, but
        # only when it would otherwise leave the slug empty.
        kept.append(word if kept or len(word) <= SLUG_MAX else word[:SLUG_MAX])
        length += addition
    return "-".join(kept)


def make_run_id(prompt: str) -> str:
    """``<date>-<slug>-<hash>``.

    the predecessor's hash suffix is a good collision strategy; leading with the date is
    the part it is missing, so a directory of runs sorts chronologically.
    """
    import hashlib

    digest = hashlib.sha256(prompt.encode()).hexdigest()[:6]
    return f"{time.strftime('%Y-%m-%d')}-{make_slug(prompt) or 'run'}-{digest}"
