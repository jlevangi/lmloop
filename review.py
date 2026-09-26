"""Review personas: which ones look at a run, and what they said.

Pure functions, so selection and parsing are testable without a model.
The loop that runs reviewers lives in loop.py; see docs/review.md.
"""
from __future__ import annotations

import re
from pathlib import Path

BUILTIN = Path(__file__).resolve().parent / "web" / "skills" / "review"
OPERATOR = Path.home() / ".config" / "lmloop" / "personas"

# Fixed order: cheaper checks first, and correctness gates the rest.
ORDER = ("correctness", "security", "performance", "design")

_SECURITY_PATHS = re.compile(
    r"(^|/)(server|auth|api|service|security|policy|env)[^/]*\.py$"
    r"|(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|go\.mod|Cargo\.toml)$"
    r"|\.(env|pem|key)$|(^|/)(config|settings)[^/]*\.(toml|ya?ml|json)$",
    re.I,
)
_SECURITY_DIFF = re.compile(r"subprocess|os\.system|shell=True|eval\(|exec\(|password|secret|token", re.I)
_DESIGN_PATHS = re.compile(r"\.(html|css|scss|jsx|tsx|vue|svelte)$|(^|/)(static|assets)/", re.I)
_PERF_WORDS = re.compile(r"\b(speed|fast|faster|slow|latency|memory|performance|perf|throughput)\b", re.I)


def select(changed: list[str], objective: str, plan: str, diff: str,
           pinned: list[str]) -> list[tuple[str, str]]:
    """(persona, why) in review order.  `pinned` replaces automatic selection."""
    if pinned:
        return [(name, "pinned") for name in pinned]
    chosen = [("correctness", "always")]
    hit = next((p for p in changed if _SECURITY_PATHS.search(p)), "")
    if hit or _SECURITY_DIFF.search(diff):
        chosen.append(("security", f"touches {hit}" if hit else "diff calls subprocess/eval or handles secrets"))
    if _PERF_WORDS.search(objective) or _PERF_WORDS.search(plan):
        chosen.append(("performance", "objective or plan mentions performance"))
    hit = next((p for p in changed if _DESIGN_PATHS.search(p)), "")
    if hit:
        chosen.append(("design", f"touches {hit}"))
    return chosen


def parse(text: str) -> tuple[str, list[str]]:
    """("APPROVED" | "CHANGES_REQUESTED" | "", findings).  "" means no verdict."""
    verdict = ""
    match = re.search(r"^\s*verdict:\s*\**\s*(APPROVED|CHANGES_REQUESTED)\b", text, re.I | re.M)
    if match:
        verdict = match.group(1).upper()
    findings = [m.strip() for m in re.findall(r"^\s*[-*]\s+(.+)$", text, re.M)]
    return verdict, findings


def brief(name: str) -> str:
    """Operator persona overrides a built-in of the same name."""
    for root in (OPERATOR, BUILTIN):
        path = root / f"{name}.md"
        if path.is_file():
            return path.read_text()
    raise SystemExit(f"lmloop: unknown review persona `{name}` "
                     f"(looked in {OPERATOR} and {BUILTIN})")


if __name__ == "__main__":
    assert select(["a/web/static/x.css"], "", "", "", [])[-1][0] == "design"
    assert [n for n, _ in select(["web/server.py"], "make it fast", "", "", [])] == \
        ["correctness", "security", "performance"]
    assert select(["x"], "", "", "", ["design"]) == [("design", "pinned")]
    assert parse("verdict: **CHANGES_REQUESTED**\n- [a.py:3] do x\n") == ("CHANGES_REQUESTED", ["[a.py:3] do x"])
    assert parse("nothing") == ("", [])
    print("review.py: ok")
